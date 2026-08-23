from __future__ import annotations

import base64
import json

from application.account_exports import AccountExportsService
from core.base_platform import Account
from core.db import save_account
from domain.accounts import AccountExportSelection
from infrastructure.accounts_repository import AccountsRepository
from platforms.chatgpt.agent_identity import certificate_to_sub2api_export
from platforms.chatgpt.agent_identity import register_identity


def _jwt(payload: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def _certificate(seed: bytes = b"z" * 32) -> dict:
    return {
        "private_key_seed": base64.b64encode(seed).decode("ascii"),
        "agent_runtime_id": "agent-test",
        "task_id": "task-test",
        "account_id": "account-test",
        "chatgpt_user_id": "user-test",
        "email": "identity@test.com",
        "plan_type": "free",
        "chatgpt_account_is_fedramp": False,
    }


def test_certificate_exports_sub2api_agent_identity_shape():
    payload = certificate_to_sub2api_export(_certificate())

    assert payload["auth_mode"] == "agentIdentity"
    assert payload["OPENAI_API_KEY"] is None
    assert payload["agent_identity"]["agent_runtime_id"] == "agent-test"
    assert payload["agent_identity"]["agent_private_key"]
    assert payload["accounts"][0]["credentials"]["auth_mode"] == "agentIdentity"
    assert payload["accounts"][0]["credentials"]["task_id"] == "task-test"


def test_runtime_and_task_registration_use_the_access_token(monkeypatch):
    id_token = _jwt(
        {
            "email": "identity@test.com",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-test",
                "chatgpt_user_id": "user-test",
                "chatgpt_plan_type": "free",
            },
        }
    )
    calls = []
    session_kwargs = []
    sessions = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            session_kwargs.append(kwargs)
            sessions.append(self)
            self.headers = {}
            self.proxies = {}
            self.trust_env = True

        def close(self):
            return None

    def fake_request_json(session, method, url, **kwargs):
        calls.append((method, url, kwargs, dict(session.proxies)))
        if url.endswith("/v1/agent/register"):
            return {"agent_runtime_id": "agent-test"}
        return {"task_id": "task-test"}

    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.curl_requests.Session",
        FakeSession,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity._request_json",
        fake_request_json,
    )

    certificate = register_identity(
        {"access_token": "access-test", "id_token": id_token},
        proxy_url=(
            "http://USER-zone-custom-region-CO-session-old-sessTime-180:secret"
            "@global.rotgb.711proxy.com:10000"
        ),
    )

    assert certificate["task_id"] == "task-test"
    assert len(calls) == 2
    assert calls[0][2]["headers"]["Authorization"] == "Bearer access-test"
    assert calls[1][2]["headers"]["Authorization"] == "Bearer access-test"
    assert "region-US" in calls[0][3]["https"]
    assert "region-CO" not in calls[0][3]["https"]
    assert session_kwargs == [{"impersonate": "chrome124"}]
    assert sessions[0].headers["Accept"] == "application/json"
    assert "Chrome/124.0.0.0" in sessions[0].headers["User-Agent"]


def test_account_export_registers_identity_from_stored_tokens(monkeypatch):
    id_token = _jwt(
        {
            "email": "identity@test.com",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-test",
                "chatgpt_user_id": "user-test",
                "chatgpt_plan_type": "free",
            },
        }
    )
    access_token = _jwt({"exp": 4_000_000_000})
    save_account(
        Account(
            platform="chatgpt",
            email="identity@test.com",
            password="TestPass123!",
            user_id="account-test",
            extra={"access_token": access_token, "id_token": id_token},
        )
    )
    captured = {}

    def fake_register(tokens, **kwargs):
        captured["tokens"] = tokens
        captured["kwargs"] = kwargs
        return _certificate()

    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.register_identity",
        fake_register,
    )

    artifact = AccountExportsService(AccountsRepository()).export_chatgpt_agent_identity_sub2api(
        AccountExportSelection(platform="chatgpt", select_all=True)
    )
    payload = json.loads(artifact.content)

    assert captured["tokens"] == {
        "access_token": access_token,
        "id_token": id_token,
    }
    assert payload["auth_mode"] == "agentIdentity"
    assert payload["agent_identity"]["account_id"] == "account-test"
    assert artifact.account_ids
