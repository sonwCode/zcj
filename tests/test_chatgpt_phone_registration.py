from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import platforms.chatgpt.browser_register as browser_register
import platforms.chatgpt.bind_email as bind_email_module
import platforms.chatgpt.protocol_phone as protocol_phone_module
from core.base_platform import AccountStatus
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.protocol_phone import (
    ChatGPTProtocolEmailThenPhoneWorker,
    ChatGPTProtocolPhoneWorker,
)
from platforms.chatgpt.register import RegistrationEngine, RegistrationResult as EngineRegistrationResult


class _PhoneCallback:
    def __init__(self):
        self.values = iter(["+573001234567", "482913"])
        self.send_succeeded = 0
        self.success = 0
        self.code_failures: list[str] = []

    def __call__(self):
        return next(self.values)

    def mark_send_succeeded(self):
        self.send_succeeded += 1

    def report_success(self):
        self.success += 1

    def mark_code_failed(self, reason=""):
        self.code_failures.append(reason)


class _CookieJar:
    def __init__(self):
        self.values = {}

    def get(self, name, default=""):
        return self.values.get(name, default)

    def set(self, name, value, **kwargs):
        self.values[name] = value


class _JsonResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_phone_email_binding_uses_password_accepted_during_registration(monkeypatch):
    captured = {}

    class Callback:
        def __call__(self):
            return "123456"

        def mark_send_succeeded(self):
            pass

        def report_success(self):
            pass

    class FakeEngine:
        def __init__(self, **kwargs):
            self.logs = []
            self.password = ""
            self.session = object()
            self._device_id = "did-1"
            self._authorize_final_url = "https://auth.openai.com/create-account/password"
            self._password_next_page_type = "phone_otp_send"
            self._step_error_code = ""
            self._step_error_message = ""

        def _init_session(self):
            return True

        def _start_oauth(self):
            return True

        def _get_device_id(self):
            return self._device_id

        def _register_password(self):
            self.password = "ActualRegistered123!"
            return True, self.password

        def _create_user_account(self):
            return True

    class FakeBindWorker:
        mailbox_account = None

        def __init__(self, **kwargs):
            captured["password"] = kwargs["password"]

        def bind_with_engine(self, engine):
            raise RuntimeError("stop after password capture")

    monkeypatch.setattr(protocol_phone_module, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(bind_email_module, "ChatGPTProtocolBindEmailWorker", FakeBindWorker)
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=Callback(),
        mailbox_factory=lambda _proxy: object(),
        bind_email_after_registration=True,
        log_fn=lambda _message: None,
    )
    monkeypatch.setattr(worker, "_send_phone_otp", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "_collect_session",
        lambda engine, phone_number, proxy_url: EngineRegistrationResult(
            success=True,
            email=f"phone:{phone_number}",
            password=engine.password,
            account_id="account-1",
            access_token="access-1",
            metadata={},
        ),
    )

    result = worker._run_number("+56911112222", "OuterInitial123!", None)

    assert result.success is True
    assert result.password == "ActualRegistered123!"
    assert captured["password"] == "ActualRegistered123!"


def test_protocol_phone_rebuilds_oauth_session_after_transient_device_id_failure(monkeypatch):
    engines = []

    class FlakyEngine:
        def __init__(self, **kwargs):
            self.index = len(engines)
            engines.append(self)
            self.logs = []
            self.password = ""
            self._device_id = "did-1"
            self._authorize_final_url = "https://auth.openai.com/create-account/password"
            self._password_next_page_type = ""
            self._step_error_code = ""
            self._step_error_message = ""

        def _init_session(self):
            return True

        def _start_oauth(self):
            return True

        def _get_device_id(self):
            if self.index == 0:
                self._step_error_code = "proxy_network_error"
                return None
            return self._device_id

        def _register_password(self):
            self.password = "ActualRegistered123!"
            return True, self.password

        def _create_user_account(self):
            return True

    monkeypatch.setattr(protocol_phone_module, "RegistrationEngine", FlakyEngine)
    monkeypatch.setattr(protocol_phone_module.time, "sleep", lambda _seconds: None)
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=lambda: "123456",
        bind_email_after_registration=False,
        log_fn=lambda _message: None,
    )
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "_collect_session",
        lambda engine, phone_number, proxy_url: EngineRegistrationResult(
            success=True,
            email=f"phone:{phone_number}",
            password=engine.password,
            account_id="account-1",
        ),
    )

    result = worker._run_number("+628000000001", "Initial123!", None)

    assert result.success is True
    assert len(engines) == 2


def test_protocol_phone_oauth_uses_nextauth_api_bootstrap():
    calls = []

    class FakeSession:
        def __init__(self):
            self.cookies = _CookieJar()

        def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            if url.endswith("/api/auth/providers"):
                return _JsonResponse(200, {"openai": {"id": "openai"}})
            if url.endswith("/api/auth/csrf"):
                return _JsonResponse(200, {"csrfToken": "csrf-123"})
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return _JsonResponse(200, {"url": "https://auth.openai.com/create-account/password"})

    engine = RegistrationEngine(email_service=SimpleNamespace(), callback_logger=lambda message: None)
    engine.session = FakeSession()
    engine.http_client = SimpleNamespace(
        get_chatgpt_headers=lambda referer="https://chatgpt.com/login": {
            "referer": referer,
            "sec-fetch-site": "same-origin",
        }
    )
    engine.email = "+573001234567"

    assert engine._start_oauth() is True
    assert [call[1] for call in calls[:2]] == [
        "https://chatgpt.com/api/auth/providers",
        "https://chatgpt.com/api/auth/csrf",
    ]
    _, signin_url, signin_kwargs = calls[2]
    assert "ext-passkey-client-capabilities=1111" in signin_url
    assert "login_hint=%2B573001234567" in signin_url
    assert "callbackUrl=https%3A%2F%2Fchatgpt.com%2Flogin" in signin_kwargs["data"]
    assert signin_kwargs["headers"]["sec-fetch-site"] == "same-origin"


