"""Retention for the verbose task event log.

``task_events`` gets one row per log line, and ``TaskLogger.log()`` commits each one
immediately. A single ChatGPT registration emits on the order of a hundred events -
``browser_register.py`` alone has 200+ ``log()`` call sites - so a server that
registers continuously grows this table without bound. Nothing ever deleted from it:
``TaskLogsRepository`` only reads.

Two things make that a deployment problem rather than a cosmetic one:

* the SQLite file grows forever on a box with a fixed disk;
* WAL mode means the write-ahead log grows alongside it, and because ``auto_vacuum``
  is not enabled, deleting rows does not give the space back by itself.

This module adds an age window plus a hard row cap. It trims in small batches so a
purge never holds the write lock for long, never deletes events that belong to a task
still running (the UI is watching those), and checkpoints the WAL afterwards. A full
``VACUUM`` is available but opt-in, because it rewrites the whole database under an
exclusive lock.

``task_logs`` - the per-account success/failure record - is deliberately NOT purged.
That is history, not a log.

Everything is off unless configured: ``ZCJ_TASK_EVENT_RETENTION_DAYS`` (default 14) and
``ZCJ_TASK_EVENT_MAX_ROWS`` (default 200000) can each be set to 0 to disable that half.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlmodel import Session

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_ROWS = 200_000
DEFAULT_BATCH = 5_000
# Guard against a pathological loop if a delete somehow matches nothing.
MAX_BATCHES = 10_000


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def retention_days() -> int:
    """Age window in days. 0 disables age-based trimming."""
    return _env_int("ZCJ_TASK_EVENT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def retention_max_rows() -> int:
    """Hard row cap. 0 disables cap-based trimming."""
    return _env_int("ZCJ_TASK_EVENT_MAX_ROWS", DEFAULT_MAX_ROWS)


def vacuum_enabled() -> bool:
    """Whether to run a full VACUUM after a purge that actually deleted rows."""
    raw = str(os.environ.get("ZCJ_RETENTION_VACUUM", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def retention_enabled() -> bool:
    return retention_days() > 0 or retention_max_rows() > 0


def _blocked_task_ids():
    """Subquery of tasks whose events must not be trimmed.

    A running task streams events into the UI; deleting the tail of that stream
    mid-flight would make the progress view lose history for no disk benefit.
    """
    from core.db import TaskModel

    try:
        from application.tasks import ACTIVE_TASK_STATUSES, TASK_STATUS_PENDING

        blocked = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
    except Exception:
        blocked = []
    if not blocked:
        return None
    return select(TaskModel.id).where(TaskModel.status.in_(blocked))


def _select_ids(session: Session, stmt) -> list:
    """Collect integer ids from a single-column select.

    ``SQLModel.Session.exec()`` returns ``Row`` objects for ``select(Model.id)``
    rather than bare scalars, and those Rows cannot be bound back into an ``IN``
    clause. ``Session.execute().all()`` is used here and the unwrapping is done
    explicitly so this keeps working across SQLModel versions.
    """
    ids = []
    for row in session.execute(stmt).all():
        value = row[0] if isinstance(row, (tuple, list)) or hasattr(row, "_mapping") else row
        if value is not None:
            ids.append(int(value))
    return ids


def _delete_ids(session: Session, ids: list) -> int:
    if not ids:
        return 0
    from core.db import TaskEventModel

    session.execute(delete(TaskEventModel).where(TaskEventModel.id.in_(ids)))
    session.commit()
    return len(ids)


def purge_task_events(
    *,
    engine=None,
    days: int | None = None,
    cap: int | None = None,
    batch: int = DEFAULT_BATCH,
    dry_run: bool = False,
    checkpoint: bool = True,
    vacuum: bool | None = None,
) -> dict:
    """Trim ``task_events`` by age and by row count.

    Returns a summary and never raises for a missing table (a fresh install has none
    until the first log line), so it is safe to call from the scheduler.
    """
    from core.db import TaskEventModel, engine as default_engine, is_sqlite

    eng = engine if engine is not None else default_engine
    window = retention_days() if days is None else int(days)
    limit_rows = retention_max_rows() if cap is None else int(cap)
    do_vacuum = vacuum_enabled() if vacuum is None else bool(vacuum)
    size = max(int(batch), 1)

    result = {
        "deleted_by_age": 0,
        "deleted_by_cap": 0,
        "deleted_total": 0,
        "remaining": 0,
        "batches": 0,
        "dry_run": bool(dry_run),
        "wal_checkpointed": False,
        "vacuumed": False,
        "skipped_reason": "",
    }
    if window <= 0 and limit_rows <= 0:
        result["skipped_reason"] = "retention disabled"
        return result

    blocked = _blocked_task_ids()

    with Session(eng) as session:
        try:
            result["remaining"] = int(
                session.exec(select(func.count()).select_from(TaskEventModel)).one()[0] or 0
            )
        except Exception as exc:
            result["skipped_reason"] = "task_events 不可用: %s" % exc
            return result

        if window > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=window)
            for _ in range(MAX_BATCHES):
                stmt = select(TaskEventModel.id).where(TaskEventModel.created_at < cutoff)
                if blocked is not None:
                    stmt = stmt.where(TaskEventModel.task_id.not_in(blocked))
                ids = _select_ids(session, stmt.limit(size))
                if not ids:
                    break
                if dry_run:
                    result["deleted_by_age"] += len(ids)
                    break
                result["deleted_by_age"] += _delete_ids(session, ids)
                result["batches"] += 1
                if len(ids) < size:
                    break

        if limit_rows > 0:
            count_stmt = select(func.count()).select_from(TaskEventModel)
            if blocked is not None:
                count_stmt = count_stmt.where(TaskEventModel.task_id.not_in(blocked))
            try:
                live = int(session.exec(count_stmt).one()[0] or 0)
            except Exception:
                live = 0
            excess = live - limit_rows
            while excess > 0 and result["batches"] < MAX_BATCHES:
                stmt = select(TaskEventModel.id)
                if blocked is not None:
                    stmt = stmt.where(TaskEventModel.task_id.not_in(blocked))
                ids = _select_ids(
                    session,
                    stmt.order_by(TaskEventModel.id).limit(min(size, excess)),
                )
                if not ids:
                    break
                if dry_run:
                    result["deleted_by_cap"] += len(ids)
                    break
                result["deleted_by_cap"] += _delete_ids(session, ids)
                result["batches"] += 1
                excess -= len(ids)

        result["deleted_total"] = result["deleted_by_age"] + result["deleted_by_cap"]
        if not dry_run and result["deleted_total"] > 0:
            result["remaining"] = max(result["remaining"] - result["deleted_total"], 0)

    if not dry_run and checkpoint and result["deleted_total"] > 0:
        if is_sqlite(_url_of(eng)):
            result["wal_checkpointed"] = _checkpoint(eng)
            if do_vacuum:
                result["vacuumed"] = _vacuum(eng)
    return result


def _url_of(engine) -> str:
    try:
        return str(engine.url)
    except Exception:
        return ""


def _checkpoint(engine) -> bool:
    """Fold the WAL back into the main file and truncate it.

    Without this the -wal file keeps holding the pages of everything just deleted.
    """
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        return True
    except Exception:
        return False


def _vacuum(engine) -> bool:
    """Rewrite the database so freed pages actually return to the filesystem.

    ``auto_vacuum`` is not enabled on existing databases, so ``incremental_vacuum``
    would be a no-op; only a full ``VACUUM`` reclaims space. It takes an exclusive
    lock and rewrites the file, hence opt-in.
    """
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
            conn.commit()
        return True
    except Exception:
        return False


def run_retention_cycle() -> dict:
    """Scheduler entry point. Never raises."""
    if not retention_enabled():
        return {"skipped_reason": "retention disabled"}
    try:
        return purge_task_events()
    except Exception as exc:
        return {"error": str(exc)[:300]}
