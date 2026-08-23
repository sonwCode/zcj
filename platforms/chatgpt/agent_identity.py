"""Generate Codex Agent Identity credentials from an existing ChatGPT session."""

from __future__ import annotations

import base64
import json
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Any

from curl_cffi import requests as curl_requests
from nacl.bindings import crypto_box_seal_open
from nacl.bindings import crypto_sign_ed25519_pk_to_curve25519
from nacl.bindings import crypto_sign_ed25519_sk_to_curve25519
from nacl.bindings import crypto_sign_seed_keypair
from nacl.signing import SigningKey


DEFAULT_AUTH_API_BASE_URL = "https://auth.openai.com/api/accounts"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
AGENT_IDENTITY_IMPERSONATE = "chrome124"
AGENT_IDENTITY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
ED25519_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class AgentIdentityError(RuntimeError):
    pass


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3 or not all(parts):
        raise AgentIdentityError("OAuth token 不是有效的三段式 JWT")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AgentIdentityError("OAuth token payload 无效") from exc
    if not isinstance(value, dict):
        raise AgentIdentityError("OAuth token payload 必须是对象")
    return value


def token_identity(token: str) -> dict[str, Any]:
    claims = decode_jwt_payload(token)
    auth = claims.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        raise AgentIdentityError("OAuth token 缺少 OpenAI account claims")
    account_id = str(auth.get("chatgpt_account_id") or "").strip()
    user_id = str(
        auth.get("chatgpt_user_id")
        or auth.get("chatgpt_account_user_id")
        or auth.get("user_id")
        or ""
    ).strip()
    if not account_id or not user_id:
        raise AgentIdentityError("OAuth token 缺少 Agent Identity 所需账户 claims")
    profile = claims.get("https://api.openai.com/profile")
    email = claims.get("email")
    if not isinstance(email, str) and isinstance(profile, dict):
        email = profile.get("email")
    return {
        "account_id": account_id,
        "chatgpt_user_id": user_id,
        "email": email if isinstance(email, str) and email.strip() else None,
        "plan_type": str(auth.get("chatgpt_plan_type") or "unknown"),
        "chatgpt_account_is_fedramp": bool(auth.get("chatgpt_account_is_fedramp", False)),
    }


def has_identity_claims(token: str) -> bool:
    try:
        token_identity(token)
        return True
    except AgentIdentityError:
        return False


def _ssh_ed25519_public_key(signing_key: SigningKey) -> str:
    algorithm = b"ssh-ed25519"
    public_key = signing_key.verify_key.encode()
    blob = (
        len(algorithm).to_bytes(4, "big")
        + algorithm
        + len(public_key).to_bytes(4, "big")
        + public_key
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def _task_signature(signing_key: SigningKey, runtime_id: str, timestamp: str) -> str:
    payload = f"{runtime_id}:{timestamp}".encode("utf-8")
    return base64.b64encode(signing_key.sign(payload).signature).decode("ascii")


def _decrypt_task_id(signing_key: SigningKey, encrypted_task_id: str) -> str:
    ed_public_key, ed_secret_key = crypto_sign_seed_keypair(signing_key.encode())
    curve_public_key = crypto_sign_ed25519_pk_to_curve25519(ed_public_key)
    curve_secret_key = crypto_sign_ed25519_sk_to_curve25519(ed_secret_key)
    try:
        ciphertext = base64.b64decode(encrypted_task_id, validate=True)
        task_id = crypto_box_seal_open(
            ciphertext,
            curve_public_key,
            curve_secret_key,
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentIdentityError("无法解密 Agent Task 注册响应") from exc
    if not task_id:
        raise AgentIdentityError("Agent Task ID 为空")
    return task_id


def _request_json(
    session: Any,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    attempts: int = 5,
) -> dict[str, Any]:
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(
                method,
                url,
                headers={"Connection": "close", **(headers or {})},
                json=body,
                timeout=30,
            )
        except Exception as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 12))
                continue
            raise AgentIdentityError(f"Agent Identity 网络请求失败: {last_error}") from exc
        if 200 <= int(response.status_code) < 300:
            try:
                value = response.json()
            except Exception as exc:
                raise AgentIdentityError("Agent Identity 接口返回了无效 JSON") from exc
            if not isinstance(value, dict):
                raise AgentIdentityError("Agent Identity 接口返回值不是对象")
            return value
        detail = str(response.text or "")[:800].strip()
        last_error = f"HTTP {response.status_code}: {detail}"
        if int(response.status_code) in RETRYABLE_STATUSES and attempt < attempts:
            time.sleep(min(2 ** attempt, 12))
            continue
        raise AgentIdentityError(f"Agent Identity 请求失败: {last_error}")
    raise AgentIdentityError(f"Agent Identity 请求失败: {last_error or 'unknown error'}")