def test_phone_registration_state_machine_completes_sms_and_profile(monkeypatch):
    page = SimpleNamespace(url="https://chatgpt.com/")
    callback = _PhoneCallback()

    monkeypatch.setattr(browser_register, "_open_phone_signup_entry", lambda page, log: {"phoneReady": True})
    monkeypatch.setattr(
        browser_register,
        "_submit_signup_phone_identity",
        lambda page, phone, log: {"page_type": "create_account_password", "current_url": "https://auth.openai.com/create-account/password"},
    )
    monkeypatch.setattr(
        browser_register,
        "_submit_password_via_page",
        lambda page, password, log: {
            "ok": True,
            "data": {"page": {"type": "phone_verification"}},
            "url": "https://auth.openai.com/phone-verification",
        },
    )
    monkeypatch.setattr(
        browser_register,
        "_submit_phone_otp_dom",
        lambda page, code, log: {
            "ok": True,
            "data": {"page": {"type": "about_you"}},
            "url": "https://auth.openai.com/about-you",
        },
    )

    def submit_about(page, log):
        page.url = "https://chatgpt.com/"
        return {"ok": True, "data": {"page": {"type": "chatgpt_home"}}, "url": page.url}

    monkeypatch.setattr(browser_register, "_submit_about_you_via_page", submit_about)
    monkeypatch.setattr(browser_register, "_handle_post_signup_onboarding", lambda page, log: None)

    result = browser_register._browser_phone_registration_flow(page, "Secret123!", callback, lambda message: None)

    assert result["page_type"] == "chatgpt_home"
    assert result["phone_number"] == "+573001234567"
    assert result["register_mode"] == "phone"
    assert callback.send_succeeded == 1
    assert callback.success == 1
    assert callback.code_failures == []


def test_phone_registration_marks_invalid_sms_code(monkeypatch):
    page = SimpleNamespace(url="https://auth.openai.com/phone-verification")
    callback = _PhoneCallback()
    monkeypatch.setattr(browser_register, "_open_phone_signup_entry", lambda page, log: {"phoneReady": True})
    monkeypatch.setattr(
        browser_register,
        "_submit_signup_phone_identity",
        lambda page, phone, log: {"page_type": "phone_otp_verification", "current_url": page.url},
    )
    monkeypatch.setattr(
        browser_register,
        "_submit_phone_otp_dom",
        lambda page, code, log: {"ok": False, "status": 400, "text": "invalid otp code"},
    )

    with pytest.raises(RuntimeError, match="invalid otp code"):
        browser_register._browser_phone_registration_flow(
            page,
            "Secret123!",
            callback,
            lambda message: None,
            max_phone_attempts=1,
        )

    assert callback.code_failures == ["invalid otp code"]


def test_phone_registration_replaces_number_after_sms_timeout(monkeypatch):
    page = SimpleNamespace(url="https://auth.openai.com/phone-verification")

    class RotatingCallback:
        def __init__(self):
            self.phase = "need_number"
            self.activation = None
            self.completed = False
            self.number_index = 0
            self.code_index = 0
            self.cleanup_count = 0

        def __call__(self):
            if self.phase == "need_number":
                number = ["+573001111111", "+563002222222"][self.number_index]
                self.number_index += 1
                self.phase = "need_code"
                return number
            self.code_index += 1
            return "" if self.code_index == 1 else "482913"

        def cleanup(self):
            self.cleanup_count += 1

        def mark_send_failed(self, reason=""):
            pass

        def mark_send_succeeded(self):
            pass

        def report_success(self):
            self.completed = True

    callback = RotatingCallback()
    monkeypatch.setattr(browser_register, "_open_phone_signup_entry", lambda page, log: {"phoneReady": True})
    monkeypatch.setattr(
        browser_register,
        "_submit_signup_phone_identity",
        lambda page, phone, log: {"page_type": "phone_otp_verification", "current_url": page.url},
    )

    def submit_otp(page, code, log):
        page.url = "https://chatgpt.com/"
        return {"ok": True, "data": {"page": {"type": "chatgpt_home"}}, "url": page.url}

    monkeypatch.setattr(browser_register, "_submit_phone_otp_dom", submit_otp)
    monkeypatch.setattr(browser_register, "_handle_post_signup_onboarding", lambda page, log: None)

    result = browser_register._browser_phone_registration_flow(page, "Secret123!", callback, lambda message: None)

    assert callback.number_index == 2
    assert callback.cleanup_count == 1
    assert result["phone_number"] == "+563002222222"


def test_phone_result_uses_unique_identifier_and_requires_session(monkeypatch):
    page = SimpleNamespace(url="https://chatgpt.com/")
    worker = browser_register.ChatGPTBrowserRegister(
        headless=False,
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )
    monkeypatch.setattr(
        browser_register,
        "_browser_phone_registration_flow",
        lambda page, password, callback, log: {
            "page_type": "chatgpt_home",
            "phone_number": "+573001234567",
            "register_mode": "phone",
        },
    )
    monkeypatch.setattr(browser_register, "_get_cookies", lambda page: {})
    monkeypatch.setattr(
        browser_register,
        "_fetch_chatgpt_session_from_page",
        lambda page, cookies, log: {"account_id": "acct-phone", "access_token": "at-phone"},
    )

    result = worker._run_flow_and_collect(page, email="", password="Secret123!", register_mode="phone")

    assert result["email"] == "phone:+573001234567"
    assert result["account_id"] == "acct-phone"
    assert result["access_token"] == "at-phone"

    monkeypatch.setattr(
        browser_register,
        "_fetch_chatgpt_session_from_page",
        lambda page, cookies, log: {"account_id": "acct-phone", "access_token": ""},
    )
    with pytest.raises(RuntimeError, match="session 缺少"):
        worker._run_flow_and_collect(page, email="", password="Secret123!", register_mode="phone")


