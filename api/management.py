from __future__ import annotations

from collections import Counter

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from application.account_checks import AccountChecksService
from application.bitbrowser_profiles import bitbrowser_profile_pool
from core.account_graph import load_account_graphs
from core.db import AccountModel, SmsPoolBlacklistModel, TaskModel, engine
from core.lifecycle import check_accounts_validity, flag_expiring_trials, lifecycle_manager
from core.mihomo_config import mihomo_config_manager
from core.scheduler import scheduler


router = APIRouter(prefix="/management", tags=["management"])


class LifecycleCheckRequest(BaseModel):
    platform: str = "chatgpt"
    limit: int = Field(default=100, ge=1, le=1000)


class LifecycleWarningRequest(BaseModel):
    hours: int = Field(default=48, ge=1, le=24 * 365)


class TokenRefreshRequest(BaseModel):
    platform: str = "chatgpt"
    concurrency: int = Field(default=100, ge=1, le=200)


@router.get("/summary")
def summary():
    with Session(engine) as session:
        accounts = session.exec(select(AccountModel)).all()
        graphs = load_account_graphs(session, [int(item.id or 0) for item in accounts if item.id])
        task_rows = session.exec(select(TaskModel.status, func.count()).group_by(TaskModel.status)).all()
    platform_counts = Counter(item.platform for item in accounts)
    lifecycle_counts = Counter()
    validity_counts = Counter()
    plan_counts = Counter()
    for item in accounts:
        graph = graphs.get(int(item.id or 0), {})
        lifecycle_counts[str(graph.get("lifecycle_status") or "unknown")] += 1
        validity_counts[str(graph.get("validity_status") or "unknown")] += 1
        plan_counts[str(graph.get("plan_state") or "unknown")] += 1
    return {
        "accounts": len(accounts),
        "by_platform": dict(platform_counts),
        "by_lifecycle_status": dict(lifecycle_counts),
        "by_validity_status": dict(validity_counts),
        "by_plan_state": dict(plan_counts),
        "tasks": {str(status): int(count) for status, count in task_rows},
    }


@router.get("/integrations")
def integrations():
    with Session(engine) as session:
        blacklist_count = int(session.exec(select(func.count()).select_from(SmsPoolBlacklistModel)).one() or 0)
    return {
        "registration_engine": {"status": "ready", "name": "aBaiFreeGPT ChatGPT engine", "isolated": True},
        "legacy_registration": {"status": "ready", "name": "other platform workbench", "isolated": True},
        "mihomo": {"status": "configured" if mihomo_config_manager.config_path.is_file() else "not_configured"},
        "bitbrowser": {"status": "configured" if bitbrowser_profile_pool.list_profiles() else "not_configured", "profiles": len(bitbrowser_profile_pool.list_profiles())},
        "sms_blacklist": {"status": "ready", "items": blacklist_count},
    }


@router.get("/lifecycle/status")
def lifecycle_status():
    return {
        "running": lifecycle_manager._running,
        "check_interval_hours": lifecycle_manager.check_interval / 3600,
        "warning_hours": lifecycle_manager.warning_hours,
        "scheduler_running": scheduler._running,
        "daily_401_hour": scheduler.daily_401_hour,
        "daily_401_concurrency": scheduler.daily_401_concurrency,
    }


@router.post("/lifecycle/check")
def lifecycle_check(body: LifecycleCheckRequest):
    return {"ok": True, "data": check_accounts_validity(platform=body.platform, limit=body.limit)}


@router.post("/lifecycle/warn")
def lifecycle_warn(body: LifecycleWarningRequest):
    return {"ok": True, "data": flag_expiring_trials(hours_warning=body.hours)}


@router.post("/lifecycle/refresh")
def lifecycle_refresh(body: TokenRefreshRequest):
    return AccountChecksService().check_refresh_tokens_async(body.platform, body.concurrency, browser=True)
