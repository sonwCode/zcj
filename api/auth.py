from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from core.auth import (
    SESSION_COOKIE_NAME,
    _get_password,
    issue_session,
    request_is_authenticated,
    revoke_session,
    session_token_from_request,
    use_secure_cookie,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = ""


@router.get("/check")
def auth_check(request: Request):
    """Expose auth configuration state without exposing any credential."""
    configured = bool(_get_password())
    return {
        "required": True,
        "configured": configured,
        "authenticated": request_is_authenticated(request),
    }


@router.post("/login")
def auth_login(body: LoginRequest, response: Response, request: Request):
    password = _get_password()
    if not password:
        return {"ok": False, "error": "APP_PASSWORD 未配置"}
    if not hmac.compare_digest(body.password, password):
        return {"ok": False, "error": "密码错误"}
    token, ttl = issue_session()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl,
        httponly=True,
        secure=use_secure_cookie(request),
        samesite="lax",
        path="/",
    )
    return {"ok": True, "expires_in": ttl}


@router.post("/logout")
def auth_logout(request: Request, response: Response):
    revoke_session(session_token_from_request(request))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}