def test_phone_result_mapper_rejects_incomplete_registration():
    platform = object.__new__(ChatGPTPlatform)

    with pytest.raises(RuntimeError, match="缺少 account_id"):
        platform._map_chatgpt_result({
            "register_mode": "phone",
            "phone_number": "+573001234567",
            "access_token": "at-phone",
        })

    result = platform._map_chatgpt_result({
        "register_mode": "phone",
        "email": "phone:+573001234567",
        "phone_number": "+573001234567",
        "account_id": "acct-phone",
        "access_token": "at-phone",
    })
    assert result.email == "phone:+573001234567"
    assert result.user_id == "acct-phone"
    assert result.extra["phone_number"] == "+573001234567"


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        ("9 8765 4340", "987654340"),
        ("+56 9 8765 4340", "987654340"),
        ("(09) 8765-4340", "0987654340"),
    ],
)
def test_phone_input_validation_accepts_browser_formatting(actual, expected):
    assert browser_register._phone_input_digits_match(actual, expected) is True


def test_phone_registration_does_not_fallback_to_camoufox(monkeypatch):
    worker = browser_register.ChatGPTBrowserRegister(
        headless=False,
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )
    monkeypatch.setattr(
        worker,
        "_run_once_chromium",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("phone form failed")),
    )
    monkeypatch.setattr(
        worker,
        "_run_once",
        lambda **kwargs: pytest.fail("phone registration must not fall back to Camoufox"),
    )

    with pytest.raises(RuntimeError, match="phone form failed"):
        worker.run(email="", password="Secret123!", register_mode="phone")


def test_protocol_phone_worker_rotates_numbers_inside_one_account_attempt(monkeypatch):
    class Callback:
        def __init__(self):
            self.phase = "need_number"
            self.activation = None
            self.completed = False
            self.values = iter(["+573001111111", "+563002222222"])
            self.cleanup_count = 0

        def __call__(self):
            return next(self.values)

        def cleanup(self):
            self.cleanup_count += 1

        def mark_send_failed(self, reason=""):
            pass

    callback = Callback()
    worker = ChatGPTProtocolPhoneWorker(phone_callback=callback, log_fn=lambda message: None)
    calls = []

    def fake_run_number(phone_number, password, proxy_url):
        calls.append(phone_number)
        if len(calls) == 1:
            return EngineRegistrationResult(
                success=False,
                error_code="phone_number_in_use",
                error_message="used",
            )
        return EngineRegistrationResult(success=True, email=f"phone:{phone_number}")

    monkeypatch.setattr(worker, "_run_number", fake_run_number)
    result = worker.run(password="Secret123!")

    assert result.success is True
    assert calls == ["+573001111111", "+563002222222"]
    assert callback.cleanup_count == 1


def test_protocol_phone_worker_pins_711proxy_to_rented_number_country(monkeypatch):
    class Callback:
        def __call__(self):
            return "+56911112222"

    proxies = []
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=Callback(),
        proxy_url=(
            "http://USER-zone-custom:secret@global.rotgb.711proxy.com:10000"
        ),
        proxy_session_id="fixed123",
        log_fn=lambda message: None,
    )

    def fake_run_number(phone_number, password, proxy_url):
        proxies.append(proxy_url)
        return EngineRegistrationResult(success=True, email=f"phone:{phone_number}")

    monkeypatch.setattr(worker, "_run_number", fake_run_number)

    result = worker.run(password="Secret123!")

    assert result.success is True
    assert len(proxies) == 1
    assert "region-CL" in proxies[0]
    assert "session-fixed123" in proxies[0]


def test_protocol_phone_result_mapper_preserves_pending_email_binding_context():
    platform = object.__new__(ChatGPTPlatform)
    adapter = platform.build_protocol_phone_adapter()
    engine_result = EngineRegistrationResult(
        success=True,
        email="phone:+56911112222",
        password="Secret123!",
        account_id="acct-phone",
        access_token="access-token",
        session_token="session-token",
        metadata={
            "phone_number": "+56911112222",
            "register_mode": "phone",
            "email_binding_status": "failed",
            "email_binding_error": "otp timeout",
            "auth_cookies": [{"name": "login_session", "value": "cookie"}],
            "oai_device_id": "did-1",
            "auth_proxy_url": "http://proxy.example:8080",
            "auth_proxy_session": "session123",
        },
    )

    result = adapter.result_mapper(SimpleNamespace(password="Secret123!"), engine_result)

    assert result.status == AccountStatus.PENDING_VERIFICATION
    assert result.email == "phone:+56911112222"
    assert result.extra["email_binding_error"] == "otp timeout"
    assert result.extra["oai_device_id"] == "did-1"
    assert result.extra["auth_proxy_url"] == "http://proxy.example:8080"
    assert json.loads(result.extra["auth_cookies"])[0]["name"] == "login_session"


