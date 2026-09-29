from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session

from core.db import engine
from core.registry import list_platforms
from services.solver_manager import is_running


def _task_runtime_status() -> dict:
    try:
        from services.task_runtime import task_runtime

        status = task_runtime.get_status()
        status["ok"] = bool(status.get("running") and status.get("dispatcher_alive"))
        return status
    except Exception as exc:
        return {"ok": False, "running": False, "dispatcher_alive": False, "error": str(exc)[:200]}


def _scheduler_status() -> dict:
    try:
        from core.scheduler import scheduler

        status = scheduler.get_status()
        status["ok"] = bool(status.get("running") and status.get("thread_alive"))
        return status
    except Exception as exc:
        return {"ok": False, "running": False, "thread_alive": False, "error": str(exc)[:200]}


class HealthRuntime:
    def health(self) -> dict:
        return {"ok": True, "service": "account-manager-v2"}

    def readiness(self) -> dict:
        db_ok = False
        db_error = ""
        registry_ok = False
        registry_error = ""
        try:
            with Session(engine) as session:
                session.exec(text("SELECT 1"))
            db_ok = True
        except Exception as exc:
            db_error = str(exc)

        try:
            platforms = list_platforms()
            platform_count = len(platforms)
            registry_ok = True
        except Exception as exc:
            platforms = []
            platform_count = 0
            registry_error = str(exc)

        task_runtime = _task_runtime_status()
        scheduler = _scheduler_status()
        # Solver is an optional helper process and is reported for diagnostics,
        # but it does not make the account manager unready by itself.
        return {
            "ok": db_ok and registry_ok and task_runtime.get("ok", False) and scheduler.get("ok", False),
            "database": {"ok": db_ok, "error": db_error},
            "registry": {"ok": registry_ok, "platform_count": platform_count, "error": registry_error},
            "task_runtime": task_runtime,
            "scheduler": scheduler,
            "solver": {"running": is_running()},
        }
