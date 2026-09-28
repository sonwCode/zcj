from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from application.task_logs import TaskLogsService

router = APIRouter(prefix="/tasks", tags=["task-logs"])
service = TaskLogsService()


@router.get("/logs")
def list_task_logs(platform: str = "", page: int = 1, page_size: int = 50):
    return service.list_logs(platform=platform, page=page, page_size=page_size)


# NOTE: there is deliberately no ``GET /tasks/{task_id}/events`` here. One already
# exists in ``api/tasks.py`` (``since=``, and it 404s on an unknown task). Both
# were registered under the same path, so whichever router was included first won
# and the other was unreachable: the frontend asks for ``?since=`` while this copy
# took ``after_id=``, so the shadowed handler would have silently ignored the
# cursor and replayed the whole log on every poll. It also produced a duplicate
# OpenAPI operation id, which breaks generated clients. The polling endpoint
# lives in api/tasks.py; only the SSE stream is defined here.
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