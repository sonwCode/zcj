"""Persistent task runtime for single-process execution."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

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
    task_lane,
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

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            mark_incomplete_tasks_interrupted()
            self._dispatcher = threading.Thread(target=self._loop, daemon=True, name="task-runtime")
            self._dispatcher.start()
            print("[TaskRuntime] 已启动")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        print("[TaskRuntime] 停止中")

    def wake_up(self) -> None:
        # Polling loop wakes quickly already; this method exists as an explicit runtime hook.
        return

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
                    args=(task_id,),
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
            time.sleep(self.poll_interval)
        self._reap_workers()

    def _run_task(self, task_id: str) -> None:
        try:
            execute_task(task_id)
        finally:
            with self._lock:
                self._workers.pop(task_id, None)

    def _reap_workers(self) -> None:
        with self._lock:
            finished = [task_id for task_id, worker in self._workers.items() if not worker.thread.is_alive()]
            for task_id in finished:
                self._workers.pop(task_id, None)


task_runtime = TaskRuntime()
