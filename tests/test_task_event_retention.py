"""Retention for ``task_events``.

The event log gets one row per log line and nothing ever deleted from it, so on a
server that registers continuously the SQLite file grows without bound. These tests
pin the two trimming rules and the safety property that events belonging to a task
that is still running are never removed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from core import db as db_module
from core import retention
from core.db import TaskEventModel, TaskModel


def _add_events(count: int, *, age_days: int = 0, task_id: str = "t-done") -> None:
    with Session(db_module.engine) as session:
        for index in range(count):
            session.add(
                TaskEventModel(
                    task_id=task_id,
                    type="log",
                    message="event %d" % index,
                    created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
                )
            )
        session.commit()


def _add_task(task_id: str, status: str) -> None:
    with Session(db_module.engine) as session:
        session.add(TaskModel(id=task_id, type="register", platform="chatgpt", status=status))
        session.commit()


def _count() -> int:
    with Session(db_module.engine) as session:
        return len(session.exec(select(TaskEventModel)).all())


def test_age_window_removes_only_old_events():
    _add_task("t-done", "success")
    _add_events(10, age_days=30)
    _add_events(5, age_days=1)

    result = retention.purge_task_events(days=14, cap=0)

    assert result["deleted_by_age"] == 10
    assert _count() == 5


def test_row_cap_removes_oldest_first():
    _add_task("t-done", "success")
    _add_events(50)

    result = retention.purge_task_events(days=0, cap=20, batch=7)

    assert result["deleted_by_cap"] == 30
    assert _count() == 20
    with Session(db_module.engine) as session:
        ids = [
            row.id
            for row in session.exec(select(TaskEventModel).order_by(TaskEventModel.id)).all()
        ]
    assert min(ids) > 30, "the surviving rows must be the newest ones"


def test_events_of_a_running_task_are_never_removed():
    _add_task("t-running", "running")
    _add_task("t-done", "success")
    _add_events(20, age_days=60, task_id="t-running")
    _add_events(20, age_days=60, task_id="t-done")

    result = retention.purge_task_events(days=14, cap=0)

    assert result["deleted_by_age"] == 20
    with Session(db_module.engine) as session:
        remaining = {row.task_id for row in session.exec(select(TaskEventModel)).all()}
    assert remaining == {"t-running"}


def test_dry_run_reports_without_deleting():
    _add_task("t-done", "success")
    _add_events(30, age_days=60)

    result = retention.purge_task_events(days=14, cap=0, dry_run=True)

    assert result["deleted_by_age"] == 30
    assert _count() == 30


def test_both_halves_disabled_is_a_noop():
    _add_task("t-done", "success")
    _add_events(30, age_days=60)

    result = retention.purge_task_events(days=0, cap=0)

    assert result["skipped_reason"] == "retention disabled"
    assert _count() == 30


def test_large_trim_runs_in_batches():
    _add_task("t-done", "success")
    _add_events(500)

    result = retention.purge_task_events(days=0, cap=100, batch=64)

    assert _count() == 100
    assert result["batches"] > 1, "a big purge must not be one long write lock"


def test_env_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("ZCJ_TASK_EVENT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("ZCJ_TASK_EVENT_MAX_ROWS", raising=False)
    assert retention.retention_days() == retention.DEFAULT_RETENTION_DAYS
    assert retention.retention_max_rows() == retention.DEFAULT_MAX_ROWS

    monkeypatch.setenv("ZCJ_TASK_EVENT_RETENTION_DAYS", "3")
    monkeypatch.setenv("ZCJ_TASK_EVENT_MAX_ROWS", "0")
    assert retention.retention_days() == 3
    assert retention.retention_max_rows() == 0

    monkeypatch.setenv("ZCJ_TASK_EVENT_RETENTION_DAYS", "not-a-number")
    assert retention.retention_days() == retention.DEFAULT_RETENTION_DAYS
