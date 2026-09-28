"""The task-event polling endpoint, exercised over HTTP.

``GET /tasks/{task_id}/events`` used to be registered twice - once in
``api/tasks.py`` (``since=``, 404s on an unknown task) and once in
``api/task_logs.py`` (``after_id=``, no 404). FastAPI keeps the first
registration, so the ``api/task_logs.py`` copy was unreachable and the
``after_id`` cursor it took was silently ignored: the frontend asks for
``?since=``, and had the include order ever flipped, every poll would have
replayed the whole log from the beginning.

``tests/test_route_uniqueness.py`` proves the *path* is registered once. These
tests prove the *handler that survives* is the right one and that its cursor
actually filters - which is the part a schema-level assertion cannot see.
"""
from __future__ import annotations

from sqlmodel import Session

from core import db as db_module
from core.db import TaskEventModel, TaskModel


def _seed(task_id: str = "t-events") -> list[int]:
    """One task and three events; returns the event ids in insertion order."""
    with Session(db_module.engine) as session:
        session.add(TaskModel(id=task_id, type="register", platform="chatgpt", status="running"))
        session.commit()
        ids = []
        for n in (1, 2, 3):
            event = TaskEventModel(task_id=task_id, message=f"event-{n}")
            session.add(event)
            session.commit()
            session.refresh(event)
            ids.append(int(event.id))
        return ids


def test_events_endpoint_returns_the_tasks_query_shape(client):
    """The survivor returns ``items``; the removed copy returned ``events``/``cursor``."""
    _seed()

    response = client.get("/api/tasks/t-events/events")

    assert response.status_code == 200
    body = response.json()
    assert "items" in body, "the api/tasks.py handler must be the one serving this path"
    assert "cursor" not in body and "events" not in body, (
        "the shadowed api/task_logs.py handler returned these keys - it is still winning"
    )
    assert [item["message"] for item in body["items"]] == ["event-1", "event-2", "event-3"]


def test_events_endpoint_honours_the_since_cursor(client):
    """``?since=`` must filter; the unreachable copy would have ignored it entirely."""
    ids = _seed()

    response = client.get(f"/api/tasks/t-events/events?since={ids[0]}")

    assert response.status_code == 200
    messages = [item["message"] for item in response.json()["items"]]
    assert messages == ["event-2", "event-3"], "since must act as an id > cursor filter"


def test_events_endpoint_404s_on_unknown_task(client):
    """Another discriminator: only the api/tasks.py handler checks the task exists."""
    response = client.get("/api/tasks/does-not-exist/events")

    assert response.status_code == 404


def test_events_endpoint_scopes_events_to_one_task(client):
    _seed("t-one")
    _seed("t-two")

    response = client.get("/api/tasks/t-one/events")

    assert response.status_code == 200
    assert [item["message"] for item in response.json()["items"]] == [
        "event-1",
        "event-2",
        "event-3",
    ]
