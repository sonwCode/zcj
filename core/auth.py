"""Session authentication middleware for the application API.

The configured application password is only used to mint an opaque, short-lived
session. It is never accepted as a bearer token or stored in a browser cookie.
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_COOKIE_NAME = "_auth"
DEFAULT_SESSION_TTL_SECONDS = 3600
MAX_SESSION_TTL_SECONDS = 86400
_PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/api/ready",
        "/api/auth/check",
        "/api/auth/login",
    }
)
_sessions: dict[str, float] = {}
_sessions_lock = threading.RLock()


def _get_password() -> str:
    return os.environ.get("APP_PASSWORD", "").strip()


def session_ttl_seconds() -> int:
    """Return a bounded session lifetime, allowing short test deployments."""
    raw = os.environ.get("AUTH_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL_SECONDS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_SESSION_TTL_SECONDS
    return max(1, min(value, MAX_SESSION_TTL_SECONDS))


def _prune_expired(now: float | None = None) -> None:
    current = time.time() if now is None else now
    for token, expires_at in list(_sessions.items()):
        if expires_at <= current:
            _sessions.pop(token, None)


def issue_session() -> tuple[str, int]:
    """Create an opaque in-memory session and return its token and TTL."""
    ttl = session_ttl_seconds()
    with _sessions_lock:
        _prune_expired()
        token = secrets.token_urlsafe(32)
        _sessions[token] = time.time() + ttl
    return token, ttl


def validate_session(token: str | None) -> bool:
    """Validate a session token and remove it once it expires."""
    if not token:
        return False
    with _sessions_lock:
        expires_at = _sessions.get(token)
        if expires_at is None:
            return False
        if expires_at <= time.time():
            _sessions.pop(token, None)
            return False
        return True


def revoke_session(token: str | None) -> None:
    if token:
        with _sessions_lock:
            _sessions.pop(token, None)


def session_token_from_request(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() == "bearer":
        return token.strip()
    return request.cookies.get(SESSION_COOKIE_NAME, "")


def request_is_authenticated(request: Request) -> bool:
    return validate_session(session_token_from_request(request))


def use_secure_cookie(request: Request) -> bool:
    configured = os.environ.get("AUTH_COOKIE_SECURE", "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    return request.url.scheme == "https"


def _is_public_request(request: Request) -> bool:
    path = request.url.path.rstrip("/") or "/"
    return request.method.upper() == "OPTIONS" or path in _PUBLIC_PATHS


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"

        # Static assets and non-API routes are intentionally not gated here.
        if not path.startswith("/api") or _is_public_request(request):
            return await call_next(request)

        if request_is_authenticated(request):
            return await call_next(request)

        return Response(
            content='{"detail":"Unauthorized"}',
            status_code=401,
            media_type="application/json",
        )
