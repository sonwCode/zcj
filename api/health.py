from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from application.health import HealthService

router = APIRouter(tags=["health"])
service = HealthService()


@router.get("/health")
def health():
    return service.health()


@router.get("/ready")
def ready():
    """Readiness probe used by Docker HEALTHCHECK and load balancers.

    Returns 503 when the database or the platform registry is unavailable so a
    scheduler can act on the status code instead of parsing the body. Liveness
    (``/health``) deliberately stays 200: the process is up even when a dependency
    is not, and restarting it would not help.
    """
    payload = service.readiness()
    if not payload.get("ok"):
        return JSONResponse(status_code=503, content=payload)
    return payload
