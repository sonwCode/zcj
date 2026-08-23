from __future__ import annotations


def test_scheduler_checks_accounts_when_auto_delete_is_disabled(monkeypatch):
    from core.config_store import config_store
    from core.scheduler import Scheduler
    from core import sub2api_sync

    calls: list[tuple[str, object]] = []
    scheduler = Scheduler()
    monkeypatch.setattr(scheduler, "check_trial_expiry", lambda: calls.append(("trial", None)))
    monkeypatch.setattr(
        scheduler,
        "check_accounts_valid",
        lambda platform=None, limit=50: calls.append(("check", (platform, limit)))
        or {"valid": 1, "invalid": 0, "unknown": 0},
    )
    monkeypatch.setattr(config_store, "get", lambda key, default="": "false" if key == "sub2api_auto_delete_invalid" else default)
    monkeypatch.setattr(
        sub2api_sync,
        "reconcile_sub2_remote_statuses",
        lambda limit=500: calls.append(("reconcile", limit)),
    )
    monkeypatch.setattr(
        sub2api_sync,
        "backfill_unsynced_accounts",
        lambda limit=100: calls.append(("backfill", limit)),
    )
    monkeypatch.setattr(
        sub2api_sync,
        "cleanup_invalid_synced_accounts",
        lambda limit=500: calls.append(("cleanup", limit)),
    )

    scheduler._run_cycle()

    assert ("check", ("chatgpt", 500)) in calls
    call_names = [name for name, _value in calls]
    assert call_names.index("check") < call_names.index("reconcile")
    assert not any(name == "cleanup" for name, _ in calls)


def test_scheduler_rechecks_retained_invalid_accounts(monkeypatch):
    from core.scheduler import Scheduler

    captured = {}

    def _check_accounts_validity(**kwargs):
        captured.update(kwargs)
        return {"valid": 0, "invalid": 0, "unknown": 0}

    monkeypatch.setattr(
        "core.lifecycle.check_accounts_validity",
        _check_accounts_validity,
    )

    Scheduler().check_accounts_valid(platform="chatgpt", limit=25)

    assert captured == {
        "platform": "chatgpt",
        "limit": 25,
        "include_inactive": True,
    }


def test_scheduler_runs_first_cycle_after_one_minute(monkeypatch):
    from core.scheduler import Scheduler

    waits = []
    scheduler = Scheduler()

    class _StopOnWait:
        def wait(self, seconds):
            waits.append(seconds)
            return True

        def set(self):
            return None

        def clear(self):
            return None

    scheduler._stop_event = _StopOnWait()
    scheduler._running = True
    scheduler._loop()

    assert waits == [60]
