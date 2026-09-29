"""Persistent task runtime for single-process execution."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
import time
import uuid

from application.tasks import (
    TASK_LANE_ACCOUNT_ACTION,
    TASK_LANE_ACCOUNT_CHECK,
    TASK_LANE_MAIN,
    TASK_LANE_PAYMENT,
    TASK_LANE_PLATFORM_ACTION,
    TASK_LANE_REGISTER,
    claim_next_runnable_task,
    execute_task,
    mark_incomplete_tasks_interrupted,
    request_cancel,
    renew_task_lease,
    task_lane,
    TASK_LEASE_RENEW_SECONDS,
)


@dataclass(slots=True)
class TaskWorkerState:
    thread: threading.Thread
    task_type: str = ""
    lane: str = "main"
    platform: str = ""
    account_keys: set[str] = field(default_factory=set)


class TaskRuntime:
    def __init__(
        self,
        *,
        max_parallel_tasks: int = 3,
        max_parallel_per_platform: int = 1,
        max_parallel_check_tasks: int = 1,
        lane_capacities: dict[str, int] | None = None,
        poll_interval: float = 0.5,
    ):
        self.max_parallel_tasks = max_parallel_tasks
        self.max_parallel_per_platform = max_parallel_per_platform
        self.max_parallel_check_tasks = max(int(max_parallel_check_tasks), 1)
        # Defaults preserve the original main/register capacity, while
        # allowing each independent operation class to make progress.  The
        # dictionary is public/configurable so new lanes can be added without
        # another scheduler rewrite.
        self.lane_capacities = {
            TASK_LANE_MAIN: max(int(max_parallel_tasks), 1),
            TASK_LANE_REGISTER: max(int(max_parallel_tasks), 1),
            TASK_LANE_ACCOUNT_CHECK: self.max_parallel_check_tasks,
            TASK_LANE_ACCOUNT_ACTION: 1,
            TASK_LANE_PLATFORM_ACTION: 1,
            TASK_LANE_PAYMENT: 1,
        }
        for lane, capacity in (lane_capacities or {}).items():
            if str(lane).strip():
                self.lane_capacities[str(lane)] = max(int(capacity), 1)
        self.platform_limited_lanes = {TASK_LANE_MAIN, TASK_LANE_REGISTER}
        self.poll_interval = poll_interval
        self._running = False
        self._dispatcher: threading.Thread | None = None
        self._workers: dict[str, TaskWorkerState] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._stop_timeout = 30.0
        self.worker_id = f"runtime-{os.getpid()}-{uuid.uuid4().hex}"

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._wake_event.clear()
            mark_incomplete_tasks_interrupted()
            self._dispatcher = threading.Thread(target=self._loop, daemon=True, name="task-runtime")
            self._dispatcher.start()
            print("[TaskRuntime] 已启动")

    def stop(self, *, timeout: float | None = None) -> None:
        with self._lock:
            self._running = False
            worker_ids = list(self._workers)
        self._stop_event.set()
        self._wake_event.set()
        for task_id in worker_ids:
            try:
                request_cancel(task_id)
            except Exception:
                pass
        dispatcher = self._dispatcher
        wait_timeout = self._stop_timeout if timeout is None else max(float(timeout), 0.0)
        if dispatcher is not None and dispatcher.is_alive():
            dispatcher.join(timeout=wait_timeout)
        deadline = time.monotonic() + wait_timeout
        while True:
            with self._lock:
                workers = [state.thread for state in self._workers.values()]
            alive = [worker for worker in workers if worker.is_alive()]
            if not alive or time.monotonic() >= deadline:
                break
            remaining = max(deadline - time.monotonic(), 0.0)
            for worker in alive:
                worker.join(timeout=min(0.2, remaining))
        self._reap_workers()
        print("[TaskRuntime] 已停止")

    def wake_up(self) -> None:
        self._wake_event.set()

    def get_status(self) -> dict:
        with self._lock:
            workers = list(self._workers.values())
            return {
                "running": bool(self._running),
                "dispatcher_alive": bool(self._dispatcher and self._dispatcher.is_alive()),
                "worker_count": sum(1 for state in workers if state.thread.is_alive()),
            }

    def _loop(self) -> None:
        while self._running:
            self._reap_workers()
            with self._lock:
                running_lane_counts: dict[str, int] = {}
                running_platform_counts: dict[str, int] = {}
                busy_account_keys: set[str] = set()
                for state in self._workers.values():
                    running_lane_counts[state.lane] = running_lane_counts.get(state.lane, 0) + 1
                    if state.platform and state.lane in self.platform_limited_lanes:
                        key = f"{state.lane}:{state.platform}"
                        running_platform_counts[key] = running_platform_counts.get(key, 0) + 1
                    busy_account_keys.update(state.account_keys)
                available_slots = sum(
                    max(int(capacity) - running_lane_counts.get(lane, 0), 0)
                    for lane, capacity in self.lane_capacities.items()
                )
            # Lanes are scheduled independently.  A check, OAuth, payment, or
            # future lane may start while registration slots are occupied.
            while available_slots > 0 and self._running:
                task_info = claim_next_runnable_task(
                    running_platform_counts=running_platform_counts,
                    busy_account_keys=busy_account_keys,
                    max_parallel_per_platform=self.max_parallel_per_platform,
                    running_lane_counts=running_lane_counts,
                    lane_capacities=self.lane_capacities,
                    platform_limited_lanes=self.platform_limited_lanes,
                    worker_id=self.worker_id,
                )
                if not task_info:
                    break
                lane = str(task_info.get("lane") or task_lane(str(task_info.get("type") or "")))
                if lane not in self.lane_capacities:
                    lane = TASK_LANE_MAIN
                if running_lane_counts.get(lane, 0) >= self.lane_capacities[lane]:
                    break
                running_lane_counts[lane] = running_lane_counts.get(lane, 0) + 1
                available_slots = sum(
                    max(int(capacity) - running_lane_counts.get(name, 0), 0)
                    for name, capacity in self.lane_capacities.items()
                )
                task_id = task_info["id"]
                worker = threading.Thread(
                    target=self._run_task,
                    args=(task_id, self.worker_id),
                    daemon=True,
                    name=f"task-worker-{task_id}",
                )
                with self._lock:
                    self._workers[task_id] = TaskWorkerState(
                        thread=worker,
                        task_type=str(task_info.get("type") or ""),
                        lane=lane,
                        platform=str(task_info.get("platform", "") or ""),
                        account_keys=set(task_info.get("account_keys") or []),
                    )
                    if lane in self.platform_limited_lanes and task_info.get("platform"):
                        key = f"{lane}:{task_info['platform']}"
                        running_platform_counts[key] = running_platform_counts.get(key, 0) + 1
                    busy_account_keys.update(set(task_info.get("account_keys") or []))
                worker.start()
            self._wake_event.wait(self.poll_interval)
            self._wake_event.clear()
        self._reap_workers()

    def _run_task(self, task_id: str, owner_id: str) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_lease,
            args=(task_id, owner_id, heartbeat_stop),
            daemon=True,
            name=f"task-lease-{task_id}",
        )
        heartbeat.start()
        try:
            execute_task(task_id, owner_id=owner_id)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)
            with self._lock:
                self._workers.pop(task_id, None)

    @staticmethod
    def _renew_lease(task_id: str, owner_id: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(TASK_LEASE_RENEW_SECONDS):
            try:
                if not renew_task_lease(task_id, owner_id):
                    return
            except Exception:
                return

    def _reap_workers(self) -> None:
        with self._lock:
            finished = [task_id for task_id, worker in self._workers.items() if not worker.thread.is_alive()]
            for task_id in finished:
                self._workers.pop(task_id, None)


task_runtime = TaskRuntime()
