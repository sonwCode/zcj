from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["tasks"])
service = TasksQueryService()


@router.get("")
def list_tasks(platform: str = "", status: str = "", page: int = 1, page_size: int = 50):
    return service.list_tasks(platform=platform, status=status, page=page, page_size=page_size)


@router.get("/{task_id}")
def get_task(task_id: str):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/events")
def list_task_events(task_id: str, since: int = 0, limit: int = 200):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return service.list_events(task_id, since=since, limit=limit)

class ManualOtpPayload(BaseModel):
    code: str
    request_id: str = ""
    task_id: str = ""
    email: str = ""


@router.get("/otp/waiting")
def list_manual_otp_waiting():
    """人工验证码通道：列出正在等待人工输入验证码的任务。"""
    from core.manual_otp import manual_otp_broker

    return {"waiting": manual_otp_broker.list_waiting()}


@router.post("/otp/submit")
def submit_manual_otp(payload: ManualOtpPayload):
    """人工验证码通道：把操作员输入的验证码交给正在等待的注册任务。"""
    from core.manual_otp import manual_otp_broker

    request = manual_otp_broker.submit(
        payload.code,
        request_id=payload.request_id,
        task_id=payload.task_id,
        email=payload.email,
    )
    if request is None:
        raise HTTPException(404, "没有等待中的验证码请求，或该请求已过期")
    return {"ok": True, "request": request.to_dict()}
