"""定时任务调度 - 账号有效性检测、trial 到期提醒"""
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from .account_graph import load_account_graphs, patch_account_graph
from .base_platform import AccountStatus
from .db import engine, AccountModel
from .registry import load_all


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread = None
        self._full_cycle_thread: threading.Thread = None
        self._stop_event = threading.Event()
        self._full_cycle_lock = threading.Lock()
        self._probation_cycle_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._full_cycle_status = self._empty_cycle_status()
        self._probation_cycle_status = self._empty_cycle_status()
        self._heartbeat_at = ""
        self._next_full_run_at = ""
        self._probation_scan_interval_seconds = 5
        self._continuous_check_interval_seconds = 60

    @staticmethod
    def _empty_cycle_status() -> dict:
        return {
            "running": False,
            "last_started_at": "",
            "last_completed_at": "",
            "last_duration_seconds": 0.0,
            "last_error": "",
            "last_result": {},
        }

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _set_cycle_status(self, kind: str, **updates) -> None:
        with self._status_lock:
            target = (
                self._probation_cycle_status
                if kind == "probation"
                else self._full_cycle_status
            )
            target.update(updates)

    def get_status(self) -> dict:
        with self._status_lock:
            full = dict(self._full_cycle_status)
            probation = dict(self._probation_cycle_status)
            heartbeat_at = self._heartbeat_at
            next_full_run_at = self._next_full_run_at
        return {
            "running": bool(self._running),
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "heartbeat_at": heartbeat_at,
            "next_full_run_at": next_full_run_at,
            "probation_scan_interval_seconds": self._probation_scan_interval_seconds,
            "continuous_check_interval_seconds": self._continuous_check_interval_seconds,
            "full_cycle": full,
            "probation_cycle": probation,
        }

    def _update_runtime_status(self, *, next_full_in_seconds: float) -> None:
        next_seconds = max(float(next_full_in_seconds or 0), 0.0)
        with self._status_lock:
            self._heartbeat_at = self._now_iso()
            self._next_full_run_at = (
                datetime.now(timezone.utc) + timedelta(seconds=next_seconds)
            ).isoformat().replace("+00:00", "Z")

    def start(self):
        if self._running and self._thread and self._thread.is_alive():
            return
        try:
            from core.sub2api_sync import repair_misclassified_registry_ineligible_accounts

            repaired = repair_misclassified_registry_ineligible_accounts(limit=1000)
            if repaired:
                print(f"[Scheduler] 已修复 {repaired} 个被 Sub2 资格错误标记为失效的账号")
        except Exception as exc:
            print(f"[Scheduler] Sub2 状态修复跳过: {exc}")
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(
            "[Scheduler] 已启动: "
            f"持续复检=每{self._continuous_check_interval_seconds}s，"
            f"队列扫描={self._probation_scan_interval_seconds}s",
            flush=True,
        )

    def stop(self):
        self._running = False
        self._stop_event.set()

    def _loop(self):
        next_full_run = time.monotonic() + 60
        while self._running:
            try:
                from core.config_store import config_store

                interval_minutes = max(
                    int(config_store.get("sub2api_check_interval_minutes", "5") or 5),
                    5,
                )
            except Exception:
                interval_minutes = 5
            # Scan the persisted continuous-monitor queue frequently enough to
            # keep the one-minute cadence tight. The heavier full-account sweep
            # still follows its configured interval and first runs after one minute.
            until_full = next_full_run - time.monotonic()
            delay_seconds = max(
                1,
                min(
                    self._probation_scan_interval_seconds,
                    int(math.ceil(until_full)),
                ),
            )
            self._update_runtime_status(next_full_in_seconds=until_full)
            if self._stop_event.wait(delay_seconds):
                break
            self._update_runtime_status(
                next_full_in_seconds=next_full_run - time.monotonic()
            )
            try:
                self._run_probation_cycle()
            except Exception as e:
                print(f"[Scheduler] 持续复检错误: {e}", flush=True)
            if time.monotonic() >= next_full_run:
                if not self._full_cycle_thread or not self._full_cycle_thread.is_alive():
                    print("[Scheduler] 开始账号有效性检测...", flush=True)
                    self._full_cycle_thread = threading.Thread(
                        target=self._run_full_cycle_safely,
                        daemon=True,
                        name="account-validity-cycle",
                    )
                    self._full_cycle_thread.start()
                next_full_run = time.monotonic() + interval_minutes * 60
                self._update_runtime_status(
                    next_full_in_seconds=next_full_run - time.monotonic()
                )

    def _run_full_cycle_safely(self):
        try:
            self._run_cycle()
        except Exception as exc:
            print(f"[Scheduler] 错误: {exc}", flush=True)

    def _run_cycle(self):
        """Run one maintenance cycle.

        Account validity detection is deliberately independent from remote
        auto-deletion.  The old coupling silently disabled all periodic local
        checks whenever ``sub2api_auto_delete_invalid`` was off.
        """
        if not self._full_cycle_lock.acquire(blocking=False):
            return {"skipped": "another_cycle_running"}
        started = time.monotonic()
        self._set_cycle_status(
            "full",
            running=True,
            last_started_at=self._now_iso(),
            last_error="",
        )
        try:
            result = self._run_cycle_impl()
        except Exception as exc:
            self._set_cycle_status("full", last_error=str(exc))
            raise
        else:
            self._set_cycle_status("full", last_result=dict(result or {}))
            return result
        finally:
            self._set_cycle_status(
                "full",
                running=False,
                last_completed_at=self._now_iso(),
                last_duration_seconds=round(time.monotonic() - started, 3),
            )
            self._full_cycle_lock.release()

    def _run_cycle_impl(self):
        self.check_trial_expiry()
        from core.config_store import config_store

        auto_delete = str(
            config_store.get("sub2api_auto_delete_invalid", "false") or ""
        ).strip().lower() in {"1", "true", "yes", "on", "enabled"}

        # Local validity comes first.  Remote Sub2 maintenance must never
        # delay or suppress the core detector when that service is slow.
        check_results = self.check_accounts_valid(platform="chatgpt", limit=500)
        print(
            "[Scheduler] 账号有效性检测完成: "
            f"有效 {check_results.get('valid', 0)}, "
            f"失效 {check_results.get('invalid', 0)}, "
            f"未知 {check_results.get('unknown', check_results.get('error', 0))}",
            flush=True,
        )

        from core.sub2api_sync import (
            backfill_unsynced_accounts,
            cleanup_invalid_synced_accounts,
            reconcile_sub2_remote_statuses,
        )

        try:
            reconcile_sub2_remote_statuses(limit=500)
        except Exception as exc:
            print(f"[Scheduler] Sub2 状态同步跳过: {exc}", flush=True)
        try:
            backfill_unsynced_accounts(limit=100)
        except Exception as exc:
            print(f"[Scheduler] Sub2 补传跳过: {exc}", flush=True)
        if auto_delete:
            try:
                cleanup_invalid_synced_accounts(limit=500)
            except Exception as exc:
                print(f"[Scheduler] Sub2 清理跳过: {exc}", flush=True)
        return {"validity": check_results, "remote_cleanup_enabled": auto_delete}

    def _run_probation_cycle(self):
        if not self._probation_cycle_lock.acquire(blocking=False):
            return {"skipped": "another_cycle_running"}
        started = time.monotonic()
        self._set_cycle_status(
            "probation",
            running=True,
            last_started_at=self._now_iso(),
            last_error="",
        )
        try:
            from core.config_store import config_store
            from core.lifecycle import check_due_account_probations

            try:
                concurrency = min(
                    max(int(config_store.get("account_check_concurrency", "5") or 5), 1),
                    20,
                )
            except (TypeError, ValueError):
                concurrency = 5
            result = check_due_account_probations(
                limit=100,
                concurrency=concurrency,
            )
            self._set_cycle_status("probation", last_result=dict(result or {}))
            return result
        except Exception as exc:
            self._set_cycle_status("probation", last_error=str(exc))
            raise
        finally:
            self._set_cycle_status(
                "probation",
                running=False,
                last_completed_at=self._now_iso(),
                last_duration_seconds=round(time.monotonic() - started, 3),
            )
            self._probation_cycle_lock.release()

    def check_trial_expiry(self):
        """检查 trial 到期账号，更新状态"""
        now = int(datetime.now(timezone.utc).timestamp())
        with Session(engine) as s:
            accounts = s.exec(select(AccountModel)).all()
            graphs = load_account_graphs(s, [int(acc.id or 0) for acc in accounts if acc.id])
            updated = 0
            for acc in accounts:
                graph = graphs.get(int(acc.id or 0), {})
                if graph.get("lifecycle_status") != "trial":
                    continue
                trial_end_time = int((graph.get("overview") or {}).get("trial_end_time") or 0)
                if trial_end_time and trial_end_time < now:
                    acc.updated_at = datetime.now(timezone.utc)
                    patch_account_graph(s, acc, lifecycle_status=AccountStatus.EXPIRED.value)
                    s.add(acc)
                    updated += 1
            s.commit()
            if updated:
                print(f"[Scheduler] {updated} 个 trial 账号已到期")

    def check_accounts_valid(self, platform: str = None, limit: int = 50):
        """批量检测账号有效性"""
        load_all()
        from core.config_store import config_store
        from core.lifecycle import check_accounts_validity

        try:
            concurrency = min(
                max(int(config_store.get("account_check_concurrency", "5") or 5), 1),
                20,
            )
        except (TypeError, ValueError):
            concurrency = 5

        return check_accounts_validity(
            platform=str(platform or ""),
            limit=max(int(limit), 1),
            include_inactive=True,
            exclude_probation_pending=True,
            concurrency=concurrency,
        )


scheduler = Scheduler()
