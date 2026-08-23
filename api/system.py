from __future__ import annotations

from fastapi import APIRouter

from application.system import SystemService
from core.version import __version__

router = APIRouter(tags=["system"])
service = SystemService()


@router.get("/solver/status")
def solver_status():
    return service.solver_status()


@router.post("/solver/restart")
def solver_restart():
    return service.restart_solver()


@router.get("/version")
def get_version():
    """Return local version only.

    This private build intentionally does not call any upstream GitHub release API,
    so the UI will not show update prompts pointing to an external repository.
    """
    return {
        "current": __version__,
        "latest": None,
        "has_update": False,
    }
