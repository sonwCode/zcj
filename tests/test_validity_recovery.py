from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from sqlmodel import Session, select

from application.tasks import _run_single_account_check
from core.account_graph import load_account_graphs, patch_account_graph
from core.base_platform import RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.lifecycle import check_accounts_validity
from core.proxy_pool import proxy_pool
from platforms.chatgpt import payment
from platforms.chatgpt.plugin import ChatGPTPlatform


def _jwt(payload: dict) -> str:
    def enc(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}."


class _AlwaysValidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return True


class _AlwaysInvalidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False


class _UnknownPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False

    def get_last_check_overview(self) -> dict:
        return {
            "validity_status": "unknown",
            "validity_reason": "temporary network failure",
            "check_error": "curl (7)",
        }


class _RecoveredCredentialPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return True

    def get_last_check_overview(self) -> dict:
        return {
            "validity_status": "valid",
            "validity_reason": "session refresh recovered the account",
            "plan_state": "free",
        }

    def get_last_check_credential_updates(self) -> dict:
        return {"web_access_token": "fresh-web-access"}


class _RemoteAuthResponse:
    status_code = 401
    url = "https://chatgpt.com/backend-api/me"
    headers = {"x-request-id": "req-test", "cf-ray": "ray-test"}

    def __init__(self, code: str = "token_invalidated"):
        self.code = code

    def json(self):
        return {
            "error": {
                "code": self.code,
                "type": "invalid_request_error",
                "message": "authentication token invalidated",
            }
        }


class _RemoteAuthError(RuntimeError):
    def __init__(self, code: str = "token_invalidated"):
        super().__init__("HTTP Error 401")
        self.response = _RemoteAuthResponse(code)


class _CloudflareChallengeResponse:
    status_code = 403
    url = "https://chatgpt.com/backend-api/me"
    headers = {
        "server": "cloudflare",
        "content-type": "text/html; charset=UTF-8",
        "cf-mitigated": "challenge",
        "cf-ray": "ray-edge",
    }

    def json(self):
        raise ValueError("HTML challenge")


class _CloudflareChallengeError(RuntimeError):
    def __init__(self):
        super().__init__("HTTP Error 403")
        self.response = _CloudflareChallengeResponse()


def _create_account(*, platform: str = "chatgpt", lifecycle_status: str = "registered") -> int:
    with Session(engine) as session:
        model = AccountModel(platform=platform, email=f"{platform}@example.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates={"valid": lifecycle_status != "invalid"},
        )
        session.commit()
        return int(model.id or 0)


def _overview(account_id: int):
    with Session(engine) as session:
        return session.exec(
            select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)
        ).one()


def test_single_account_check_recovers_previously_invalid_account(monkeypatch):
    account_id = _create_account(lifecycle_status="invalid")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _AlwaysValidPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is True
    assert result["valid"] is True
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "valid"
    assert overview.display_status == "registered"
    assert overview.checked_at


def test_lifecycle_validity_check_does_not_overwrite_lifecycle_status(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _AlwaysInvalidPlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["invalid"] == 1
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "invalid"
    assert overview.display_status == "invalid"
    assert overview.checked_at
    assert overview.get_summary()["invalid_detected_at"]


def test_scheduled_validity_check_recovers_retained_invalid_account(monkeypatch):
    account_id = _create_account(lifecycle_status="invalid")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _AlwaysValidPlatform)

    results = check_accounts_validity(
        platform="chatgpt",
        limit=10,
        include_inactive=True,
    )

    assert results["valid"] == 1
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "valid"
    assert overview.display_status == "registered"


def test_single_account_check_persists_recovered_web_token(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _RecoveredCredentialPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is True
    assert result["validity_status"] == "valid"
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
    credentials = {item["key"]: item["value"] for item in graph["credentials"]}
    assert credentials["web_access_token"] == "fresh-web-access"


def test_lifecycle_validity_check_keeps_transient_failure_unknown(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _UnknownPlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["unknown"] == 1
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "unknown"
    assert overview.display_status == "registered"
    assert overview.get_summary()["check_error"] == "curl (7)"


def test_legacy_invalid_account_preserves_first_known_check_time():
    account_id = _create_account(lifecycle_status="registered")
    first_checked_at = "2026-07-20T10:00:00+00:00"
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            model,
            summary_updates={
                "checked_at": first_checked_at,
                "valid": False,
                "validity_status": "invalid",
            },
        )
        session.commit()

        overview = session.get(AccountOverviewModel, account_id)
        legacy_summary = overview.get_summary()
        legacy_summary.pop("invalid_detected_at", None)
        overview.set_summary(legacy_summary)
        session.add(overview)
        session.commit()

        patch_account_graph(
            session,
            model,
            summary_updates={
                "checked_at": "2026-07-22T10:00:00+00:00",
                "valid": False,
                "validity_status": "invalid",
            },
        )
        session.commit()

    assert _overview(account_id).get_summary()["invalid_detected_at"] == "2026-07-20T10:00:00Z"


def test_chatgpt_subscription_status_falls_back_to_wham_usage(monkeypatch):
    captured_headers: dict[str, str] = {}

    class _Resp:
        def __init__(self, data=None, error: Exception | None = None):
            self._data = data
            self._error = error

        def raise_for_status(self):
            if self._error:
                raise self._error

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(error=RuntimeError("403"))
        captured_headers.update(kwargs.get("headers") or {})
        return _Resp(data={"plan_type": "free"})

    monkeypatch.setattr(payment.cffi_requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": json.dumps({"chatgpt_account_id": "acct-123"}),
            "extra": {},
        },
    )()

    status = payment.check_subscription_status(account)

    assert status == "free"
    assert captured_headers["Authorization"] == "Bearer token"
    assert captured_headers["Chatgpt-Account-Id"] == "acct-123"