def register_identity(
    tokens: dict[str, Any],
    *,
    auth_api_base_url: str = DEFAULT_AUTH_API_BASE_URL,
    codex_base_url: str = DEFAULT_CODEX_BASE_URL,
    proxy_url: str | None = None,
    proxy_region: str = "US",
) -> dict[str, Any]:
    access_token = str(tokens.get("access_token") or "").strip()
    identity_token = str(tokens.get("id_token") or access_token).strip()
    if not access_token:
        raise AgentIdentityError("账号缺少 access_token")
    identity = token_identity(identity_token)
    if proxy_url and proxy_region:
        from core.proxy_utils import pin_711proxy_session

        proxy_url = pin_711proxy_session(
            proxy_url,
            region=str(proxy_region).strip().upper(),
            session_id=f"agent{secrets.token_hex(3)}",
            session_minutes=30,
        )
    signing_key = SigningKey.generate()
    session = curl_requests.Session(impersonate=AGENT_IDENTITY_IMPERSONATE)
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": AGENT_IDENTITY_USER_AGENT,
        }
    )
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    else:
        session.proxies = {"http": "", "https": ""}
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        if identity["chatgpt_account_is_fedramp"]:
            headers["X-OpenAI-Fedramp"] = "true"
        registration = _request_json(
            session,
            "POST",
            f"{auth_api_base_url.rstrip('/')}/v1/agent/register",
            headers=headers,
            body={
                "abom": {
                    "agent_version": "account-manager-agent-identity-1",
                    "agent_harness_id": "codex-cli",
                    "running_location": f"custom-{sys.platform}",
                },
                "agent_public_key": _ssh_ed25519_public_key(signing_key),
                "capabilities": ["responsesapi"],
                "ttl": None,
            },
        )
        runtime_id = str(registration.get("agent_runtime_id") or "").strip()
        if not runtime_id:
            raise AgentIdentityError("Agent 注册响应缺少 agent_runtime_id")
        timestamp = _utc_timestamp()
        task = _request_json(
            session,
            "POST",
            f"{auth_api_base_url.rstrip('/')}/v1/agent/{runtime_id}/task/register",
            headers=headers,
            body={
                "timestamp": timestamp,
                "signature": _task_signature(signing_key, runtime_id, timestamp),
            },
        )
    finally:
        session.close()

    task_id = str(task.get("task_id") or task.get("taskId") or "").strip()
    if not task_id:
        encrypted = str(
            task.get("encrypted_task_id") or task.get("encryptedTaskId") or ""
        ).strip()
        if not encrypted:
            raise AgentIdentityError("Agent Task 注册响应缺少 task_id")
        task_id = _decrypt_task_id(signing_key, encrypted)
    return {
        "version": 1,
        "credential_type": "codex_agent_identity",
        "capabilities": ["responsesapi"],
        "created_at": _utc_timestamp(),
        "agent_runtime_id": runtime_id,
        "private_key_seed": base64.b64encode(signing_key.encode()).decode("ascii"),
        "task_id": task_id,
        **identity,
        "codex_base_url": codex_base_url.rstrip("/"),
        "auth_api_base_url": auth_api_base_url.rstrip("/"),
    }


def _agent_private_key(certificate: dict[str, Any]) -> str:
    seed = base64.b64decode(str(certificate.get("private_key_seed") or ""), validate=True)
    if len(seed) != 32:
        raise AgentIdentityError("Agent Identity 私钥种子长度无效")
    return base64.b64encode(ED25519_PKCS8_PREFIX + seed).decode("ascii")


def certificate_to_sub2api_export(certificate: dict[str, Any]) -> dict[str, Any]:
    account_id = str(certificate.get("account_id") or "").strip()
    email = certificate.get("email")
    name = str(email or certificate.get("agent_runtime_id") or account_id)
    identity = {
        "agent_runtime_id": certificate["agent_runtime_id"],
        "agent_private_key": _agent_private_key(certificate),
        "account_id": account_id,
        "chatgpt_user_id": certificate["chatgpt_user_id"],
        "email": email,
        "plan_type": certificate.get("plan_type") or "unknown",
        "chatgpt_account_is_fedramp": bool(
            certificate.get("chatgpt_account_is_fedramp", False)
        ),
        "task_id": certificate["task_id"],
    }
    credentials = {
        "auth_mode": "agentIdentity",
        **identity,
        "chatgpt_account_id": account_id,
        "workspace_id": account_id,
    }
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": _utc_timestamp(),
        "proxies": [],
        "accounts": [
            {
                "name": name,
                "platform": "openai",
                "type": "oauth",
                "credentials": credentials,
                "extra": {
                    "email": email,
                    "name": name,
                    "source": "chatgpt_web_session",
                    "account_id": account_id,
                    "chatgpt_account_id": account_id,
                    "workspace_id": account_id,
                },
                "concurrency": 10,
                "priority": 1,
                "rate_multiplier": 1,
                "auto_pause_on_expired": True,
            }
        ],
        "auth_mode": "agentIdentity",
        "OPENAI_API_KEY": None,
        "agent_identity": identity,
    }