def test_protocol_phone_worker_releases_final_bound_number(monkeypatch):
    class Callback:
        def __init__(self):
            self.phase = "need_number"
            self.activation = object()
            self.completed = False
            self.cleanup_count = 0
            self.send_failures = []

        def __call__(self):
            return "+573001111111"

        def mark_send_failed(self, reason=""):
            self.send_failures.append(reason)

        def cleanup(self):
            self.cleanup_count += 1

    callback = Callback()
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=callback,
        log_fn=lambda message: None,
        max_phone_attempts=1,
    )
    monkeypatch.setattr(
        worker,
        "_run_number",
        lambda phone_number, password, proxy_url: EngineRegistrationResult(
            success=False,
            error_code="phone_number_in_use",
            error_message="bound",
        ),
    )

    result = worker.run(password="Secret123!")

    assert result.error_code == "phone_number_in_use"
    assert callback.cleanup_count == 1
    assert callback.send_failures == ["bound"]
    assert callback.activation is None


def test_protocol_phone_worker_logs_proxy_phone_country_mismatch_once(monkeypatch):
    class Callback:
        def __init__(self):
            self.phase = "need_number"
            self.activation = object()
            self.completed = False
            self.cleanup_count = 0

        def __call__(self):
            return "+628000000001"

        def mark_send_failed(self, reason=""):
            return None

        def cleanup(self):
            self.cleanup_count += 1

    logs = []
    attempts = []
    callback = Callback()
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=callback,
        log_fn=logs.append,
        max_phone_attempts=8,
        proxy_country="MY",
    )
    def reject_number(phone_number, password, proxy_url):
        attempts.append(phone_number)
        return EngineRegistrationResult(
            success=False,
            error_code="phone_number_rejected",
            error_message="Failed to create account. Please try again.",
        )

    monkeypatch.setattr(
        worker,
        "_run_number",
        reject_number,
    )

    result = worker.run(password="Secret123!")

    mismatch_logs = [message for message in logs if "代理出口国家 MY" in message]
    assert len(mismatch_logs) == 1
    assert len(attempts) == 2
    assert callback.cleanup_count == 2
    assert result.error_code == "phone_proxy_country_mismatch"


def test_protocol_phone_worker_stops_after_three_similar_number_rejections(monkeypatch):
    class Callback:
        def __init__(self):
            self.phase = "need_number"
            self.activation = object()
            self.completed = False
            self.cleanup_count = 0

        def __call__(self):
            return "+628000000001"

        def mark_send_failed(self, reason=""):
            return None

        def cleanup(self):
            self.cleanup_count += 1

    callback = Callback()
    logs = []
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=callback,
        max_phone_attempts=8,
        log_fn=logs.append,
    )
    attempts = []

    def reject_number(phone_number, password, proxy_url):
        attempts.append(phone_number)
        return EngineRegistrationResult(
            success=False,
            error_code="phone_number_rejected",
            error_message=(
                "We've detected suspicious behavior from phone numbers similar to yours."
            ),
        )

    monkeypatch.setattr(worker, "_run_number", reject_number)

    result = worker.run(password="Secret123!")

    assert result.error_code == "phone_country_pool_rejected"
    assert len(attempts) == 3
    assert callback.cleanup_count == 3
    assert any("停止继续消耗" in message for message in logs)


def test_protocol_phone_worker_explicitly_sends_phone_otp():
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "continue_url": "https://auth.openai.com/phone-verification",
                "page": {"type": "phone_otp_verification"},
            }

    class Session:
        def get(self, url, **kwargs):
            calls.append(("GET", url, None))
            return Response()

        def post(self, url, **kwargs):
            calls.append(("POST", url, json.loads(kwargs.get("data") or "{}")))
            return Response()

    engine = SimpleNamespace(
        session=Session(),
        _device_id="did-1",
        _password_continue_url="https://auth.openai.com/phone-verification",
        _password_next_payload={"phone_verification_channel": "sms"},
        _step_error_code="",
        _step_error_message="",
    )
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )

    assert worker._send_phone_otp(engine, "+563001234567") is True
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert calls[1][1].endswith("/api/accounts/phone-otp/send")
    assert calls[1][2]["phone_verification_channel"] == "sms"


def test_protocol_email_phone_worker_uses_add_phone_send_transition():
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "continue_url": "https://auth.openai.com/phone-verification",
                "page": {"type": "phone_otp_verification"},
            }

    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    engine = SimpleNamespace(
        session=Session(),
        _device_id="did-1",
        _otp_page_type="add_phone",
        _otp_continue_url="https://auth.openai.com/add-phone",
        _password_continue_url="https://auth.openai.com/add-phone",
        _password_next_payload={},
        _step_error_code="",
        _step_error_message="",
    )
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )

    assert worker._send_add_phone_number(
        engine,
        "+573001234567",
        "https://auth.openai.com/add-phone",
    ) is True
    assert calls[0][0].endswith("/api/accounts/add-phone/send")
    assert json.loads(calls[0][1]["data"]) == {"phone_number": "+573001234567"}
    assert calls[0][1]["allow_redirects"] is False
    assert engine._otp_page_type == "phone_otp_verification"


def test_email_phone_worker_exchanges_codex_callback(monkeypatch):
    from platforms.chatgpt import protocol_phone as protocol_phone_module
    from platforms.chatgpt.constants import CODEX_REDIRECT_URI

    class Response:
        status_code = 302
        url = "https://auth.openai.com/oauth/authorize"
        headers = {
            "Location": (
                f"{CODEX_REDIRECT_URI}?code=CODE&state=STATE"
            )
        }

        def json(self):
            return {}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    engine = SimpleNamespace(
        session=Session(),
        _otp_continue_url=(f"{CODEX_REDIRECT_URI}?code=CODE&state=STATE"),
        _otp_page_type="external_url",
    )
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )
    worker._codex_oauth_start = SimpleNamespace(
        auth_url="https://auth.openai.com/oauth/authorize",
        state="STATE",
        code_verifier="VERIFIER",
    )
    monkeypatch.setattr(
        protocol_phone_module,
        "submit_callback_url",
        lambda **kwargs: json.dumps({
            "access_token": "codex-access",
            "refresh_token": "codex-refresh",
            "id_token": "codex-id",
        }),
    )

    result = worker._complete_codex_oauth(engine)

    assert result["refresh_token"] == "codex-refresh"