def test_chatgpt_usage_extracts_account_id_from_access_token(monkeypatch):
    captured_headers: dict[str, str] = {}
    access_token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "workspace-account",
                "chatgpt_plan_type": "workspace",
            }
        }
    )

    class _Resp:
        def __init__(self, data=None, error: Exception | None = None):
            self._data = data
            self._error = error

        def raise_for_status(self):
            if self._error:
                raise self._error

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(error=RuntimeError("403"))
        captured_headers.update(kwargs.get("headers") or {})
        return _Resp(data={"plan_type": "workspace"})

    monkeypatch.setattr(payment.cffi_requests, "get", _fake_get)
    account = type("AccountStub", (), {"access_token": access_token, "cookies": "", "extra": {}})()

    status = payment.check_subscription_status(account)

    assert status == "team"
    assert captured_headers["Chatgpt-Account-Id"] == "workspace-account"


def test_chatgpt_check_valid_uses_proxy_pool_before_direct(monkeypatch):
    calls: list[str | None] = []
    proxy_events: list[tuple[str, str]] = []

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        if proxy != "http://127.0.0.1:7890":
            raise RuntimeError("should use proxy first")
        return {
            "status": "free",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "free"},
        }

    monkeypatch.setattr(payment, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {
                "access_token": "token",
                "id_token": "",
                "cookies": "",
            },
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert plugin.get_last_check_overview()["chatgpt_usage"] == {"plan_type": "free"}


def test_chatgpt_check_valid_reuses_stored_registration_proxy_only(monkeypatch):
    calls: list[str | None] = []
    pool_calls = []
    auth_proxy = "http://user:secret@global.rotgb.711proxy.com:10000"

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        return {"status": "free", "source": "backend-api/wham/usage"}

    monkeypatch.setattr(payment, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(
        proxy_pool,
        "get_next",
        lambda region="": pool_calls.append(region) or "http://pool.example:8080",
    )

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {"access_token": "token", "auth_proxy_url": auth_proxy},
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == [auth_proxy]
    assert pool_calls == []


def test_chatgpt_check_valid_classifies_network_failure_as_unknown(monkeypatch):
    monkeypatch.setattr(
        payment,
        "fetch_subscription_status_details",
        lambda account, proxy=None: (_ for _ in ()).throw(RuntimeError("curl (7) Failed to connect")),
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type("AccountStub", (), {"token": "token", "region": "", "extra": {}})()

    assert plugin.check_valid(account) is False
    overview = plugin.get_last_check_overview()
    assert overview["validity_status"] == "unknown"
    assert "curl (7)" in overview["check_error"]


def test_chatgpt_check_valid_classifies_http_403_as_invalid(monkeypatch):
    monkeypatch.setattr(
        payment,
        "fetch_subscription_status_details",
        lambda account, proxy=None: (_ for _ in ()).throw(RuntimeError("HTTP 403 account deactivated")),
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type("AccountStub", (), {"token": "token", "region": "", "extra": {}})()

    assert plugin.check_valid(account) is False
    overview = plugin.get_last_check_overview()
    assert overview["validity_status"] == "invalid"
    assert overview["validity_http_status"] == 403


def test_chatgpt_check_valid_classifies_cloudflare_403_as_unknown(monkeypatch):
    monkeypatch.setattr(
        payment,
        "fetch_subscription_status_details",
        lambda account, proxy=None: (_ for _ in ()).throw(_CloudflareChallengeError()),
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type("AccountStub", (), {"token": "token", "region": "", "extra": {}})()

    assert plugin.check_valid(account) is False
    overview = plugin.get_last_check_overview()
    assert overview["validity_status"] == "unknown"
    assert overview["validity_http_status"] == 403
    assert overview["validity_error_code"] == "cloudflare_challenge"


def test_chatgpt_check_valid_refreshes_web_session_before_marking_invalid(monkeypatch):
    calls: list[str] = []

    def _fake_status(account, proxy=None):
        calls.append(account.access_token)
        if account.access_token == "fresh-web-access":
            return {"status": "free", "source": "backend-api/me"}
        raise _RemoteAuthError("token_revoked")

    monkeypatch.setattr(payment, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(
        "platforms.chatgpt.token_refresh.TokenRefreshManager.refresh_by_session_token",
        lambda self, session_token, existing_refresh_token="": SimpleNamespace(
            success=True,
            access_token="fresh-web-access",
            refresh_token=existing_refresh_token,
            error_message="",
        ),
    )

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type(
        "AccountStub",
        (),
        {
            "token": "stale-access",
            "region": "",
            "extra": {
                "web_access_token": "stale-access",
                "access_token": "stale-access",
                "session_token": "web-session",
                "refresh_token": "codex-refresh",
            },
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == ["stale-access", "fresh-web-access"]
    overview = plugin.get_last_check_overview()
    assert overview["validity_status"] == "valid"
    assert overview["auth_recovery_attempted"] is True
    assert overview["auth_recovery_succeeded"] is True
    assert overview["check_token_source"] == "session_refresh"
    assert plugin.get_last_check_credential_updates() == {
        "web_access_token": "fresh-web-access"
    }


def test_chatgpt_check_valid_keeps_sanitized_error_after_refresh_rejected(monkeypatch):
    monkeypatch.setattr(
        payment,
        "fetch_subscription_status_details",
        lambda account, proxy=None: (_ for _ in ()).throw(_RemoteAuthError()),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.token_refresh.TokenRefreshManager.refresh_by_session_token",
        lambda self, session_token, existing_refresh_token="": SimpleNamespace(
            success=True,
            access_token="still-rejected",
            refresh_token=existing_refresh_token,
            error_message="",
        ),
    )

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type(
        "AccountStub",
        (),
        {
            "token": "stale-access",
            "region": "",
            "extra": {
                "web_access_token": "stale-access",
                "session_token": "web-session",
            },
        },
    )()

    assert plugin.check_valid(account) is False
    overview = plugin.get_last_check_overview()
    assert overview["validity_status"] == "invalid"
    assert overview["validity_error_code"] == "token_invalidated"
    assert overview["validity_request_id"] == "req-test"
    assert overview["validity_cf_ray"] == "ray-test"
    assert overview["auth_recovery_attempted"] is True
    assert "需要重新登录" in overview["validity_reason"]


def test_chatgpt_check_valid_keeps_unknown_when_refreshed_probe_has_network_error(monkeypatch):
    def _fake_status(account, proxy=None):
        if account.access_token == "fresh-web-access":
            raise RuntimeError("curl (7) Failed to connect")
        raise _RemoteAuthError("token_revoked")

    monkeypatch.setattr(payment, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(
        "platforms.chatgpt.token_refresh.TokenRefreshManager.refresh_by_session_token",
        lambda self, session_token, existing_refresh_token="": SimpleNamespace(
            success=True,
            access_token="fresh-web-access",
            refresh_token=existing_refresh_token,
            error_message="",
        ),
    )

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type(
        "AccountStub",
        (),
        {
            "token": "stale-access",
            "region": "",
            "extra": {
                "web_access_token": "stale-access",
                "session_token": "web-session",
            },
        },
    )()

    assert plugin.check_valid(account) is False
    overview = plugin.get_last_check_overview()
    assert overview["validity_status"] == "unknown"
    assert overview["auth_recovery_attempted"] is True
    assert overview["auth_recovery_succeeded"] is True
    assert "curl (7)" in overview["check_error"]


def test_chatgpt_check_valid_keeps_unknown_when_session_refresh_route_is_blocked(monkeypatch):
    monkeypatch.setattr(
        payment,
        "fetch_subscription_status_details",
        lambda account, proxy=None: (_ for _ in ()).throw(_RemoteAuthError("token_revoked")),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.token_refresh.TokenRefreshManager.refresh_by_session_token",
        lambda self, session_token, existing_refresh_token="": SimpleNamespace(
            success=False,
            access_token="",
            refresh_token=existing_refresh_token,
            error_message="Session token 刷新失败: HTTP 403",
        ),
    )

    plugin = ChatGPTPlatform(config=RegisterConfig())
    account = type(
        "AccountStub",
        (),
        {
            "token": "stale-access",
            "region": "",
            "extra": {
                "web_access_token": "stale-access",
                "session_token": "web-session",
                "auth_proxy_url": "http://proxy.example:8080",
            },
        },
    )()

    assert plugin.check_valid(account) is False
    overview = plugin.get_last_check_overview()
    assert overview["validity_status"] == "unknown"
    assert overview["auth_recovery_attempted"] is True
    assert overview["auth_recovery_succeeded"] is False
    assert overview["auth_recovery_error"] == "Session token 刷新失败: HTTP 403"
