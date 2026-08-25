from __future__ import annotations

import json

from types import SimpleNamespace

from platforms.chatgpt.constants import OPENAI_PAGE_TYPES
from platforms.chatgpt.register import (
    RegistrationEngine,
    SentinelPayload,
    SignupFormResult,
    _extract_chatgpt_account_id,
    _extract_chatgpt_session_credentials,
)


class _JsonResponse:
    status_code = 200
    text = '{"page":{"type":"email_otp_verification"}}'

    def json(self):
        return {"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}


class _FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.posts.append((url, headers or {}, data))
        return _JsonResponse()


class _SendOtpResponse:
    status_code = 200
    text = '{"ok":true}'


class _EmailVerificationPageResponse:
    status_code = 200
    text = "<html>Email verification</html>"


def _bare_engine() -> RegistrationEngine:
    engine = object.__new__(RegistrationEngine)
    engine.email = "new@example.com"
    engine.password = "Secret123!"
    engine.email_info = {"service_id": "mailbox-1"}
    engine.session = _FakeSession()
    engine.logs = []
    engine.callback_logger = None
    engine.task_uuid = None
    engine.proxy_url = None
    engine._otp_sent_at = None
    engine._otp_page_reached = False
    engine._otp_delivery_requested = False
    engine._otp_delivery_confirmed = False
    engine._otp_delivery_method = ""
    engine._otp_delivery_http_status = 0
    engine._otp_delivery_page_type = ""
    engine._is_existing_account = False
    engine._device_id = None
    engine._sentinel_token = None
    engine._signup_sentinel = None
    engine._password_sentinel = None
    engine._create_account_continue_url = None
    engine._email_otp_continue_url = ""
    engine._email_otp_page_loaded = False
    engine._email_otp_csrf_token = ""
    engine._otp_continue_url = None
    engine._otp_page_type = None
    engine._otp_response_data = {}
    engine._otp_external_method = "GET"
    engine._step_error_code = ""
    engine._step_error_message = ""
    engine.session_token = None
    engine.email_otp_first = False
    engine._oauth_email_verification = False
    engine.otp_submit_delay = 0.0
    return engine


def test_preflight_location_skips_duplicate_ip_lookup():
    engine = _bare_engine()
    engine.preflight_location = "CO"
    engine.http_client = SimpleNamespace(
        check_ip_location=lambda: (_ for _ in ()).throw(AssertionError("unexpected IP lookup"))
    )

    assert engine._check_ip_location() == (True, "CO")


def test_signup_email_otp_page_is_not_treated_as_existing_account():
    engine = _bare_engine()

    result = engine._submit_signup_form("device-id", None)

    assert result.success is True
    assert result.page_type == "email_otp_verification"
    assert result.is_existing_account is False
    assert engine._is_existing_account is False


def test_protocol_email_otp_signup_sends_otp_without_password_step():
    engine = _bare_engine()
    calls = {"password": 0, "send": 0}

    def create_email():
        engine.email = "new@example.com"
        engine.email_info = {"service_id": "mailbox-1"}
        return True

    def register_password():
        calls["password"] += 1
        return False, None

    def send_otp():
        calls["send"] += 1
        return True

    engine._check_ip_location = lambda: (True, "JP")
    engine._create_email = create_email
    engine._init_session = lambda: True
    engine._start_oauth = lambda: True
    engine._get_device_id = lambda: "device-id"
    engine._check_sentinel = lambda did: None
    engine._submit_signup_form = lambda did, sen: SignupFormResult(
        success=True,
        page_type=OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
        response_data={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}},
    )
    engine._register_password = register_password
    engine._send_verification_code = send_otp
    engine._get_verification_code = lambda: None

    result = engine.run()

    assert result.success is False
    assert result.error_message == "获取验证码失败"
    assert calls == {"password": 0, "send": 1}


def test_send_verification_code_uses_email_verification_referer():
    engine = _bare_engine()
    calls = []

    class SendSession:
        def get(self, url, headers=None, timeout=None):
            calls.append((url, headers or {}))
            return _SendOtpResponse()

    engine.session = SendSession()

    assert engine._send_verification_code() is True
    assert calls[-1][0].endswith("/api/accounts/email-otp/send")
    assert calls[-1][1]["referer"] == "https://auth.openai.com/email-verification"


def test_send_verification_code_visits_email_verification_page_before_send():
    engine = _bare_engine()
    engine._email_otp_continue_url = "https://auth.openai.com/email-verification"
    calls = []

    class SendSession:
        def get(self, url, headers=None, timeout=None):
            calls.append((url, headers or {}))
            if len(calls) == 1:
                return _EmailVerificationPageResponse()
            return _SendOtpResponse()

    engine.session = SendSession()

    assert engine._send_verification_code() is True
    assert calls[0][0] == "https://auth.openai.com/email-verification"
    assert calls[1][0].endswith("/api/accounts/email-otp/send")
    assert engine._email_otp_page_loaded is True


def test_passwordless_email_challenge_prefers_resend_and_confirms_delivery():
    engine = _bare_engine()
    engine._oauth_email_verification = True
    engine._email_otp_page_loaded = True
    calls = []

    class ResendSession:
        def post(self, url, headers=None, data=None, timeout=None):
            calls.append(("POST", url, headers or {}, data))
            return _SendOtpResponse()

        def get(self, *args, **kwargs):
            raise AssertionError("confirmed resend must not create a second challenge")

    engine.session = ResendSession()

    assert engine._send_verification_code() is True
    assert calls[0][1].endswith("/api/accounts/email-otp/resend")
    assert engine._otp_delivery_requested is True
    assert engine._otp_delivery_confirmed is True
    assert engine._otp_delivery_method == "resend:POST"
    assert engine._otp_delivery_http_status == 200
    assert engine._otp_sent_at is not None


def test_failed_otp_delivery_never_sets_sent_timestamp():
    engine = _bare_engine()
    engine._email_otp_page_loaded = True

    class FailedResponse:
        status_code = 409
        text = '{"error":{"code":"invalid_state"}}'

        def json(self):
            return {"error": {"code": "invalid_state"}}

    class FailedSession:
        def get(self, *args, **kwargs):
            return FailedResponse()

    engine.session = FailedSession()

    assert engine._send_verification_code() is False
    assert engine._otp_delivery_requested is True
    assert engine._otp_delivery_confirmed is False
    assert engine._otp_sent_at is None
    assert engine._step_error_code == "otp_delivery_failed"


def test_send_otp_405_post_fallback_is_reachable_and_confirmed():
    engine = _bare_engine()
    engine._email_otp_page_loaded = True
    calls = []

    class MethodResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = "{}"

        def json(self):
            return {}

    class MethodSession:
        def get(self, url, **kwargs):
            calls.append(("GET", url))
            return MethodResponse(405)

        def post(self, url, **kwargs):
            calls.append(("POST", url))
            return MethodResponse(200)

    engine.session = MethodSession()

    assert engine._send_verification_code() is True
    assert [method for method, _url in calls] == ["GET", "POST"]
    assert engine._otp_delivery_confirmed is True
    assert engine._otp_delivery_method == "send:POST"


def test_mailbox_polling_requires_confirmed_delivery():
    engine = _bare_engine()

    class Mailbox:
        def get_verification_code(self, **kwargs):
            raise AssertionError("mailbox must not be polled before delivery confirmation")

    engine.email_service = Mailbox()

    assert engine._get_verification_code() is None
    assert engine._step_error_code == "otp_delivery_not_confirmed"
    assert "投递未获得服务端确认" in engine._last_otp_error


def test_validate_verification_code_uses_device_and_otp_sentinel_flow():
    engine = _bare_engine()
    engine._device_id = "device-id"
    engine._email_otp_continue_url = "https://auth.openai.com/email-verification"
    sentinel_calls = []
    post_calls = []

    class ValidateSession:
        def post(self, url, headers=None, data=None, timeout=None):
            post_calls.append((url, headers or {}, data, timeout))
            return _JsonResponse()

    def check_sentinel(device_id, *, flow="authorize_continue"):
        sentinel_calls.append((device_id, flow))
        return SentinelPayload(p="pow", t="turnstile", c="challenge", flow=flow)

    engine.session = ValidateSession()
    engine._check_sentinel = check_sentinel

    assert engine._validate_verification_code("123456") is True
    assert sentinel_calls == [("device-id", "email_otp_validate")]

    _, headers, body, timeout = post_calls[0]
    assert headers["origin"] == "https://auth.openai.com"
    assert headers["referer"] == "https://auth.openai.com/email-verification"
    assert headers["oai-device-id"] == "device-id"
    assert json.loads(headers["openai-sentinel-token"]) == {
        "p": "pow",
        "t": "turnstile",
        "c": "challenge",
        "id": "device-id",
        "flow": "email_otp_validate",
    }
    assert json.loads(body) == {"code": "123456"}
    assert timeout == 15


def test_validate_verification_code_classifies_invalid_state_without_logging_payload():
    engine = _bare_engine()
    engine._device_id = "device-id"
    engine._check_sentinel = lambda *args, **kwargs: None

    class InvalidStateResponse:
        status_code = 409
        text = '{"error":{"code":"invalid_state","message":"secret response detail"}}'

        def json(self):
            return {
                "error": {
                    "code": "invalid_state",
                    "message": "secret response detail",
                }
            }

    class InvalidStateSession:
        def post(self, *args, **kwargs):
            return InvalidStateResponse()

    engine.session = InvalidStateSession()

    assert engine._validate_verification_code("123456") is False
    assert engine._step_error_code == "oauth_invalid_state"
    assert engine._step_error_message == "secret response detail"
    assert all("123456" not in item and "secret response detail" not in item for item in engine.logs)