def test_email_phone_worker_reads_oauth_url_from_phone_otp_payload():
    class Response:
        status_code = 200

        def json(self):
            return {
                "page": {
                    "type": "external_url",
                    "payload": {"url": "/api/oauth/oauth2/auth?state=STATE"},
                }
            }

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    engine = SimpleNamespace(
        session=Session(),
        _device_id="did-1",
        _otp_continue_url="",
        _otp_page_type="",
        _otp_response_data={},
        _step_error_code="",
        _step_error_message="",
    )
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )

    assert worker._validate_phone_otp(engine, "123456") is True
    assert engine._otp_page_type == "external_url"
    assert engine._otp_continue_url == "/api/oauth/oauth2/auth?state=STATE"
    assert engine._otp_response_data["page"]["type"] == "external_url"


def test_email_phone_worker_already_verified_returns_codex_tokens(monkeypatch):
    engine = SimpleNamespace()
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (engine, "", {"already_verified": True}),
    )
    monkeypatch.setattr(
        worker,
        "_complete_codex_oauth",
        lambda target_engine: {
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
        },
    )

    result = worker.run_for_account(email="user@example.com", password="Secret123!")

    assert result["already_verified"] is True
    assert result["refresh_token"] == "refresh"


def test_email_phone_worker_acquires_complete_codex_credentials_without_phone(monkeypatch):
    engine = SimpleNamespace()
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: "",
        log_fn=lambda message: None,
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (engine, "", {"already_verified": True}),
    )
    monkeypatch.setattr(
        worker,
        "_complete_codex_oauth",
        lambda target_engine: {
            "access_token": "codex-access",
            "refresh_token": "codex-refresh",
            "id_token": "codex-id",
        },
    )

    result = worker.acquire_codex_credentials(
        email="user@example.com",
        password="Secret123!",
    )

    assert result["ok"] is True
    assert result["data"]["refresh_token"] == "codex-refresh"


def test_email_phone_worker_reports_phone_required_without_renting_number(monkeypatch):
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: (_ for _ in ()).throw(AssertionError("must not rent phone")),
        log_fn=lambda message: None,
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (SimpleNamespace(), "https://auth.openai.com/add-phone", {}),
    )

    result = worker.acquire_codex_credentials(
        email="user@example.com",
        password="Secret123!",
    )

    assert result == {
        "ok": False,
        "error_code": "phone_required",
        "error": "Codex OAuth requires phone verification",
    }


def test_email_phone_worker_restores_account_scoped_cookie_context():
    class Cookies:
        def __init__(self):
            self.set_calls = []
            self.values = {}

        def set(self, name, value, **kwargs):
            self.set_calls.append((name, value, kwargs))
            self.values[(name, kwargs.get("domain", ""))] = value

        def get(self, name, default=""):
            for (cookie_name, _domain), value in reversed(list(self.values.items())):
                if cookie_name == name:
                    return value
            return default

    cookies = Cookies()
    engine = SimpleNamespace(
        session=SimpleNamespace(cookies=cookies),
        _device_id="",
    )
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: "",
        existing_device_id="stable-device-id",
        existing_auth_cookies=[
            {
                "name": "__Secure-next-auth.session-token",
                "value": "session-token",
                "domain": "chatgpt.com",
                "path": "/",
            }
        ],
        log_fn=lambda message: None,
    )
    worker._auth_generation = 1

    worker._restore_auth_context(engine)

    assert engine._device_id == "stable-device-id"
    assert any(
        name == "__Secure-next-auth.session-token"
        and value == "session-token"
        and kwargs["domain"] == "chatgpt.com"
        for name, value, kwargs in cookies.set_calls
    )
    assert any(
        name == "oai-did" and value == "stable-device-id"
        for name, value, _kwargs in cookies.set_calls
    )


