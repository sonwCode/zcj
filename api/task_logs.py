from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from application.task_logs import TaskLogsService

router = APIRouter(prefix="/tasks", tags=["task-logs"])
service = TaskLogsService()


@router.get("/logs")
def list_task_logs(platform: str = "", page: int = 1, page_size: int = 50):
    return service.list_logs(platform=platform, page=page, page_size=page_size)


@router.get("/{task_id}/events")
def list_task_events(task_id: str, after_id: int = 0, limit: int = 200):
    """Polling fallback for clients that cannot use the SSE stream."""
    return service.list_events(task_id=task_id, after_id=after_id, limit=limit)


@router.get("/{task_id}/events/stream")
def stream_task_events(task_id: str, after_id: int = 0):
    """Replay and then tail a task event log as Server-Sent Events."""
    return StreamingResponse(
        service.stream_events(task_id=task_id, after_id=after_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )