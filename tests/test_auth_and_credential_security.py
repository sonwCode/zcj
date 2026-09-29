from __future__ import annotations

from starlette.requests import Request

from core.auth import (
    SESSION_COOKIE_NAME,
    issue_session,
    request_is_authenticated,
    revoke_session,
    validate_session,
)
from core.vault import MASKED_SECRET
from application.provider_settings import ProviderSettingsService
from infrastructure.provider_settings_repository import _merge_auth


def _request(*, authorization: str = "", cookie: str = "") -> Request:
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    if cookie:
        headers.append((b"cookie", f"{SESSION_COOKIE_NAME}={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/accounts",
            "headers": headers,
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def test_app_password_is_not_a_bearer_session(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "app-password")
    assert request_is_authenticated(_request(authorization="Bearer app-password")) is False

    token, ttl = issue_session()
    assert ttl > 0
    assert validate_session(token) is True
    assert request_is_authenticated(_request(cookie=token)) is True
    revoke_session(token)
    assert validate_session(token) is False


def test_provider_serialization_masks_auth_values():
    class Item:
        id = 7
        provider_type = "sms"
        provider_key = "fixture"
        display_name = "Fixture"
        auth_mode = "api_key"
        enabled = True
        is_default = False

        def get_auth(self):
            return {"api_key": "raw-api-key", "pool_text": "account----token", "empty": ""}

        def get_config(self):
            return {"country": "US"}

        def get_metadata(self):
            return {}

    class Definition:
        label = "Fixture"
        description = ""
        driver_type = "fixture"
        is_builtin = False
        category = ""

        def get_auth_modes(self):
            return ["api_key"]

        def get_fields(self):
            return [
                {"key": "api_key", "secret": True, "category": "auth"},
                {"key": "pool_text", "category": "auth"},
            ]

    class Definitions:
        def get_by_key(self, provider_type, provider_key):
            return Definition()

    service = ProviderSettingsService(repository=object())
    service.definitions = Definitions()
    result = service._serialize(Item())

    assert "api_key" not in result["auth"]
    assert result["auth_preview"]["api_key"] == MASKED_SECRET
    assert result["auth"]["pool_text"] == "account----token"
    assert "raw-api-key" not in str(result)
    assert result["auth"]["empty"] == ""


def test_provider_auth_merge_clears_non_secret_fields_and_preserves_secret_placeholders():
    merged = _merge_auth(
        {"pool_text": "old-account----token", "api_key": "old-api-key"},
        {"pool_text": "", "api_key": MASKED_SECRET},
        known_keys={"pool_text", "api_key"},
        secret_keys={"api_key"},
    )

    assert merged["pool_text"] == ""
    assert merged["api_key"] == "old-api-key"