def test_email_phone_worker_continues_account_chooser_with_exact_account(monkeypatch):
    account_id = "account-current"
    selected = []
    cookie_payload = protocol_phone_module.base64.urlsafe_b64encode(
        json.dumps(
            {
                "workspaces": [
                    {"id": "workspace-other", "account_id": "account-other"},
                    {"id": "workspace-current", "account_id": account_id},
                ]
            }
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")

    class Response:
        def __init__(self, status_code, *, url="", payload=None, headers=None):
            self.status_code = status_code
            self.url = url
            self._payload = payload or {}
            self.headers = headers or {}

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.cookies = _CookieJar()
            self.cookies.set("oai-did", "did-current")
            self.cookies.set("oai-client-auth-session", cookie_payload + ".signature")

        def get(self, url, **kwargs):
            assert "client_auth_session_dump" not in url
            return Response(200, url="https://auth.openai.com/choose-an-account")

        def post(self, url, **kwargs):
            selected.append(json.loads(kwargs["data"])["workspace_id"])
            return Response(
                200,
                url=url,
                payload={
                    "continue_url": "/add-phone",
                    "page": {"type": "add_phone", "payload": {"channel": "sms"}},
                },
            )

    class FakeEngine:
        def __init__(self, **kwargs):
            self.session = Session()
            self._device_id = ""
            self._authorize_final_url = ""
            self._otp_page_type = ""
            self._otp_continue_url = ""

        def _init_session(self):
            return True

    monkeypatch.setattr(protocol_phone_module, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        protocol_phone_module,
        "generate_oauth_url",
        lambda **kwargs: SimpleNamespace(
            auth_url="https://auth.openai.com/oauth/authorize",
            state="STATE",
            code_verifier="VERIFIER",
        ),
    )
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: (_ for _ in ()).throw(
            AssertionError("chooser continuation must not rent a phone")
        ),
        existing_account_id=account_id,
        log_fn=lambda message: None,
    )

    engine, add_phone_url, payload = worker._open_phone_challenge(
        email="user@example.com",
        password="Secret123!",
    )

    assert selected == ["workspace-current"]
    assert add_phone_url == "https://auth.openai.com/add-phone"
    assert payload == {"channel": "sms"}
    assert engine._otp_page_type == "add_phone"
    assert engine._otp_continue_url == add_phone_url


def test_email_phone_worker_rejects_ambiguous_account_chooser():
    candidates = [
        {"id": "workspace-one", "account_id": "account-one"},
        {"id": "workspace-two", "account_id": "account-two"},
    ]
    post_calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"workspaces": candidates}

    class Session:
        cookies = _CookieJar()

        def get(self, url, **kwargs):
            assert url.endswith("/api/accounts/client_auth_session_dump")
            return Response()

        def post(self, url, **kwargs):
            post_calls.append((url, kwargs))
            raise AssertionError("ambiguous chooser must not select an account")

    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=lambda: "",
        existing_account_id="account-missing",
        log_fn=lambda message: None,
    )

    with pytest.raises(RuntimeError, match="多个账号.*未找到当前账号"):
        worker._select_authorization_workspace(
            SimpleNamespace(session=Session()),
            "https://auth.openai.com/choose-an-account",
        )

    assert post_calls == []


def test_email_phone_worker_stops_on_account_deactivated_without_changing_number(monkeypatch):
    class Callback:
        def __init__(self):
            self.values = iter(["+15550001111", "482913"])
            self.phone_calls = 0
            self.cleanup_count = 0

        def __call__(self):
            value = next(self.values)
            if value.startswith("+"):
                self.phone_calls += 1
            return value

        def mark_send_failed(self, reason=""):
            return None

        def cleanup(self):
            self.cleanup_count += 1

    callback = Callback()
    engine = SimpleNamespace(
        _otp_page_type="add_phone",
        _step_error_code="account_deactivated",
        _step_error_message=(
            "You do not have an account because it has been deleted or deactivated."
        ),
    )
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=callback,
        log_fn=lambda message, **kwargs: None,
        max_phone_attempts=8,
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (engine, "https://auth.openai.com/add-phone", {}),
    )
    monkeypatch.setattr(worker, "_send_add_phone_number", lambda *args: True)
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args: False)
    monkeypatch.setattr(worker, "_reset_number", lambda reason: None)
    monkeypatch.setattr(worker, "_complete_codex_oauth", lambda target_engine: {})

    with pytest.raises(RuntimeError, match="ACCOUNT_DEACTIVATED"):
        worker.run_for_account(email="user@example.com", password="Secret123!")

    assert callback.phone_calls == 1
    assert callback.cleanup_count == 1


def test_email_phone_worker_stops_on_fraud_guard_without_rotating_more_numbers(monkeypatch):
    class Callback:
        def __init__(self):
            self.phone_calls = 0
            self.cleanup_count = 0

        def __call__(self):
            self.phone_calls += 1
            return "+15550001111"

        def mark_send_failed(self, reason=""):
            self.failure_reason = reason

        def cleanup(self):
            self.cleanup_count += 1

    callback = Callback()
    engine = SimpleNamespace(
        _otp_page_type="add_phone",
        _step_error_code="fraud_guard",
        _step_error_message=(
            "We've detected suspicious behavior from phone numbers similar to yours."
        ),
    )
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=SimpleNamespace(),
        phone_callback=callback,
        log_fn=lambda message, **kwargs: None,
        max_phone_attempts=8,
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (engine, "https://auth.openai.com/add-phone", {}),
    )
    monkeypatch.setattr(worker, "_send_add_phone_number", lambda *args: False)

    with pytest.raises(RuntimeError, match="PHONE_RISK_REJECTED"):
        worker.run_for_account(email="user@example.com", password="Secret123!")

    assert callback.phone_calls == 1
    assert "suspicious behavior" in callback.failure_reason.lower()


def test_protocol_phone_worker_classifies_suspicious_sms_send_as_number_rejection():
    class PageResponse:
        status_code = 200

    class RejectedResponse:
        status_code = 400

        def json(self):
            return {
                "error": {
                    "message": (
                        "We've detected suspicious behavior from phone numbers similar "
                        "to yours. Please try again later."
                    ),
                    "code": "",
                }
            }

    class Session:
        def get(self, url, **kwargs):
            return PageResponse()

        def post(self, url, **kwargs):
            return RejectedResponse()

    engine = SimpleNamespace(
        session=Session(),
        _device_id="did-1",
        _password_continue_url="https://auth.openai.com/phone-verification",
        _password_next_payload={"phone_verification_channel": "sms"},
        _step_error_code="",
        _step_error_message="",
    )
    logs = []
    worker = ChatGPTProtocolPhoneWorker(
        phone_callback=lambda: "",
        log_fn=logs.append,
    )

    assert worker._send_phone_otp(engine, "+628000000001") is False
    assert engine._step_error_code == "phone_number_rejected"
    assert "suspicious behavior" in engine._step_error_message.lower()
    assert any("立即释放并换号" in message for message in logs)


