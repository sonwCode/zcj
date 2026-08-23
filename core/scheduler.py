"""定时任务调度 - 账号有效性检测、trial 到期提醒"""
import threading
from datetime import datetime, timezone

from sqlmodel import Session, select

from .account_graph import load_account_graphs, patch_account_graph
from .base_platform import AccountStatus
from .db import engine, AccountModel
from .registry import load_all


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._running:
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
        print("[Scheduler] 已启动")

    def stop(self):
        self._running = False
        self._stop_event.set()

    def _loop(self):
        first_cycle = True
        while self._running:
            try:
                from core.config_store import config_store

                interval_minutes = max(
                    int(config_store.get("sub2api_check_interval_minutes", "5") or 5),
                    5,
                )
            except (TypeError, ValueError):
                interval_minutes = 5
            # Run once shortly after startup so accounts created before a
            # restart do not wait a full interval.  LifecycleManager defers its
            # legacy 6-hour check, avoiding two simultaneous liveness sweeps.
            delay_seconds = 60 if first_cycle else interval_minutes * 60
            print(
                f"[Scheduler] 下一次账号有效性检测将在 {delay_seconds} 秒后运行",
                flush=True,
            )
            if self._stop_event.wait(delay_seconds):
                break
            first_cycle = False
            try:
                print("[Scheduler] 开始账号有效性检测...", flush=True)
                self._run_cycle()
            except Exception as e:
                print(f"[Scheduler] 错误: {e}", flush=True)

    def _run_cycle(self):
        """Run one maintenance cycle.

        Account validity detection is deliberately independent from remote
        auto-deletion.  The old coupling silently disabled all periodic local
        checks whenever ``sub2api_auto_delete_invalid`` was off.
        """
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
        from core.lifecycle import check_accounts_validity

        return check_accounts_validity(
            platform=str(platform or ""),
            limit=max(int(limit), 1),
            include_inactive=True,
        )


scheduler = Scheduler()
