from __future__ import annotations

from fastapi import APIRouter

from application.system import SystemService
from core.build_info import get_build_info
from core.scheduler import scheduler

router = APIRouter(tags=["system"])
service = SystemService()


@router.get("/solver/status")
def solver_status():
    return service.solver_status()


@router.post("/solver/restart")
def solver_restart():
    return service.restart_solver()


@router.get("/version", include_in_schema=False)
@router.get("/system/version")
def get_version():
    """Return local version only.

    This private build intentionally does not call any upstream GitHub release API,
    so the UI will not show update prompts pointing to an external repository.
    """
    build = get_build_info()
    return {
        "current": build["version"],
        "latest": None,
        "has_update": False,
        "git_sha": build["git_sha"],
        "build_time": build["build_time"],
        "started_at": build["started_at"],
    }


@router.get("/scheduler/status", include_in_schema=False)
@router.get("/system/scheduler/status")
def scheduler_status():
    """Expose scheduler heartbeat/results so silent detector death is visible."""
    return scheduler.get_status()
