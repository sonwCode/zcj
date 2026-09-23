from __future__ import annotations

import base64
import hashlib
import hmac
import os
import json
import secrets
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status

from customer_portal_api.app.config import settings
from customer_portal_api.app.db import utcnow


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt, expected = password_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(iterations_raw),
    )
    return hmac.compare_digest(digest.hex(), expected)


def create_access_token(subject: str, claims: dict[str, Any]) -> str:
    now = int(utcnow().timestamp())
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + settings.access_token_ttl_seconds,
        **claims,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _b64url_encode(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    encoded_payload = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")
    signature = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的 access token") from exc
    signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")
    expected_signature = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual_signature = _b64url_decode(encoded_signature)
    if not hmac.compare_digest(expected_signature, actual_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="access token 签名无效")
    payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
    if int(payload.get("exp", 0) or 0) <= int(utcnow().timestamp()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="access token 已过期")
    return payload


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_expiry():
    return utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds)




_PAYMENT_UNSIGNED_FLAG = "PORTAL_PAYMENT_ALLOW_UNSIGNED_CALLBACKS"
_PAYMENT_SECRET_MAP = "PORTAL_PAYMENT_CALLBACK_SECRETS"


def payment_callback_secret(channel_code: str) -> str:
    """Resolve the shared secret for one payment channel.

    Lookup order: PORTAL_PAYMENT_SECRET_<CHANNEL>, then the
    PORTAL_PAYMENT_CALLBACK_SECRETS map ("channel:secret,channel2:secret2").
    """
    code = str(channel_code or "").strip().lower()
    if not code:
        return ""
    direct = os.getenv("PORTAL_PAYMENT_SECRET_" + code.upper().replace("-", "_"), "").strip()
    if direct:
        return direct
    for pair in os.getenv(_PAYMENT_SECRET_MAP, "").split(","):
        name, _, secret = pair.partition(":")
        if name.strip().lower() == code and secret.strip():
            return secret.strip()
    return ""


def verify_payment_callback(channel_code: str, raw_body: bytes, signature: str) -> None:
    """Reject unsigned or badly signed payment callbacks.

    Without this, anyone who can guess an order number could POST a fake
    "success" callback and activate a subscription for free.
    """
    secret = payment_callback_secret(channel_code)
    if not secret:
        if os.getenv(_PAYMENT_UNSIGNED_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}:
            print(
                "[portal][WARN] 支付渠道 " + str(channel_code) +
                " 未配置签名密钥，因 " + _PAYMENT_UNSIGNED_FLAG + " 已显式开启而跳过校验"
            )
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="支付渠道未配置回调签名密钥，已拒绝该回调",
        )

    provided = str(signature or "").strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="支付回调签名校验失败",
        )
