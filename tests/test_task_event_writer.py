"""Buffered writes for the task event log.

The hot path used to commit one row per log line. Measured under concurrency that is
~15x slower than batching and gets worse as threads are added, because every line
queues for the SQLite write lock. These tests pin the batching behaviour and, more
importantly, the flush-on-read contract: a reader must never see a stale view just
because events are sitting in the buffer.
"""
from __future__ import annotations

import time

import pytest
from sqlmodel import Session, select

from core import db as db_module
from core.db import TaskEventModel
from core.task_event_writer import (
    MAX_FLUSH_FAILURES,
    TaskEventWriter,
    flush_pending_events,
    task_event_writer,
)


@pytest.fixture(autouse=True)
def _clean_buffer():
    """The singleton buffer survives between tests; clear it on both sides."""
    task_event_writer.flush()
    yield
    task_event_writer.stop()
    task_event_writer._buffer = []


def _count() -> int:
    with Session(db_module.engine) as session:
        return len(session.exec(select(TaskEventModel)).all())


def test_enqueue_does_not_write_until_flush():
    writer = TaskEventWriter(interval=10, max_buffer=10_000)
    for index in range(25):
        writer.enqueue("t1", "m%d" % index)

    assert writer.pending() == 25
    assert _count() == 0, "enqueue must not touch the database"

    assert writer.flush() == 25
    assert _count() == 25
    assert writer.pending() == 0


def test_flush_on_empty_buffer_is_free():
    assert TaskEventWriter().flush() == 0


def test_buffer_threshold_triggers_a_flush():
    writer = TaskEventWriter(interval=10, max_buffer=5)
    for index in range(5):
        writer.enqueue("t1", "m%d" % index)

    assert writer.pending() == 0
    assert _count() == 5


def test_order_is_preserved():
    writer = TaskEventWriter(interval=10, max_buffer=10_000)
    for index in range(10):
        writer.enqueue("t1", "m%d" % index)
    writer.flush()

    with Session(db_module.engine) as session:
        messages = [
            row.message
            for row in session.exec(select(TaskEventModel).order_by(TaskEventModel.id)).all()
        ]
    assert messages == ["m%d" % index for index in range(10)]


def test_repeated_flush_failures_spool_without_unbounded_buffer(tmp_path):
    """A dead database persists events locally instead of dropping them."""
    writer = TaskEventWriter(interval=10, max_buffer=10_000, spool_path=tmp_path / "events.jsonl")
    writer.enqueue("t1", "m")

    # Mirror flush(): it takes the rows out of the buffer before attempting the
    # write, so a retry re-queues them once rather than duplicating them.
    def attempt_failed_flush():
        rows = list(writer._buffer)
        writer._buffer = []
        writer._requeue_or_drop(rows, RuntimeError("boom"))

    for _ in range(MAX_FLUSH_FAILURES):
        attempt_failed_flush()
    assert writer.pending() == 1, "rows are retried for a few attempts"

    attempt_failed_flush()
    assert writer.pending() == 0, "spooled rows leave the in-memory buffer"
    assert writer.stats()["dropped"] == 0
    assert writer.stats()["spooled"] == 1
    assert (tmp_path / "events.jsonl").exists()


def test_stop_flushes_what_is_left(tmp_path):
    writer = TaskEventWriter(interval=10, max_buffer=10_000, spool_path=tmp_path / "events.jsonl")
    writer.enqueue("t1", "m")
    writer.stop()
    assert _count() == 1


def test_background_flusher_writes_on_its_own():
    writer = TaskEventWriter(interval=0.05, max_buffer=10_000)
    writer.start()
    writer.start()  # idempotent
    writer.enqueue("t1", "m")
    time.sleep(0.4)
    assert _count() == 1
    writer.stop()


def test_flush_pending_events_respects_the_switch(monkeypatch):
    monkeypatch.setenv("ZCJ_TASK_EVENT_BUFFER", "0")
    assert flush_pending_events() == 0


def test_readers_flush_the_buffer_first():
    """The contract the SSE stream and the polling endpoint depend on.

    Events sit in memory until something flushes. If a reader queried the table
    without flushing it would silently show a stale log, which is exactly the kind of
    bug that only shows up as "the UI lags behind" in production.
    """
    from application.tasks import TaskLogger, list_task_events

    logger = TaskLogger("t-e2e")
    logger.log("hello")
    assert task_event_writer.pending() == 1

    events = list_task_events("t-e2e")
    assert [event["message"] for event in events] == ["hello"]
    assert task_event_writer.pending() == 0


def test_sse_repository_flushes_the_buffer_first():
    from application.tasks import TaskLogger
    from infrastructure.task_logs_repository import TaskLogsRepository

    logger = TaskLogger("t-sse")
    logger.log("streamed")
    assert task_event_writer.pending() == 1

    events = TaskLogsRepository().list_events(task_id="t-sse")
    assert [event["message"] for event in events] == ["streamed"]
    assert task_event_writer.pending() == 0


def test_state_events_flush_pending_logs_first():
    """A state event must not jump ahead of log lines emitted before it."""
    from application.tasks import TaskLogger, append_task_event

    logger = TaskLogger("t-order")
    logger.log("first")
    append_task_event("t-order", "state", event_type="state")

    with Session(db_module.engine) as session:
        messages = [
            row.message
            for row in session.exec(
                select(TaskEventModel)
                .where(TaskEventModel.task_id == "t-order")
                .order_by(TaskEventModel.id)
            ).all()
        ]
    assert messages == ["first", "state"]
