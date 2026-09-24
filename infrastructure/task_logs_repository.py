from __future__ import annotations

import json

from sqlmodel import Session, select, func

from core.datetime_utils import serialize_datetime
from core.db import TaskEventModel, TaskLog, TaskModel, engine
from domain.task_logs import TaskLogRecord

TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "canceled", "completed"}


def _to_record(model: TaskLog) -> TaskLogRecord:
    try:
        detail = json.loads(model.detail_json or "{}")
    except Exception:
        detail = {}
    return TaskLogRecord(
        id=int(model.id or 0),
        platform=model.platform,
        email=model.email,
        status=model.status,
        error=model.error,
        detail=detail,
        created_at=model.created_at,
    )


class TaskLogsRepository:
    def list(self, *, platform: str = "", page: int = 1, page_size: int = 50) -> tuple[int, list[TaskLogRecord]]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        with Session(engine) as session:
            query = select(TaskLog)
            total_query = select(func.count()).select_from(TaskLog)
            if platform:
                query = query.where(TaskLog.platform == platform)
                total_query = total_query.where(TaskLog.platform == platform)
            query = query.order_by(TaskLog.id.desc())
            total = int(session.exec(total_query).one() or 0)
            items = session.exec(query.offset((page - 1) * page_size).limit(page_size)).all()
        return total, [_to_record(item) for item in items]

    def list_events(self, *, task_id: str, after_id: int = 0, limit: int = 200) -> list[dict]:
        """Return task events after a cursor, oldest first, for SSE replay."""
        if not task_id:
            return []
        # SSE 每秒轮询这里；先 flush 才能保证刚写的事件立刻可见。
        from core.task_event_writer import flush_pending_events

        flush_pending_events()
        bounded = min(max(int(limit or 0), 1), 500)
        with Session(engine) as session:
            rows = session.exec(
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .where(TaskEventModel.id > int(after_id or 0))
                .order_by(TaskEventModel.id)
                .limit(bounded)
            ).all()
        return [
            {
                "id": int(row.id or 0),
                "task_id": row.task_id,
                "type": row.type,
                "level": row.level,
                "message": row.message,
                "detail": row.get_detail(),
                "created_at": serialize_datetime(row.created_at),
            }
            for row in rows
        ]

    def task_status(self, task_id: str) -> str:
        if not task_id:
            return ""
        with Session(engine) as session:
            model = session.get(TaskModel, task_id)
        return str(getattr(model, "status", "") or "")