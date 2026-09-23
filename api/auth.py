from __future__ import annotations

import hmac
import os

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = ""


@router.get("/check")
def auth_check():
    """Return whether the app requires a password."""
    password = os.environ.get("APP_PASSWORD", "").strip()
    return {"required": bool(password)}


@router.post("/login")
def auth_login(body: LoginRequest):
    password = os.environ.get("APP_PASSWORD", "").strip()
    if not password:
        return {"ok": True}
    if hmac.compare_digest(body.password, password):
        return {"ok": True, "token": password}
    return {"ok": False, "error": "密码错误"}