def test_validate_verification_code_classifies_deactivated_email_identity():
    engine = _bare_engine()
    engine._device_id = "device-id"
    engine._check_sentinel = lambda *args, **kwargs: None

    class DeactivatedResponse:
        status_code = 403

        def json(self):
            return {
                "error": {
                    "code": "account_deactivated",
                    "message": "account deleted or deactivated",
                }
            }

    class Session:
        def post(self, *args, **kwargs):
            return DeactivatedResponse()

    engine.session = Session()

    assert engine._validate_verification_code("123456") is False
    assert engine._step_error_code == "email_account_deactivated"


def test_validate_verification_code_reads_nested_external_handoff():
    engine = _bare_engine()
    engine._check_sentinel = lambda *args, **kwargs: None

    class Response:
        status_code = 200

        def json(self):
            return {
                "page": {
                    "type": "external_url",
                    "payload": {
                        "url": "https://auth.openai.com/api/oauth/oauth2/auth?opaque=secret",
                        "method": "GET",
                    },
                }
            }

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    engine.session = Session()

    assert engine._validate_verification_code("123456") is True
    assert engine._otp_page_type == "external_url"
    assert engine._otp_external_method == "GET"
    assert engine._otp_continue_url.endswith("opaque=secret")
    assert all("opaque=secret" not in item for item in engine.logs)


class _ExternalResponse:
    def __init__(self, status_code=200, *, url="", location="", data=None):
        self.status_code = status_code
        self.url = url
        self.headers = {"Location": location} if location else {}
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def test_external_handoff_follows_redirect_to_about_you():
    engine = _bare_engine()
    engine._otp_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth?opaque=secret"
    engine._otp_external_method = "GET"
    calls = []

    class Session:
        cookies = {}

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if "/api/oauth/oauth2/auth" in url:
                return _ExternalResponse(
                    302,
                    url=url,
                    location="/about-you",
                )
            return _ExternalResponse(200, url="https://auth.openai.com/about-you")

    engine.session = Session()

    assert engine._advance_external_registration_step() is True
    assert engine._otp_page_type == "about_you"
    assert [url.split("?")[0] for url, _ in calls] == [
        "https://auth.openai.com/api/oauth/oauth2/auth",
        "https://auth.openai.com/about-you",
    ]
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)


def test_external_handoff_stops_before_oauth_callback():
    engine = _bare_engine()
    engine._otp_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth"
    callback_url = "https://chatgpt.com/api/auth/callback/openai?code=TOKEN&state=STATE"
    calls = []

    class Session:
        cookies = {}

        def get(self, url, **kwargs):
            calls.append(url)
            return _ExternalResponse(302, url=url, location=callback_url)

    engine.session = Session()

    assert engine._advance_external_registration_step() is True
    assert engine._otp_page_type == "oauth_callback"
    assert engine._create_account_continue_url == callback_url
    assert calls == ["https://auth.openai.com/api/oauth/oauth2/auth"]


def test_external_handoff_rejects_non_get_method():
    engine = _bare_engine()
    engine._otp_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth"
    engine._otp_external_method = "POST"

    assert engine._advance_external_registration_step() is False
    assert engine._step_error_code == "external_auth_method_unsupported"


def test_external_handoff_has_redirect_limit():
    engine = _bare_engine()
    engine._otp_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth"
    calls = []

    class Session:
        cookies = {}

        def get(self, url, **kwargs):
            calls.append(url)
            return _ExternalResponse(302, url=url, location=f"/api/oauth/oauth2/auth?step={len(calls)}")

    engine.session = Session()

    assert engine._advance_external_registration_step(max_redirects=3) is False
    assert engine._step_error_code == "external_auth_redirect_limit"
    assert len(calls) == 3


