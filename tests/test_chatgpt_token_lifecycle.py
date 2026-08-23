from __future__ import annotations

from types import SimpleNamespace


def test_normalize_oauth_token_response_accepts_camel_case_and_nested_tokens():
    from platforms.chatgpt.oauth import normalize_oauth_token_response

    result = normalize_oauth_token_response(
        {
            "data": {
                "accessToken": "access-value",
                "refreshToken": "refresh-value",
                "idToken": "id-value",
                "expiresIn": 3600,
            }
        }
    )

    assert result == {
        "access_token": "access-value",
        "refresh_token": "refresh-value",
        "id_token": "id-value",
        "expires_in": 3600,
        "token_type": "",
    }


def test_find_callback_url_accepts_nested_json_response():
    from platforms.chatgpt.protocol_phone import _find_callback_url

    callback = "http://localhost:1455/auth/callback?code=code-value&state=state-value"
    payload = {
        "data": {
            "steps": [
                {"next": {"redirect": callback}},
            ]
        }
    }

    assert _find_callback_url(payload) == callback


def test_refresh_account_prefers_oauth_refresh_token(monkeypatch):
    from platforms.chatgpt.token_refresh import TokenRefreshManager, TokenRefreshResult

    manager = TokenRefreshManager()
    calls = []

    def oauth(refresh_token, client_id=None):
        calls.append("oauth")
        assert refresh_token == "codex-refresh"
        return TokenRefreshResult(
            success=True,
            access_token="new-access",
            refresh_token="rotated-refresh",
        )

    def session(*args, **kwargs):
        calls.append("session")
        raise AssertionError("session must be fallback when Codex RT exists")

    monkeypatch.setattr(manager, "refresh_by_oauth_token", oauth)
    monkeypatch.setattr(manager, "refresh_by_session_token", session)

    account = SimpleNamespace(
        email="user@example.com",
        refresh_token="codex-refresh",
        session_token="web-session",
        client_id="codex-client",
    )
    result = manager.refresh_account(account)

    assert result.success is True
    assert result.refresh_token == "rotated-refresh"
    assert calls == ["oauth"]


def test_session_refresh_preserves_existing_refresh_token(monkeypatch):
    from platforms.chatgpt.token_refresh import TokenRefreshManager

    class Response:
        status_code = 200

        def json(self):
            return {
                "accessToken": "session-access",
                "expires": "2030-01-01T00:00:00.000Z",
            }

    class Session:
        class Cookies:
            def set(self, *args, **kwargs):
                return None

        cookies = Cookies()

        def get(self, *args, **kwargs):
            return Response()

    manager = TokenRefreshManager()
    monkeypatch.setattr(manager, "_create_session", lambda: Session())

    result = manager.refresh_by_session_token(
        "web-session",
        existing_refresh_token="codex-refresh",
    )

    assert result.success is True
    assert result.access_token == "session-access"
    assert result.refresh_token == "codex-refresh"


def test_session_refresh_retries_cloudflare_challenge(monkeypatch):
    from platforms.chatgpt.token_refresh import TokenRefreshManager

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = (
                {
                    "server": "cloudflare",
                    "content-type": "text/html; charset=UTF-8",
                    "cf-mitigated": "challenge",
                }
                if status_code == 403
                else {"content-type": "application/json"}
            )

        def json(self):
            return {"accessToken": "session-access"}

    class Session:
        class Cookies:
            def set(self, *args, **kwargs):
                return None

        cookies = Cookies()

        def __init__(self, response):
            self.response = response

        def get(self, *args, **kwargs):
            return self.response

    responses = iter((Response(403), Response(200)))
    manager = TokenRefreshManager()
    monkeypatch.setattr(manager, "_create_session", lambda: Session(next(responses)))
    monkeypatch.setattr("platforms.chatgpt.token_refresh.time.sleep", lambda _seconds: None)

    result = manager.refresh_by_session_token("web-session")

    assert result.success is True
    assert result.access_token == "session-access"
