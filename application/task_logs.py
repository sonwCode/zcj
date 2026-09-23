from __future__ import annotations

import json
import time
from typing import Iterator

from core.datetime_utils import serialize_datetime
from infrastructure.task_logs_repository import TERMINAL_TASK_STATUSES, TaskLogsRepository


class TaskLogsService:
    def __init__(self, repository: TaskLogsRepository | None = None) -> None:
        self.repository = repository or TaskLogsRepository()

    def list_logs(self, *, platform: str = "", page: int = 1, page_size: int = 50) -> dict:
        total, items = self.repository.list(platform=platform, page=page, page_size=page_size)
        return {
            "total": total,
            "page": page,
            "items": [
                {
                    "id": item.id,
                    "platform": item.platform,
                    "email": item.email,
                    "status": item.status,
                    "error": item.error,
                    "detail": item.detail or {},
                    "created_at": serialize_datetime(item.created_at),
                }
                for item in items
            ],
        }

    def list_events(self, *, task_id: str, after_id: int = 0, limit: int = 200) -> dict:
        events = self.repository.list_events(task_id=task_id, after_id=after_id, limit=limit)
        return {
            "task_id": task_id,
            "events": events,
            "cursor": events[-1]["id"] if events else int(after_id or 0),
            "status": self.repository.task_status(task_id),
        }

    def stream_events(
        self,
        *,
        task_id: str,
        after_id: int = 0,
        poll_seconds: float = 1.0,
        max_seconds: float = 300.0,
        heartbeat_seconds: float = 15.0,
    ) -> Iterator[str]:
        """Yield Server-Sent Events for one task until it reaches a terminal state.

        A sync generator is deliberate: FastAPI runs it in a worker thread, so the
        short sleep does not block the event loop, and no new dependency is needed.
        """
        started = time.monotonic()
        cursor = int(after_id or 0)
        last_emit = time.monotonic()
        idle_polls = 0
        yield ": connected\n\n"
        while True:
            if time.monotonic() - started > float(max_seconds):
                yield "event: timeout\ndata: {}\n\n"
                return
            events = self.repository.list_events(task_id=task_id, after_id=cursor, limit=200)
            for event in events:
                cursor = max(cursor, int(event.get("id") or 0))
                payload = json.dumps(event, ensure_ascii=False)
                event_type = str(event.get("type") or "log")
                yield "id: " + str(cursor) + "\nevent: " + event_type + "\ndata: " + payload + "\n\n"
                last_emit = time.monotonic()
            if events:
                idle_polls = 0
            else:
                idle_polls += 1
                now = time.monotonic()
                if (now - last_emit) >= float(heartbeat_seconds):
                    yield ": ping\n\n"
                    last_emit = now
                status = self.repository.task_status(task_id)
                if status in TERMINAL_TASK_STATUSES and idle_polls >= 2:
                    done = json.dumps({"status": status, "cursor": cursor}, ensure_ascii=False)
                    yield "event: done\ndata: " + done + "\n\n"
                    return
            time.sleep(max(float(poll_seconds), 0.2))