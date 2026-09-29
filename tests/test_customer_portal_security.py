"""Regression tests for the customer portal security fixes.

Covers: no shipped default JWT secret / admin password, and signed-only
payment callbacks (unsigned or tampered callbacks must not activate anything).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time

import pytest

_TMP_DIR = tempfile.mkdtemp(prefix="portal-test-")
os.environ["PORTAL_DATABASE_URL"] = "sqlite:///" + _TMP_DIR + "/portal.db"
os.environ.pop("PORTAL_JWT_SECRET", None)
os.environ.pop("PORTAL_ADMIN_PASSWORD", None)
os.environ.pop("PORTAL_CORS_ORIGINS", None)
os.environ["PORTAL_PAYMENT_SECRET_TESTCHAN"] = "test-channel-secret"

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from customer_portal_api.app.config import (  # noqa: E402
    resolve_seed_admin_password,
    settings,
)
from customer_portal_api.main import app  # noqa: E402

CHANNEL = "testchan"
CHANNEL_SECRET = "test-channel-secret"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _forge_jwt(secret: str, role: str = "admin") -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(
        json.dumps(
            {
                "sub": "1",
                "iat": now,
                "exp": now + 3600,
                "role_code": role,
                "username": "attacker",
            }
        ).encode()
    )
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def _sign(body: bytes, secret: str = CHANNEL_SECRET) -> dict[str, str]:
    return {"x-portal-signature": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_default_jwt_secret_is_never_used() -> None:
    assert settings.jwt_secret
    assert settings.jwt_secret != "change-me-in-production"


def test_missing_admin_password_fails_closed() -> None:
    password, generated = resolve_seed_admin_password()
    assert password == ""
    assert generated is False


def test_login_with_shipped_default_password_is_rejected(client) -> None:
    response = client.post("/api/auth/login", json={"account": "admin", "password": "admin123456"})
    assert response.status_code == 401


def test_jwt_forged_with_old_default_secret_is_rejected(client) -> None:
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer " + _forge_jwt("change-me-in-production")},
    )
    assert response.status_code == 401


def test_jwt_signature_alone_is_not_an_admin_session(client) -> None:
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer " + _forge_jwt(settings.jwt_secret)},
    )
    # A valid signature still needs a provisioned, active portal user.
    assert response.status_code == 401


def test_channel_without_secret_rejects_every_callback(client) -> None:
    body = json.dumps({"payment_no": "X", "status": "success"}).encode()
    assert client.post("/api/payment/callback/unconfigured", content=body).status_code == 403


def test_unsigned_callback_is_rejected(client) -> None:
    body = json.dumps({"payment_no": "X", "status": "success"}).encode()
    assert client.post(f"/api/payment/callback/{CHANNEL}", content=body).status_code == 401


def test_badly_signed_callback_is_rejected(client) -> None:
    body = json.dumps({"payment_no": "X", "status": "success"}).encode()
    response = client.post(
        f"/api/payment/callback/{CHANNEL}",
        content=body,
        headers={"x-portal-signature": "sha256=" + "deadbeef" * 8},
    )
    assert response.status_code == 401


def test_correctly_signed_callback_passes_verification(client) -> None:
    body = json.dumps({"payment_no": "X", "status": "success"}).encode()
    # 404 means the signature passed and the payment number simply does not exist.
    response = client.post(f"/api/payment/callback/{CHANNEL}", content=body, headers=_sign(body))
    assert response.status_code == 404


def test_cors_is_not_wildcard_by_default(client) -> None:
    assert settings.cors_origins == []
    response = client.get("/docs", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/app/platforms", None),
        ("GET", "/api/admin/users", None),
        ("GET", "/api/config", None),
        ("POST", "/api/app/tasks/register", {"platform": "chatgpt"}),
        ("POST", "/api/admin/users", {"username": "new", "password": "secret"}),
    ],
)
def test_protected_portal_surfaces_require_access_token(client, method, path, payload) -> None:
    response = client.request(method, path, json=payload)
    assert response.status_code == 401