@pytest.mark.parametrize(
    ("server_code", "server_message"),
    [
        ("voip_phone_disallowed", "Invalid phone number. Please try again."),
        ("account_creation_failed", "Failed to create account. Please try again."),
    ],
)
def test_protocol_password_phone_rejection_stops_password_retries(
    monkeypatch,
    server_code,
    server_message,
):
    class RejectedResponse:
        status_code = 400
        text = "phone registration rejected"

        def json(self):
            return {
                "error": {
                    "message": server_message,
                    "code": server_code,
                }
            }

    calls = []

    class FakeSession:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            return RejectedResponse()

    engine = RegistrationEngine(email_service=SimpleNamespace(), callback_logger=lambda message: None)
    engine.session = FakeSession()
    engine.email = "+15550001111"
    engine._device_id = "did-1"
    monkeypatch.setattr(engine, "_load_create_account_password_page", lambda: True)
    monkeypatch.setattr(engine, "_check_sentinel", lambda *args, **kwargs: None)

    ok, password = engine._register_password()

    assert ok is False
    assert password is None
    assert len(calls) == 1
    assert engine._step_error_code == "phone_number_rejected"


def test_email_then_phone_verifies_without_creating_another_account(monkeypatch):
    class Callback:
        def __init__(self):
            self.values = iter(["+56911112222", "482913"])
            self.completed = False
            self.send_succeeded = 0

        def __call__(self):
            return next(self.values)

        def set_cancel_check(self, callback):
            pass

        def set_resend_callback(self, callback):
            self.resend_callback = callback

        def mark_send_succeeded(self):
            self.send_succeeded += 1

        def report_success(self):
            self.completed = True

    callback = Callback()
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=object(),
        phone_callback=callback,
        log_fn=lambda message: None,
    )
    engine = SimpleNamespace(
        _password_continue_url="",
        _password_next_payload={},
        _otp_page_type="sign_in_with_chatgpt_codex_consent",
        _step_error_message="",
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (engine, "https://auth.openai.com/add-phone", {}),
    )
    monkeypatch.setattr(worker, "_send_add_phone_number", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "_complete_codex_oauth",
        lambda _engine: {
            "access_token": "codex-access",
            "refresh_token": "codex-refresh",
            "id_token": "codex-id",
        },
    )

    result = worker.run_for_account(email="new@example.com", password="Secret123!")

    assert result == {
        "ok": True,
        "already_verified": False,
        "phone_number": "+56911112222",
        "page_type": "sign_in_with_chatgpt_codex_consent",
        "access_token": "codex-access",
        "refresh_token": "codex-refresh",
        "id_token": "codex-id",
    }
    assert callback.completed is True
    assert callback.send_succeeded == 1


def test_email_then_phone_keeps_verified_account_when_codex_callback_has_no_tokens(monkeypatch):
    class Callback:
        def __init__(self):
            self.values = iter(["+573001234567", "482913"])
            self.completed = False
            self.cleanup_count = 0

        def __call__(self):
            return next(self.values)

        def set_cancel_check(self, callback):
            pass

        def set_resend_callback(self, callback):
            pass

        def mark_send_succeeded(self):
            pass

        def report_success(self):
            self.completed = True

        def cleanup(self):
            self.cleanup_count += 1

    logs = []
    callback = Callback()
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=object(),
        phone_callback=callback,
        require_codex_refresh_token=False,
        log_fn=lambda message, **kwargs: logs.append((message, kwargs.get("level", "info"))),
    )
    worker._codex_oauth_start = object()
    engine = SimpleNamespace(
        _password_continue_url="",
        _password_next_payload={},
        _otp_page_type="contact_verification",
        _step_error_message="",
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (engine, "https://auth.openai.com/add-phone", {}),
    )
    monkeypatch.setattr(worker, "_send_add_phone_number", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "_complete_codex_oauth", lambda engine: {})

    result = worker.run_for_account(email="new@example.com", password="Secret123!")

    assert result["ok"] is True
    assert result["phone_number"] == "+573001234567"
    assert callback.completed is True
    assert callback.cleanup_count == 1
    assert any(level == "warning" and "保留当前 Free 账号" in message for message, level in logs)


def test_email_then_phone_does_not_rent_when_remote_is_already_verified(monkeypatch):
    class Callback:
        completed = False

        def __call__(self):
            raise AssertionError("already verified account must not rent a number")

        def set_cancel_check(self, callback):
            pass

    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=object(),
        phone_callback=Callback(),
        require_codex_refresh_token=False,
        log_fn=lambda message: None,
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (object(), "", {"already_verified": True}),
    )

    result = worker.run_for_account(email="new@example.com", password="Secret123!")

    assert result["ok"] is True
    assert result["already_verified"] is True
    assert result["phone_number"] == ""


