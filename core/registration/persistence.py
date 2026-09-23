"""Explicit persistence boundary for registration results.

Historically the executor wrote an account row as soon as the platform
registration call returned.  That made "registered" ambiguous: a run that
reached the account-creation screen but never obtained a usable credential
could still be persisted.

This module defines the single predicate that must pass before an account is
written.  The boundary is deliberately narrow and observable:

    BOUNDARY = "stable-http-200-at-v1"

An account is persisted only when the result carries

  * a non-empty identity (email or user id),
  * an explicit success status from the platform layer, and
  * a bearer access token of at least MIN_ACCESS_TOKEN_LENGTH characters.

The token requirement is the operational meaning of "stable HTTP 200 AT": the
access token only exists after a 2xx credential exchange, so requiring it is a
stronger and less ambiguous gate than the old truthiness check.  Pass
require_secret=True to additionally demand a password or refresh token.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BOUNDARY_VERSION = "stable-http-200-at-v1"
MIN_ACCESS_TOKEN_LENGTH = 20

_TOKEN_KEYS = ("token", "access_token", "accessToken", "at", "bearer_token")
_SECRET_KEYS = ("password", "secret", "refresh_token", "refreshToken")
_IDENTITY_KEYS = ("email", "user_id", "userId", "account_id")
_SUCCESS_STATUSES = {"success", "registered", "active", "ok", "valid", "created"}


@dataclass(frozen=True)
class PersistenceDecision:
    ok: bool
    code: str
    detail: str = ""
    boundary: str = BOUNDARY_VERSION

    def log_fields(self) -> dict[str, str]:
        return {
            "persistence_boundary": self.boundary,
            "persistence_code": self.code,
        }


def _lookup(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        if result.get(key) not in (None, ""):
            return result.get(key)
    else:
        candidate = getattr(result, key, None)
        if candidate not in (None, ""):
            return candidate
    if isinstance(result, dict):
        extra = result.get("extra")
    else:
        extra = getattr(result, "extra", None)
    if isinstance(extra, dict) and extra.get(key) not in (None, ""):
        return extra.get(key)
    return None


def _text(result: Any, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _lookup(result, key)
        if value:
            return str(value).strip()
    return ""


def _status_ok(result: Any) -> bool:
    status = _lookup(result, "status")
    if status is None:
        return True
    name = str(getattr(status, "value", status) or "").strip().lower()
    if not name:
        return True
    return name in _SUCCESS_STATUSES


def evaluate_persistence(result: Any, *, require_secret: bool = False) -> PersistenceDecision:
    """Return whether a registration result is safe to persist."""
    if result is None:
        return PersistenceDecision(False, "empty_result", "平台未返回注册结果")

    identity = _text(result, _IDENTITY_KEYS)
    if not identity:
        return PersistenceDecision(False, "missing_identity", "结果中缺少邮箱或用户标识")

    if not _status_ok(result):
        return PersistenceDecision(False, "status_not_success", "平台状态不是成功")

    token = _text(result, _TOKEN_KEYS)
    if len(token) < MIN_ACCESS_TOKEN_LENGTH:
        return PersistenceDecision(
            False,
            "missing_access_token",
            "未取得稳定的访问令牌（HTTP 200 AT），不入库",
        )

    if require_secret:
        secret = _text(result, _SECRET_KEYS)
        if not secret:
            return PersistenceDecision(False, "missing_secret", "结果中缺少密码或刷新令牌")

    return PersistenceDecision(True, "ok", "")


def boundary_summary() -> dict:
    return {
        "boundary": BOUNDARY_VERSION,
        "min_access_token_length": MIN_ACCESS_TOKEN_LENGTH,
        "identity_keys": list(_IDENTITY_KEYS),
        "token_keys": list(_TOKEN_KEYS),
        "secret_keys": list(_SECRET_KEYS),
    }