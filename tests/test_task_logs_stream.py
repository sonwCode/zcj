"""The SSE task-log stream the frontend actually consumes.

The stream that serves ``/api/tasks/{task_id}/logs/stream`` lives in
``application/task_commands.py::stream_task_events``. A second, near-identical
implementation used to exist behind ``api/task_logs.py`` (``/events/stream``), but the
frontend never called it and its terminal-status set had silently drifted - it was
missing ``interrupted``, so an interrupted task's stream would never emit ``done``.

That drift went unnoticed precisely because nothing pinned this contract. These tests
therefore target the *live* implementation and assert what matters most: the framing
and cursor behaviour, and that every terminal status - including ``interrupted`` -
produces exactly one ``done`` event carrying a usable label.

The generator polls once a second and loops until the task is terminal, so each test
seeds a task that is *already* terminal; the stream drains the backlog and stops.
``_drain`` also caps the chunk count so a regression fails instead of hanging.
"""
from __future__ import annotations

import asyncio
import json

from sqlmodel import Session

from application.task_commands import TaskCommandsService
from core import db as db_module
from core.db import TaskEventModel, TaskModel


def _seed(
    task_id: str = "t-stream",
    *,
    status: str = "running",
    messages=("a", "b"),
    error: str | None = None,
) -> list[int]:
    """One task plus its events; returns the event ids in insertion order."""
    with Session(db_module.engine) as session:
        session.add(
            TaskModel(
                id=task_id,
                type="register",
                platform="chatgpt",
                status=status,
                error=error,
            )
        )
        session.commit()
        ids = []
        for message in messages:
            event = TaskEventModel(task_id=task_id, message=message)
            session.add(event)
            session.commit()
            session.refresh(event)
            ids.append(int(event.id))
        return ids


def _drain(task_id: str, *, since: int = 0, cap: int = 40) -> list[str]:
    """Collect the stream's chunks; an already-terminal task ends at once."""
    service = TaskCommandsService()

    async def collect() -> list[str]:
        chunks: list[str] = []
        async for chunk in service.stream_task_events(task_id, since=since):
            chunks.append(chunk)
            if len(chunks) >= cap:  # a regression must fail, not hang
                break
        return chunks

    return asyncio.run(collect())


def _data_payloads(chunks: list[str]) -> list[dict]:
    payloads = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            payloads.append(json.loads(line[len("data: "):]))
    return payloads


def _done_payloads(chunks: list[str]) -> list[dict]:
    return [p for p in _data_payloads(chunks) if p.get("done")]


def test_stream_opens_with_a_retry_hint_and_a_comment():
    _seed(status="succeeded")

    chunks = _drain("t-stream")

    assert chunks[0] == "retry: 5000\n"
    assert chunks[1] == ": connected\n\n"


def test_stream_orders_events_and_keeps_their_ids():
    ids = _seed(status="succeeded")

    payloads = _data_payloads(_drain("t-stream"))

    assert [p["message"] for p in payloads[:2]] == ["a", "b"]
    assert [int(p["id"]) for p in payloads[:2]] == ids


def test_stream_honours_the_since_cursor():
    ids = _seed(status="succeeded")

    payloads = _data_payloads(_drain("t-stream", since=ids[0]))

    assert [p["message"] for p in payloads if "message" in p] == ["b"]


def test_a_succeeded_task_ends_with_one_done_event():
    _seed(status="succeeded")

    done = _done_payloads(_drain("t-stream"))

    assert len(done) == 1, "the stream must close itself instead of looping"
    assert done[0]["status"] == "succeeded"
    assert done[0]["line"] == "任务已完成"


def test_an_interrupted_task_also_ends_with_done():
    """The discriminator for the drifted copy: it did not treat this status as terminal."""
    _seed(status="interrupted")

    done = _done_payloads(_drain("t-stream"))

    assert len(done) == 1, "an interrupted task must still terminate the stream"
    assert done[0]["status"] == "interrupted"
    assert done[0]["line"] == "任务已中断"


def test_a_cancelled_task_reports_cancellation():
    _seed(status="cancelled")

    done = _done_payloads(_drain("t-stream"))

    assert len(done) == 1
    assert done[0]["line"] == "任务已取消"


def test_a_failed_task_surfaces_its_error_message():
    _seed(status="failed", error="boom")

    done = _done_payloads(_drain("t-stream"))

    assert len(done) == 1
    assert done[0]["line"] == "boom"


def test_an_unknown_task_is_reported_rather_than_hanging():
    done = _done_payloads(_drain("no-such-task"))

    assert len(done) == 1
    assert done[0]["status"] == "failed"
    assert done[0]["line"] == "任务不存在"