def test_email_then_phone_releases_rejected_number_before_retry(monkeypatch):
    class Callback:
        def __init__(self):
            self.values = iter(["+56911110001", "+56911110002", "739201"])
            self.phase = "need_number"
            self.activation = object()
            self.completed = False
            self.cleanup_count = 0
            self.failures = []

        def __call__(self):
            return next(self.values)

        def set_cancel_check(self, callback):
            pass

        def set_resend_callback(self, callback):
            pass

        def mark_send_failed(self, reason=""):
            self.failures.append(reason)

        def mark_send_succeeded(self):
            pass

        def cleanup(self):
            self.cleanup_count += 1

        def report_success(self):
            self.completed = True

    callback = Callback()
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=object(),
        phone_callback=callback,
        max_phone_attempts=2,
        require_codex_refresh_token=False,
        log_fn=lambda message: None,
    )
    engine = SimpleNamespace(
        _password_continue_url="",
        _password_next_payload={},
        _otp_page_type="consent",
        _step_error_message="number rejected",
    )
    monkeypatch.setattr(
        worker,
        "_open_phone_challenge",
        lambda **kwargs: (engine, "https://auth.openai.com/add-phone", {}),
    )
    send_calls = []

    def send_phone_otp(*args, **kwargs):
        send_calls.append(args[1])
        return len(send_calls) == 2

    monkeypatch.setattr(worker, "_send_add_phone_number", send_phone_otp)
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args, **kwargs: True)

    result = worker.run_for_account(email="new@example.com", password="Secret123!")

    assert result["phone_number"] == "+56911110002"
    assert send_calls == ["+56911110001", "+56911110002"]
    assert callback.cleanup_count == 2
    assert callback.failures == ["number rejected"]


def test_email_then_phone_rebuilds_challenge_after_sms_timeout(monkeypatch):
    class Callback:
        def __init__(self):
            self.values = iter(["+491511111111", "", "+491522222222", "482913"])
            self.phase = "need_number"
            self.activation = object()
            self.completed = False
            self.cleanup_count = 0
            self.failures = []

        def __call__(self):
            return next(self.values)

        def set_cancel_check(self, callback):
            pass

        def set_resend_callback(self, callback):
            self.resend_callback = callback

        def mark_send_succeeded(self):
            pass

        def mark_send_failed(self, reason=""):
            self.failures.append(reason)

        def cleanup(self):
            self.cleanup_count += 1

        def report_success(self):
            self.completed = True

    def make_engine(index):
        return SimpleNamespace(
            index=index,
            _password_continue_url="",
            _password_next_payload={},
            _otp_page_type="add_phone",
            _otp_continue_url="",
            _step_error_code="",
            _step_error_message="",
        )

    callback = Callback()
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=object(),
        phone_callback=callback,
        max_phone_attempts=2,
        log_fn=lambda message: None,
    )
    engines = [make_engine(1), make_engine(2)]
    open_calls = []

    def open_challenge(**kwargs):
        open_calls.append(kwargs)
        engine = engines[len(open_calls) - 1]
        return engine, "https://auth.openai.com/add-phone", {}

    monkeypatch.setattr(worker, "_open_phone_challenge", open_challenge)
    send_calls = []

    def send_add_phone(engine, phone_number, page_url):
        send_calls.append((engine, phone_number))
        return True

    monkeypatch.setattr(worker, "_send_add_phone_number", send_add_phone)
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "_complete_codex_oauth",
        lambda _engine: {
            "access_token": "codex-access",
            "refresh_token": "codex-refresh",
            "id_token": "codex-id",
        },
    )

    result = worker.run_for_account(email="new@example.com", password="Secret123!")

    assert result["ok"] is True
    assert result["phone_number"] == "+491522222222"
    assert len(open_calls) == 2
    assert send_calls == [(engines[0], "+491511111111"), (engines[1], "+491522222222")]
    assert callback.cleanup_count == 2
    assert len(callback.failures) == 1
    assert callback.completed is True


def test_email_then_phone_rebuilds_challenge_after_invalid_auth_step(monkeypatch):
    class Callback:
        def __init__(self):
            self.values = iter(["+491533333333", "+491544444444", "482913"])
            self.phase = "need_number"
            self.activation = object()
            self.completed = False
            self.cleanup_count = 0

        def __call__(self):
            return next(self.values)

        def set_cancel_check(self, callback):
            pass

        def set_resend_callback(self, callback):
            self.resend_callback = callback

        def mark_send_succeeded(self):
            pass

        def mark_send_failed(self, reason=""):
            self.failure_reason = reason

        def cleanup(self):
            self.cleanup_count += 1

        def report_success(self):
            self.completed = True

    def make_engine(index):
        return SimpleNamespace(
            index=index,
            _password_continue_url="",
            _password_next_payload={},
            _otp_page_type="add_phone",
            _otp_continue_url="",
            _step_error_code="",
            _step_error_message="",
        )

    callback = Callback()
    worker = ChatGPTProtocolEmailThenPhoneWorker(
        email_service=object(),
        phone_callback=callback,
        max_phone_attempts=2,
        log_fn=lambda message: None,
    )
    engines = [make_engine(1), make_engine(2)]
    open_calls = []

    def open_challenge(**kwargs):
        open_calls.append(kwargs)
        engine = engines[len(open_calls) - 1]
        return engine, "https://auth.openai.com/add-phone", {}

    monkeypatch.setattr(worker, "_open_phone_challenge", open_challenge)
    send_calls = []

    def send_add_phone(engine, phone_number, page_url):
        send_calls.append((engine, phone_number))
        if len(send_calls) == 1:
            engine._step_error_code = "phone_authorization_invalid"
            engine._step_error_message = "Invalid authorization step."
            return False
        return True

    monkeypatch.setattr(worker, "_send_add_phone_number", send_add_phone)
    monkeypatch.setattr(worker, "_validate_phone_otp", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        worker,
        "_complete_codex_oauth",
        lambda _engine: {
            "access_token": "codex-access",
            "refresh_token": "codex-refresh",
            "id_token": "codex-id",
        },
    )

    result = worker.run_for_account(email="new@example.com", password="Secret123!")

    assert result["ok"] is True
    assert result["phone_number"] == "+491544444444"
    assert len(open_calls) == 2
    assert send_calls == [(engines[0], "+491533333333"), (engines[1], "+491544444444")]
    assert callback.cleanup_count == 2
    assert callback.failure_reason == "Invalid authorization step."
    assert callback.completed is True
