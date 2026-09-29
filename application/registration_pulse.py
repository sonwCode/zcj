"""Pulse registration with per-node OAI IP-ban detection.

OAI temporarily bans a node's egress IP after too many registrations from it;
the observable symptom is the mailbox never receiving the verification email
(the OTP wait times out).  This controller replaces the continuous rolling
worker pool with discrete synchronized pulses:

* A wave submits at most the requested concurrency across healthy nodes,
  applies results as workers finish, and has a hard deadline so one poisoned
  browser cannot stall every later wave.
* A worker whose OTP never arrives returns ``_NO_EMAIL_MARKER``.  The
  controller requeues that account index (retry across pulses, never a
  one-worker retry) and, after ``ban_after_consecutive_no_email`` consecutive
  no-email waves for the same node, pauses that node.
* A background probe thread visits every currently banned node once per
  ``probe_interval_seconds`` cycle and runs a registration pinned to that node
  with a short OTP timeout.  A completed registration or an OTP-received
  signal proves OAI delivers mail again; failures before the OTP arrives keep
  the node paused.
  When every node is banned the pulse loop idles and probes until one recovers.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import (
    ALL_COMPLETED,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
    wait,
)
from dataclasses import dataclass
from typing import Any, Callable

from application.tasks import (
    MAX_REGISTER_CONCURRENCY,
    MAX_TASK_ACCOUNT_SUMMARIES,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCEEDED,
    _NO_EMAIL_MARKER,
    _POOL_EXHAUSTED_MARKER,
    _is_browser_infrastructure_failure,
    _is_browser_runtime_fatal_failure,
)
from core.mihomo_client import MihomoNodeError

_CANCEL_REQUESTED = "__cancel_requested__"

DEFAULT_PROBE_INTERVAL_SECONDS = 600.0
DEFAULT_PROBE_OTP_TIMEOUT_SECONDS = 90
DEFAULT_BAN_AFTER_CONSECUTIVE_NO_EMAIL = 3
# How many banned nodes a single probe cycle may test.  Probing every banned
# node in one burst floods OAI's signup endpoint from IPs we already know are
# blocked; probing a small rotating batch keeps recovery detection working
# without the "still sending verification codes" storm.
DEFAULT_PROBE_BATCH_SIZE = 5
DEFAULT_PULSE_WAVE_TIMEOUT_SECONDS = 900.0


@dataclass(slots=True)
class PulseConfig:
    pulse_interval_seconds: float = 0.0
    probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS
    probe_otp_timeout_seconds: int = DEFAULT_PROBE_OTP_TIMEOUT_SECONDS
    ban_after_consecutive_no_email: int = DEFAULT_BAN_AFTER_CONSECUTIVE_NO_EMAIL
    probe_batch_size: int = DEFAULT_PROBE_BATCH_SIZE
    wave_concurrency: int = MAX_REGISTER_CONCURRENCY
    wave_timeout_seconds: float = DEFAULT_PULSE_WAVE_TIMEOUT_SECONDS

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PulseConfig":
        def _num(key: str, default: float, *, minimum: float, maximum: float) -> float:
            try:
                value = float(payload.get(key, default))
            except (TypeError, ValueError):
                value = default
            return min(max(value, minimum), maximum)

        return cls(
            pulse_interval_seconds=_num(
                "pulse_interval_seconds", 0.0, minimum=0.0, maximum=3600.0
            ),
            probe_interval_seconds=_num(
                "probe_interval_seconds",
                DEFAULT_PROBE_INTERVAL_SECONDS,
                minimum=30.0,
                maximum=86400.0,
            ),
            probe_otp_timeout_seconds=int(
                _num(
                    "probe_otp_timeout_seconds",
                    DEFAULT_PROBE_OTP_TIMEOUT_SECONDS,
                    minimum=20.0,
                    maximum=3600.0,
                )
            ),
            ban_after_consecutive_no_email=int(
                _num(
                    "ban_after_consecutive_no_email",
                    DEFAULT_BAN_AFTER_CONSECUTIVE_NO_EMAIL,
                    minimum=1.0,
                    maximum=10.0,
                )
            ),
            probe_batch_size=int(
                _num(
                    "probe_batch_size",
                    DEFAULT_PROBE_BATCH_SIZE,
                    minimum=1.0,
                    maximum=20.0,
                )
            ),
            wave_concurrency=int(
                _num(
                    "concurrency",
                    MAX_REGISTER_CONCURRENCY,
                    minimum=1.0,
                    maximum=float(MAX_REGISTER_CONCURRENCY),
                )
            ),
            wave_timeout_seconds=_num(
                "pulse_wave_timeout_seconds",
                DEFAULT_PULSE_WAVE_TIMEOUT_SECONDS,
                minimum=60.0,
                maximum=3600.0,
            ),
        )


class PulseRegistration:
    """Drive one register task through synchronized pulses and ban probes."""

    def __init__(
        self,
        *,
        do_one: Callable[..., dict[str, Any] | str],
        allocator,
        config: PulseConfig,
        logger,
        count: int,
    ):
        self._do_one = do_one
        self._allocator = allocator
        self._config = config
        self._logger = logger
        self._count = max(int(count), 0)  # 0 => unlimited, runs until cancelled
        # Shared mutable state, guarded by ``_state_lock`` (RLock: the probe
        # thread and the pulse loop both touch counters / the index pool).
        self._state_lock = threading.RLock()
        self._pending: set[int] | None = set(range(self._count)) if self._count > 0 else None
        self._next_index = 0
        self._consumed = 0
        self._success = 0
        self._fail = 0
        self._registered: list[dict[str, Any]] = []
        self._first_error = ""
        self._cancelled = False
        # Set when a worker reports the mailbox pool is exhausted; the task then
        # stops scheduling new waves and finishes with this error.
        self._fatal_error = ""
        self._browser_infra_bad_waves = 0
        # Probe coordination.
        self._stop = threading.Event()
        self._probe_tick = threading.Event()      # immediate probe request
        self._node_recovered = threading.Event()  # a probe unbanned a node
        self._probe_thread: threading.Thread | None = None
        # Serializes the pulse wave and the probe cycle so they never compete
        # for the same Mihomo slots at the same time (avoids
        # "Mihomo registration slots are full" and the probe/wave interference).
        self._slot_lock = threading.RLock()
        # Rotating cursor across ``_allocator.banned_nodes()`` so a probe cycle
        # only tests ``probe_batch_size`` banned nodes at a time.
        self._probe_cursor = 0
        # Monotonic timestamp of the last completed probe cycle.  The probe loop
        # enforces a MINIMUM gap of ``probe_interval_seconds`` between cycles so
        # a flood of ``_probe_tick`` signals (e.g. a wave re-banning nodes right
        # after a probe unblocked one) cannot turn probing into a tight loop that
        # keeps poking OAI and extends every ban.
        self._last_probe_at = 0.0

    # ------------------------------------------------------------------ main

    def run(self) -> None:
        self._probe_thread = threading.Thread(
            target=self._probe_loop,
            name="pulse-probe",
            daemon=True,
        )
        self._probe_thread.start()
        try:
            self._pulse_loop()
        finally:
            self._stop.set()
            self._probe_tick.set()
            join_timeout = max(self._config.probe_otp_timeout_seconds + 30, 30)
            if self._probe_thread is not None:
                self._probe_thread.join(timeout=join_timeout)
            self._finalize()

    def _pulse_loop(self) -> None:
        while not self._logger.is_cancel_requested():
            healthy = self._allocator.healthy_nodes()
            if not healthy:
                self._logger.log(
                    "所有节点已封禁，注册暂停，等待探测恢复...",
                    level="warning",
                )
                # Wake the probe immediately on the first all-blocked detection
                # instead of waiting a full probe interval.
                self._probe_tick.set()
                self._wait_for_healthy_node()
                continue
            indices = self._pop_wave_indices(len(healthy))
            if not indices:
                if self._done():
                    break
                time.sleep(0.2)
                continue
            # Synchronized burst: requested workers register in parallel and
            # results are applied as each one settles.  The whole wave holds
            # ``_slot_lock`` so the probe cycle cannot run concurrently and
            # fight over the same Mihomo slots.
            with self._slot_lock:
                pool = ThreadPoolExecutor(max_workers=len(indices))
                futures = {
                    pool.submit(self._run_worker, index): index for index in indices
                }
                no_email_by_node: dict[str, list[int]] = {}
                node_had_success: set[str] = set()
                browser_infra_errors: list[str] = []
                timed_out = False
                try:
                    for future in as_completed(
                        futures,
                        timeout=self._config.wave_timeout_seconds,
                    ):
                        self._apply_wave_result(
                            future,
                            futures[future],
                            no_email_by_node,
                            node_had_success,
                            browser_infra_errors,
                        )
                        self._logger.set_progress(self._consumed, self._count)
                except FuturesTimeoutError:
                    timed_out = True
                    pending = [future for future in futures if not future.done()]
                    for future in pending:
                        future.cancel()
                    self._fatal_error = (
                        f"注册波次超过 {int(self._config.wave_timeout_seconds)} 秒，"
                        f"仍有 {len(pending)} 个 worker 未退出"
                    )
                    self._logger.log(self._fatal_error, level="error")
                finally:
                    pool.shutdown(wait=not timed_out, cancel_futures=timed_out)
                self._finish_wave_results(
                    no_email_by_node,
                    node_had_success,
                    browser_infra_errors,
                    wave_size=len(futures),
                )
            if self._fatal_error:
                # Mailbox pool exhausted or a poisoned worker exceeded the hard
                # wave deadline.  Returning lets task cleanup close the shared
                # browser pool and release any remaining Playwright calls.
                break
            if self._done() or self._logger.is_cancel_requested():
                break
            if browser_infra_errors:
                # Give a just-rebuilt Camoufox process time to settle before
                # another synchronized burst reaches it.
                self._stop.wait(min(2.0 * self._browser_infra_bad_waves, 10.0))
            if self._config.pulse_interval_seconds > 0:
                time.sleep(self._config.pulse_interval_seconds)

    def _run_worker(self, index: int):
        ctx: dict[str, Any] = {}
        result = self._do_one(
            index,
            retry_otp_once=False,
            report_no_email=False,
            worker_context=ctx,
        )
        return result, ctx

    def _apply_wave_result(
        self,
        future,
        index: int,
        no_email_by_node: dict[str, list[int]],
        node_had_success: set[str],
        browser_infra_errors: list[str],
    ) -> None:
        with self._state_lock:
            try:
                result, ctx = future.result()
            except Exception as exc:
                result = str(exc) or "worker 内部异常"
                ctx = {}
            node = str(ctx.get("node") or "")
            if isinstance(result, dict):
                self._success += 1
                self._consumed += 1
                if len(self._registered) < MAX_TASK_ACCOUNT_SUMMARIES:
                    self._registered.append(result)
                if node:
                    self._allocator.record_success(node)
                    node_had_success.add(node)
            elif result == _CANCEL_REQUESTED:
                self._cancelled = True
            elif result == _POOL_EXHAUSTED_MARKER:
                if not self._fatal_error:
                    self._fatal_error = "本地微软邮箱池已用尽，注册任务终止"
            elif result == _NO_EMAIL_MARKER:
                if node:
                    no_email_by_node.setdefault(node, []).append(index)
                else:
                    self._requeue(index)
            else:
                self._fail += 1
                self._first_error = self._first_error or str(result)
                if _is_browser_runtime_fatal_failure(result):
                    self._consumed += 1
                    self._fatal_error = f"共享浏览器池失去响应，任务终止: {result}"
                    self._logger.log(self._fatal_error, level="error")
                elif _is_browser_infrastructure_failure(result):
                    browser_infra_errors.append(str(result))
                    if self._pending is None:
                        self._consumed += 1
                    else:
                        # A browser-process crash is infrastructure failure,
                        # not an exhausted account. Retry finite tasks after
                        # the pool has rebuilt instead of silently shrinking
                        # the requested account count.
                        self._requeue(index)
                else:
                    self._consumed += 1
                if node and "Cloudflare" in str(result):
                    if node not in self._allocator.banned_nodes():
                        self._allocator.mark_blocked(node)
                        self._logger.log(
                            f"节点 {node} 触发 Cloudflare 挑战失败，直接封禁该节点",
                            level="warning",
                        )

    def _finish_wave_results(
        self,
        no_email_by_node: dict[str, list[int]],
        node_had_success: set[str],
        browser_infra_errors: list[str],
        *,
        wave_size: int,
    ) -> None:
        if self._cancelled or self._fatal_error:
            return
        crash_threshold = max((max(int(wave_size), 1) + 1) // 2, 1)
        if len(browser_infra_errors) >= crash_threshold:
            self._browser_infra_bad_waves += 1
            self._logger.log(
                "本波次共享浏览器崩溃占比过高："
                f"{len(browser_infra_errors)}/{wave_size}，"
                f"连续异常波次 {self._browser_infra_bad_waves}/2",
                level="warning",
            )
            if self._browser_infra_bad_waves >= 2:
                self._fatal_error = (
                    "共享浏览器连续两个波次大面积崩溃，已触发熔断，任务终止"
                )
                self._logger.log(self._fatal_error, level="error")
                return
        else:
            self._browser_infra_bad_waves = 0
        # Requeue every no-email index, then apply per-node ban decisions.
        for node, indexes in no_email_by_node.items():
            self._requeue_many(indexes)
            if node in node_had_success:
                # The node also registered successfully this wave: a stale/slow
                # mail is not an IP ban — clear the strike instead.
                self._allocator.record_success(node)
                continue
            strikes = self._allocator.record_no_email(node)
            if (
                strikes >= self._config.ban_after_consecutive_no_email
                and node not in self._allocator.banned_nodes()
            ):
                self._allocator.mark_blocked(node)
                self._logger.log(
                    f"节点 {node} 连续 {strikes} 次未收到验证码，判定 IP 封禁，暂停该节点",
                    level="warning",
                )
                if self._allocator.all_blocked():
                    self._logger.log(
                        "所有节点已封禁，注册暂停，等待探测恢复",
                        level="warning",
                    )
                    self._probe_tick.set()

    # ---------------------------------------------------------------- probe

    def _probe_loop(self) -> None:
        while not self._stop.is_set():
            self._probe_tick.wait(timeout=self._config.probe_interval_seconds)
            self._probe_tick.clear()
            if self._stop.is_set() or self._logger.is_cancel_requested():
                break
            if not self._allocator.banned_nodes():
                continue
            # Enforce a MINIMUM gap between probe cycles.  ``_probe_tick`` may be
            # raised by every wave that observes all-blocked, so without this
            # guard probing degenerates into a tight loop that keeps poking OAI
            # and extends every ban.  On the first cycle (``_last_probe_at == 0``)
            # probe immediately; afterwards wait out the configured interval.
            interval = max(float(self._config.probe_interval_seconds), 30.0)
            elapsed = time.monotonic() - self._last_probe_at
            if self._last_probe_at > 0 and elapsed < interval:
                wait_seconds = interval - elapsed
                if wait_seconds > 30:
                    self._logger.log(
                        f"距上次探测不足 {int(interval)}s，等待 {int(wait_seconds)}s 再探测",
                        level="debug",
                    )
                if self._stop.wait(wait_seconds):
                    break
                if self._logger.is_cancel_requested():
                    break
                if not self._allocator.banned_nodes():
                    continue
            self._run_probe_cycle()
            self._last_probe_at = time.monotonic()
            # A tick raised while probing must not fire an immediate second
            # cycle; the minimum gap and the interval gate the next run.
            self._probe_tick.clear()

    def _run_probe_cycle(self) -> None:
        banned = self._allocator.banned_nodes()
        if not banned:
            return
        # Only probe a rotating batch of banned nodes per cycle instead of the
        # whole list: probing every banned IP in one burst is the "still
        # sending verification codes" storm the user saw.
        batch_size = max(1, min(self._config.probe_batch_size, len(banned)))
        with self._state_lock:
            if self._probe_cursor >= len(banned):
                self._probe_cursor = 0
            batch = banned[self._probe_cursor : self._probe_cursor + batch_size]
            if len(batch) < batch_size:  # wrap around to the front
                batch = batch + banned[: batch_size - len(batch)]
            self._probe_cursor = (self._probe_cursor + batch_size) % len(banned)
        jobs: list[tuple[str, int]] = []
        for node in batch:
            index = self._pop_one_index()
            if index is None:
                break  # finite pool exhausted -> task is ending
            jobs.append((node, index))
        if not jobs:
            return
        # Serialized with the pulse wave so probes and registrations never
        # compete for the same Mihomo slot pool at the same time.
        with self._slot_lock:
            pool = ThreadPoolExecutor(
                max_workers=min(MAX_REGISTER_CONCURRENCY, len(jobs))
            )
            futures = [pool.submit(self._run_one_probe, node, index) for node, index in jobs]
            _, pending = wait(
                futures,
                timeout=self._config.wave_timeout_seconds,
                return_when=ALL_COMPLETED,
            )
            timed_out = bool(pending)
            if timed_out:
                for future in pending:
                    future.cancel()
                self._logger.log(
                    f"节点探测超过 {int(self._config.wave_timeout_seconds)} 秒，"
                    f"已放弃等待 {len(pending)} 个探测 worker",
                    level="error",
                )
            pool.shutdown(wait=not timed_out, cancel_futures=timed_out)

    def _run_one_probe(self, node: str, index: int) -> None:
        worker_context: dict[str, Any] = {}
        try:
            try:
                result = self._do_one(
                    index,
                    forced_node=node,
                    otp_timeout_seconds=self._config.probe_otp_timeout_seconds,
                    retry_otp_once=False,
                    report_no_email=False,
                    worker_context=worker_context,
                )
            except MihomoNodeError:
                # Node disappeared from Mihomo mid-task; stop probing it.
                self._allocator.refresh_nodes()
                self._requeue(index)
                return
            if result == _CANCEL_REQUESTED:
                self._cancelled = True
                return
            if result == _POOL_EXHAUSTED_MARKER:
                with self._state_lock:
                    if not self._fatal_error:
                        self._fatal_error = "本地微软邮箱池已用尽，注册任务终止"
                return
            if isinstance(result, dict):
                # Registration completed -> OAI delivered the verification mail.
                with self._state_lock:
                    self._success += 1
                    self._consumed += 1
                    if len(self._registered) < MAX_TASK_ACCOUNT_SUMMARIES:
                        self._registered.append(result)
                self._allocator.unblock(node)
                self._node_recovered.set()
                self._logger.log(
                    f"探测成功: 节点 {node} 已恢复，重新加入注册",
                    level="info",
                )
            elif result == _NO_EMAIL_MARKER:
                self._logger.log(
                    f"探测 {node} 未收到验证码，保持封禁",
                    level="warning",
                )
                self._requeue(index)
            elif worker_context.get("otp_received"):
                # Registration failed after the mailbox returned a real OTP.
                # Mail delivery has recovered, so the node can rejoin later
                # waves even though this particular account is retried.
                self._allocator.unblock(node)
                self._node_recovered.set()
                self._logger.log(
                    f"探测 {node} 已收到验证码，后续步骤失败但 IP 已恢复，重新加入注册: {result}",
                    level="warning",
                )
                self._requeue(index)
            else:
                # Cloudflare, TLS, homepage and other pre-OTP failures do not
                # prove that OAI is sending mail to this IP again.
                self._logger.log(
                    f"探测 {node} 在收到验证码前失败，保持封禁: {result}",
                    level="warning",
                )
                self._requeue(index)
        finally:
            self._logger.clear_subtask()

    def _wait_for_healthy_node(self) -> None:
        while not self._logger.is_cancel_requested():
            if self._allocator.healthy_nodes():
                return
            self._node_recovered.wait(timeout=5.0)
            self._node_recovered.clear()

    # ------------------------------------------------------- index pool/state

    def _pop_wave_indices(self, healthy_count: int) -> list[int]:
        # Bound each wave by all three real constraints: requested concurrency,
        # physical Mihomo slots, and currently healthy nodes.
        wave_cap = min(
            getattr(self._allocator, "slot_count", healthy_count),
            healthy_count,
            self._config.wave_concurrency,
        )
        with self._state_lock:
            if self._pending is not None:
                if not self._pending:
                    return []
                take = min(wave_cap, len(self._pending))
                return [self._pending.pop() for _ in range(take)]
            # Unlimited mode synthesises fresh indices for each wave.
            take = wave_cap
            indices = list(range(self._next_index, self._next_index + take))
            self._next_index += take
            return indices

    def _pop_one_index(self) -> int | None:
        with self._state_lock:
            if self._pending is not None:
                if not self._pending:
                    return None
                return self._pending.pop()
            index = self._next_index
            self._next_index += 1
            return index

    def _requeue(self, index: int) -> None:
        with self._state_lock:
            if self._pending is not None:
                self._pending.add(index)

    def _requeue_many(self, indexes: list[int]) -> None:
        with self._state_lock:
            if self._pending is not None and indexes:
                self._pending.update(indexes)

    def _done(self) -> bool:
        if self._count == 0:
            return False
        return self._consumed >= self._count

    # -------------------------------------------------------------- finalize

    def _finalize(self) -> None:
        # Snapshot under the lock: a probe may still be settling a registration
        # past the join timeout, so the counters must not be read mid-mutation.
        with self._state_lock:
            success = self._success
            fail = self._fail
            registered = list(self._registered)
            cancelled = self._cancelled
            fatal_error = self._fatal_error
        result_data = {
            "success": success,
            "fail": fail,
            "account_ids": [item["account_id"] for item in registered],
            "accounts": registered,
            "pulse": True,
            "unlimited": self._count == 0,
            "banned_nodes": self._allocator.banned_nodes(),
        }
        self._logger.set_result_data(result_data)
        if cancelled or self._logger.is_cancel_requested():
            self._logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        if fatal_error:
            # Mailbox pool exhausted: stop instead of looping on failures.
            self._logger.finish(TASK_STATUS_FAILED, error=fatal_error)
            return
        final_status = (
            TASK_STATUS_FAILED if (fail and success == 0) else TASK_STATUS_SUCCEEDED
        )
        self._logger.finish(final_status, error=self._first_error if success == 0 else "")