def test_run_skips_create_account_after_external_callback():
    engine = _bare_engine()
    engine.email_service = SimpleNamespace(service_type=SimpleNamespace(value="local_ms_pool"))
    callback_url = "https://chatgpt.com/api/auth/callback/openai?code=TOKEN&state=STATE"
    create_calls = []
    delivery_calls = []

    class Cookies(dict):
        pass

    class Session:
        def __init__(self):
            self.cookies = Cookies()

        def get(self, url, **kwargs):
            if url == callback_url:
                self.cookies["__Secure-next-auth.session-token"] = "session-token"
                return _ExternalResponse(200, url=url)
            if str(url).endswith("/api/auth/session"):
                return _ExternalResponse(200, url=url, data={
                    "accessToken": "header.payload.signature",
                    "refreshToken": "session-refresh-token",
                    "expires": "2030-01-01T00:00:00.000Z",
                    "user": {"email": "new@example.com"},
                })
            raise AssertionError(f"unexpected GET {url}")

    engine.session = Session()
    engine._check_ip_location = lambda: (True, "CO")
    engine._create_email = lambda: True
    engine._init_session = lambda: True
    engine._start_oauth = lambda: True

    def get_device_id():
        engine._device_id = "device-id"
        engine._oauth_email_verification = True
        engine.email_otp_first = True
        return "device-id"

    engine._get_device_id = get_device_id
    engine._check_sentinel = lambda *args, **kwargs: None
    engine._send_verification_code = lambda: delivery_calls.append(True) or True
    engine._get_verification_code = lambda: "123456"

    def validate(_code):
        engine._otp_page_type = "external_url"
        return True

    def advance():
        engine._otp_page_type = "oauth_callback"
        engine._create_account_continue_url = callback_url
        return True

    engine._validate_verification_code = validate
    engine._advance_external_registration_step = advance
    engine._create_user_account = lambda: create_calls.append(True) or False

    result = engine.run()

    assert result.success is True
    assert create_calls == []
    assert delivery_calls == [True]
    assert result.workspace_id == ""
    assert result.refresh_token == "session-refresh-token"
    assert result.metadata["refresh_token_status"] == "available"
    assert result.metadata["refresh_token_source"] == "session"
    assert result.metadata["session_token_present"] is True
    assert result.metadata["access_token_expires_at"] == "2030-01-01T00:00:00.000Z"


def test_extract_chatgpt_account_id_prefers_auth_claim():
    import base64

    payload = {
        "sub": "user-id",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    assert _extract_chatgpt_account_id(f"header.{encoded}.signature") == "acct-123"


def test_session_credentials_preserve_refresh_token_and_expiry():
    credentials = _extract_chatgpt_session_credentials(
        {
            "accessToken": "web-access",
            "refreshToken": "web-refresh",
            "idToken": "web-id",
            "expires": "2030-01-01T00:00:00.000Z",
        }
    )

    assert credentials == {
        "access_token": "web-access",
        "refresh_token": "web-refresh",
        "id_token": "web-id",
        "expires_at": "2030-01-01T00:00:00.000Z",
        "refresh_token_source": "session",
        "refresh_token_status": "available",
    }


def test_session_credentials_fall_back_to_oauth_refresh_token_without_fabricating_one():
    credentials = _extract_chatgpt_session_credentials(
        {"accessToken": "web-access"},
        {
            "access_token": "oauth-access",
            "refresh_token": "oauth-refresh",
            "id_token": "oauth-id",
            "expired": "2030-01-01T00:00:00Z",
        },
    )

    assert credentials["access_token"] == "web-access"
    assert credentials["refresh_token"] == "oauth-refresh"
    assert credentials["refresh_token_source"] == "oauth_callback"
    assert credentials["refresh_token_status"] == "available"
    assert credentials["expires_at"] == "2030-01-01T00:00:00Z"

    missing = _extract_chatgpt_session_credentials({"accessToken": "web-access"})
    assert missing["refresh_token"] == ""
    assert missing["refresh_token_status"] == "missing_from_session"


def test_email_otp_first_reuses_authorize_state_but_confirms_delivery():
    engine = _bare_engine()
    engine.email_otp_first = True
    calls = {"submit": 0, "send": 0, "validate": 0}

    def create_email():
        engine.email = "new@example.com"
        engine.email_info = {"service_id": "mailbox-1"}
        return True

    def get_device_id():
        engine._device_id = "device-id"
        engine._oauth_email_verification = True
        engine._email_otp_continue_url = "https://auth.openai.com/email-verification"
        return "device-id"

    def submit_signup(*args):
        calls["submit"] += 1
        raise AssertionError("authorize/continue must not run after direct email verification")

    def send_otp():
        calls["send"] += 1
        return True

    def validate(code):
        calls["validate"] += 1
        return False

    engine._check_ip_location = lambda: (True, "JP")
    engine._create_email = create_email
    engine._init_session = lambda: True
    engine._start_oauth = lambda: True
    engine._get_device_id = get_device_id
    engine._check_sentinel = lambda did: None
    engine._submit_signup_form = submit_signup
    engine._send_verification_code = send_otp
    engine._get_verification_code = lambda: "123456"
    engine._validate_verification_code = validate

    result = engine.run()

    assert result.success is False
    assert calls == {"submit": 0, "send": 1, "validate": 1}
