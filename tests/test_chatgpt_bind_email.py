from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from core.base_mailbox import MailboxAccount
from core.base_platform import Account, RegisterConfig
from core.account_display import build_account_display_summary
from domain.actions import ActionExecutionCommand
from infrastructure import platform_runtime as runtime_module
from platforms.chatgpt import bind_email as bind_email_module
from platforms.chatgpt.bind_email import ChatGPTBindEmailError, ChatGPTProtocolBindEmailWorker
from platforms.chatgpt.constants import CODEX_SCOPE
from platforms.chatgpt.plugin import ChatGPTPlatform


class _Response:
    def __init__(self, status_code=200, payload=None, url="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""
        self.url = url
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Cookies:
    def __init__(self):
        self.values = {"oai-did": "did-1"}

    def get(self, key, default=""):
        if key == "__Secure-next-auth.session-token":
            return "new-session-token"
        return self.values.get(key, default)

    def set(self, key, value, **kwargs):
        self.values[key] = value

    def items(self):
        return self.values.items()


def test_bind_email_clears_only_auth_transaction_cookies():
    class Cookie:
        def __init__(self, name, domain="auth.openai.com", path="/"):
            self.name = name
            self.domain = domain
            self.path = path

    class Jar(list):
        def clear(self, domain, path, name):
            for cookie in list(self):
                if (cookie.domain, cookie.path, cookie.name) == (domain, path, name):
                    self.remove(cookie)
                    return
            raise KeyError(name)

    jar = Jar([
        Cookie("login_session"),
        Cookie("oai-client-auth-session"),
        Cookie("oai-client-auth-session.sig"),
        Cookie("auth-session-minimized"),
        Cookie("__cf_bm"),
        Cookie("oai-did"),
        Cookie("__Secure-next-auth.session-token", domain="chatgpt.com"),
    ])
    engine = SimpleNamespace(session=SimpleNamespace(cookies=SimpleNamespace(jar=jar)))
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        log_fn=lambda _message: None,
    )

    worker._clear_auth_transaction_cookies(engine)

    assert [cookie.name for cookie in jar] == [
        "__cf_bm",
        "oai-did",
        "__Secure-next-auth.session-token",
    ]


class _Session:
    def __init__(self, *, email_in_use=False):
        self.cookies = _Cookies()
        self.email_in_use = email_in_use
        self.gets = []
        self.posts = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if "/oauth/authorize" in str(url):
            return _Response(200, {}, url="https://auth.openai.com/log-in/password")
        if str(url).endswith("/api/auth/session"):
            return _Response(200, {
                "accessToken": "new-access-token",
                "idToken": "new-id-token",
                "user": {"email": "bound@example.com"},
            })
        return _Response(200, {})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if str(url).endswith("/password/verify"):
            return _Response(200, {
                "continue_url": "https://auth.openai.com/add-email",
                "page": {"type": "add_email"},
            })
        if str(url).endswith("/add-email/send"):
            if self.email_in_use:
                return _Response(400, {
                    "error": {"code": "email_in_use", "message": "Email already exists"},
                })
            return _Response(200, {
                "continue_url": "https://auth.openai.com/email-verification",
                "page": {"type": "email_otp_verification"},
            })
        raise AssertionError(url)


class _Engine:
    email_in_use = False
    last_session = None

    def __init__(self, **kwargs):
        self.session = _Session(email_in_use=self.email_in_use)
        type(self).last_session = self.session
        self._device_id = "did-1"
        self._authorize_final_url = "https://auth.openai.com/log-in/password"
        self._email_otp_continue_url = ""
        self._otp_continue_url = ""
        self._step_error_message = ""

    def _init_session(self):
        return True

    def _start_oauth(self):
        return True

    def _get_device_id(self):
        return self._device_id

    def _check_sentinel(self, device_id, flow):
        return None

    def _send_verification_code(self):
        return True

    def _validate_verification_code(self, code):
        assert code == "123456"
        self._otp_continue_url = "https://chatgpt.com/api/auth/callback/openai?code=ok"
        return True


class _Mailbox:
    def __init__(self):
        self.wait_calls = 0

    def get_current_ids(self, account):
        return {"old-message"}

    def wait_for_code(self, account, **kwargs):
        self.wait_calls += 1
        assert kwargs["before_ids"] == {"old-message"}
        return "123456"


def test_protocol_bind_email_completes_add_email_and_refreshes_session(monkeypatch):
    monkeypatch.setattr(bind_email_module, "RegistrationEngine", _Engine)
    mailbox = _Mailbox()
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=mailbox,
        mailbox_account=MailboxAccount(email="bound@example.com"),
        otp_timeout_seconds=120,
        log_fn=lambda _message: None,
        existing_access_token="old-access-token",
    )

    result = worker.run()

    assert result["email"] == "bound@example.com"
    assert result["access_token"] == "new-access-token"
    assert "refresh_token" not in result
    assert result["session_token"] == "new-session-token"
    assert result["session_refreshed"] is True
    assert mailbox.wait_calls == 1
    assert any("/oauth/authorize" in str(url) for url, _ in _Engine.last_session.gets)
    oauth_url = next(str(url) for url, _ in _Engine.last_session.gets if "/oauth/authorize" in str(url))
    assert parse_qs(urlsplit(oauth_url).query)["scope"] == [CODEX_SCOPE]
    password_url, password_request = _Engine.last_session.posts[0]
    assert str(password_url).endswith("/password/verify")
    assert set(__import__("json").loads(password_request["data"])) == {"password"}
    add_email_url, add_email_request = _Engine.last_session.posts[1]
    assert str(add_email_url).endswith("/add-email/send")
    assert __import__("json").loads(add_email_request["data"]) == {"email": "bound@example.com"}


def test_protocol_bind_email_submits_phone_identifier_for_codex_login(monkeypatch):
    class PhoneEntrySession(_Session):
        def get(self, url, **kwargs):
            if "/oauth/authorize" in str(url):
                self.gets.append((url, kwargs))
                return _Response(200, {}, url="https://auth.openai.com/log-in")
            return super().get(url, **kwargs)

        def post(self, url, **kwargs):
            if str(url).endswith("/authorize/continue"):
                self.posts.append((url, kwargs))
                return _Response(200, {
                    "continue_url": "https://auth.openai.com/log-in/password",
                    "page": {"type": "login_password"},
                })
            return super().post(url, **kwargs)

    class PhoneEntryEngine(_Engine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session = PhoneEntrySession()
            type(self).last_session = self.session

    monkeypatch.setattr(bind_email_module, "RegistrationEngine", PhoneEntryEngine)
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="bound@example.com"),
        log_fn=lambda _message: None,
    )

    result = worker.run()

    assert result["email"] == "bound@example.com"
    identifier_url, identifier_request = PhoneEntryEngine.last_session.posts[0]
    assert str(identifier_url).endswith("/authorize/continue")
    identifier_body = __import__("json").loads(identifier_request["data"])
    assert identifier_body == {
        "username": {"value": "+56911112222", "kind": "phone_number"},
        "screen_hint": "login",
    }
    assert str(PhoneEntryEngine.last_session.posts[1][0]).endswith("/password/verify")


def test_protocol_bind_email_stops_before_workspace_selection(monkeypatch):
    monkeypatch.setattr(bind_email_module, "RegistrationEngine", _Engine)
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="bound@example.com"),
        log_fn=lambda _message: None,
        existing_access_token="old-access-token",
    )

    result = worker.run()

    assert result["email"] == "bound@example.com"
    assert result["access_token"] == "new-access-token"
    assert all(
        "workspace/select" not in str(url)
        for url, _request in _Engine.last_session.posts
    )


def test_protocol_bind_email_retries_transient_proxy_connect(monkeypatch):
    class FlakySession(_Session):
        def __init__(self):
            super().__init__()
            self.oauth_attempts = 0

        def get(self, url, **kwargs):
            if "/oauth/authorize" in str(url):
                self.oauth_attempts += 1
                if self.oauth_attempts == 1:
                    raise RuntimeError("curl: (56) Proxy CONNECT aborted")
            return super().get(url, **kwargs)

    class FlakyEngine(_Engine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session = FlakySession()
            type(self).last_session = self.session

    monkeypatch.setattr(bind_email_module, "RegistrationEngine", FlakyEngine)
    monkeypatch.setattr(bind_email_module.time, "sleep", lambda _seconds: None)
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="bound@example.com"),
        log_fn=lambda _message: None,
    )

    result = worker.run()

    assert result["email"] == "bound@example.com"
    assert FlakyEngine.last_session.oauth_attempts == 2


def test_protocol_bind_email_retries_transient_mailbox_error(monkeypatch):
    class FlakyMailbox(_Mailbox):
        def wait_for_code(self, account, **kwargs):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise RuntimeError("Connection reset by peer")
            return "123456"

    messages = []
    mailbox = FlakyMailbox()
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=mailbox,
        mailbox_account=MailboxAccount(email="bound@example.com"),
        otp_timeout_seconds=120,
        log_fn=messages.append,
    )
    monkeypatch.setattr(bind_email_module.time, "sleep", lambda _seconds: None)

    assert worker._wait_for_email_code({"old-message"}) == "123456"
    assert mailbox.wait_calls == 2
    assert any("邮箱取码网络中断，继续轮询" in message for message in messages)


def test_protocol_bind_email_rotates_711_session_after_repeated_connect_failures(monkeypatch):
    old_proxy = "http://user-region-CL-session-old-sessTime-180:pass@global.rotgb.711proxy.com:10000"
    fresh_proxy = "http://user-region-CL-session-fresh-sessTime-180:pass@global.rotgb.711proxy.com:10000"

    class BrokenSession(_Session):
        def get(self, url, **kwargs):
            if "/oauth/authorize" in str(url):
                raise RuntimeError("curl: (56) Proxy CONNECT aborted")
            return super().get(url, **kwargs)

    class RotatingEngine(_Engine):
        last_engine = None

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session = BrokenSession()
            self.proxy_url = old_proxy
            self.reset_count = 0
            type(self).last_engine = self

        def _reset_http_session(self):
            self.reset_count += 1
            self.session = _Session()
            type(self).last_session = self.session
            return True

    monkeypatch.setattr(bind_email_module, "RegistrationEngine", RotatingEngine)
    monkeypatch.setattr(bind_email_module, "infer_proxy_region", lambda _proxy: "CL")
    monkeypatch.setattr(bind_email_module, "pin_711proxy_session", lambda *args, **kwargs: fresh_proxy)
    monkeypatch.setattr(bind_email_module.time, "sleep", lambda _seconds: None)
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="bound@example.com"),
        proxy_url=old_proxy,
        log_fn=lambda _message: None,
    )

    result = worker.run()

    assert result["email"] == "bound@example.com"
    assert result["auth_proxy_url"] == fresh_proxy
    assert RotatingEngine.last_engine.reset_count == 1


def test_protocol_bind_email_reuses_phone_registration_engine(monkeypatch):
    engine = _Engine()
    engine._create_account_continue_url = "https://chatgpt.com/api/auth/callback/openai?code=ok"
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="bound@example.com"),
        otp_timeout_seconds=120,
        log_fn=lambda _message: None,
    )
    monkeypatch.setattr(
        bind_email_module,
        "RegistrationEngine",
        lambda **kwargs: pytest.fail("bind_with_engine must not create another engine"),
    )

    result = worker.bind_with_engine(engine)

    assert result["email"] == "bound@example.com"
    assert len([url for url, _ in engine.session.posts if str(url).endswith("/password/verify")]) == 1
    assert len([url for url, _ in engine.session.posts if str(url).endswith("/add-email/send")]) == 1


def test_page_state_reads_external_url_from_nested_page_payload():
    response = _Response(200, {
        "page": {
            "type": "external_url",
            "payload": {"url": "https://auth.openai.com/add-email"},
        },
    })

    page_type, continue_url, _ = bind_email_module._page_state(response)

    assert page_type == "external_url"
    assert continue_url == "https://auth.openai.com/add-email"


def test_protocol_bind_email_follows_external_url_before_add_email(monkeypatch):
    class ExternalSession(_Session):
        def get(self, url, **kwargs):
            if str(url).endswith("/add-email"):
                return _Response(
                    200,
                    {"page": {"type": "add_email"}},
                    url="https://auth.openai.com/add-email",
                )
            return super().get(url, **kwargs)

        def post(self, url, **kwargs):
            if str(url).endswith("/password/verify"):
                self.posts.append((url, kwargs))
                return _Response(200, {
                    "page": {
                        "type": "external_url",
                        "payload": {"url": "https://auth.openai.com/add-email"},
                    },
                })
            return super().post(url, **kwargs)

    class ExternalEngine(_Engine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session = ExternalSession()
            type(self).last_session = self.session

    monkeypatch.setattr(bind_email_module, "RegistrationEngine", ExternalEngine)
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="bound@example.com"),
        otp_timeout_seconds=120,
        log_fn=lambda _message: None,
    )

    result = worker.run()

    assert result["email"] == "bound@example.com"
    assert any(str(url).endswith("/add-email/send") for url, _ in ExternalEngine.last_session.posts)


def test_protocol_bind_email_classifies_email_in_use(monkeypatch):
    class EmailInUseEngine(_Engine):
        email_in_use = True

    monkeypatch.setattr(bind_email_module, "RegistrationEngine", EmailInUseEngine)
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        mailbox_account=MailboxAccount(email="bound@example.com"),
        log_fn=lambda _message: None,
    )

    with pytest.raises(ChatGPTBindEmailError, match="邮箱已绑定其他账号"):
        worker.run()


def test_protocol_bind_email_allocates_mailbox_only_after_phone_login(monkeypatch):
    class FailedPasswordSession(_Session):
        def post(self, url, **kwargs):
            if str(url).endswith("/password/verify"):
                return _Response(403, {
                    "error": {"code": "account_deactivated", "message": "Account deactivated"},
                })
            return super().post(url, **kwargs)

    class FailedPasswordEngine(_Engine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session = FailedPasswordSession()

    class LazyMailbox(_Mailbox):
        def __init__(self):
            super().__init__()
            self.allocations = 0

        def get_email(self):
            self.allocations += 1
            return MailboxAccount(email="unused@example.com")

    monkeypatch.setattr(bind_email_module, "RegistrationEngine", FailedPasswordEngine)
    mailbox = LazyMailbox()
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=mailbox,
        log_fn=lambda _message: None,
    )

    with pytest.raises(ChatGPTBindEmailError, match="Account deactivated"):
        worker.run()
    assert mailbox.allocations == 0


def test_protocol_bind_email_logs_deactivated_phone_account(monkeypatch):
    class DeactivatedSession(_Session):
        def post(self, url, **kwargs):
            if str(url).endswith("/password/verify"):
                return _Response(403, {
                    "error": {
                        "code": "account_deactivated",
                        "message": "Account deleted or deactivated",
                    },
                })
            return super().post(url, **kwargs)

    class DeactivatedEngine(_Engine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session = DeactivatedSession()

    messages = []
    monkeypatch.setattr(bind_email_module, "RegistrationEngine", DeactivatedEngine)
    worker = ChatGPTProtocolBindEmailWorker(
        phone_number="+56911112222",
        password="Secret123!",
        mailbox=_Mailbox(),
        log_fn=messages.append,
    )

    with pytest.raises(ChatGPTBindEmailError, match="deleted or deactivated"):
        worker.run()

    assert any("account_deactivated" in message for message in messages)


def test_bind_email_action_returns_private_persistence_payload(monkeypatch):
    mailbox_account = MailboxAccount(
        email="bound@example.com",
        account_id="mailbox-1",
        extra={
            "provider_account": {
                "provider_type": "mailbox",
                "provider_name": "local_ms_pool",
                "login_identifier": "bound@example.com",
                "credentials": {"password": "mail-secret"},
            },
            "provider_resource": {
                "provider_type": "mailbox",
                "provider_name": "local_ms_pool",
                "resource_type": "mailbox",
                "resource_identifier": "mailbox-1",
                "handle": "bound@example.com",
            },
        },
    )
    mailbox = SimpleNamespace(get_email=lambda: mailbox_account)

    class FakeSettingsRepository:
        def get_default_provider_key(self, provider_type):
            return "local_ms_pool"

        def get_by_key(self, provider_type, provider_key):
            return SimpleNamespace(enabled=True)

        def resolve_runtime_settings(self, provider_type, provider_key, overrides):
            assert overrides["mail_provider_strict"] is True
            return dict(overrides)

    class FakeWorker:
        def __init__(self, **kwargs):
            self.mailbox_account = mailbox.get_email()

        def run(self):
            return {
                "email": "bound@example.com",
                "access_token": "new-access-token",
                "session_token": "new-session-token",
                "session_refreshed": True,
            }

    import core.base_mailbox as base_mailbox_module
    import infrastructure.provider_settings_repository as settings_module

    monkeypatch.setattr(settings_module, "ProviderSettingsRepository", FakeSettingsRepository)
    create_calls = []

    def fake_create_mailbox(*args, **kwargs):
        create_calls.append((args, kwargs))
        return mailbox

    monkeypatch.setattr(base_mailbox_module, "create_mailbox", fake_create_mailbox)
    monkeypatch.setattr(bind_email_module, "ChatGPTProtocolBindEmailWorker", FakeWorker)
    monkeypatch.setattr("platforms.chatgpt.plugin.proxy_pool.get_next", lambda region="": None)

    platform = ChatGPTPlatform(RegisterConfig())
    result = platform.execute_action(
        "bind_email",
        Account(
            platform="chatgpt",
            email="phone:+56911112222",
            password="Secret123!",
            extra={
                "phone_number": "+56911112222",
                "access_token": "old-access-token",
                "account_overview": {"validity_status": "invalid"},
            },
        ),
        {"mailbox_provider": "local_ms_pool", "otp_timeout_seconds": "120"},
    )

    assert result["ok"] is True
    assert result["data"]["email"] == "bound@example.com"
    assert "credentials" not in result["data"]
    assert result["_persist"]["credentials"]["access_token"] == "new-access-token"
    assert result["_persist"]["provider_resources"][0]["handle"] == "bound@example.com"
    assert create_calls[0][1]["proxy"] is None


def test_bind_email_action_rejects_email_account():
    platform = ChatGPTPlatform(RegisterConfig())
    result = platform.execute_action(
        "bind_email",
        Account(platform="chatgpt", email="existing@example.com", password="Secret123!"),
        {},
    )
    assert result == {"ok": False, "error": "该操作只适用于手机号注册账号"}


def test_platform_runtime_persists_bound_email_without_exposing_mailbox_credentials(monkeypatch):
    model = SimpleNamespace(
        id=9,
        platform="chatgpt",
        email="phone:+56911112222",
        updated_at=None,
    )
    committed = []
    patch_args = {}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_type, account_id):
            return model

        def add(self, item):
            pass

        def commit(self):
            committed.append(True)

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def set_logger(self, log_fn):
            pass

        def set_cancel_checker(self, cancel_check):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": True,
                "data": {"message": "邮箱添加并验证成功", "email": "bound@example.com"},
                "_persist": {
                    "account_email": "bound@example.com",
                    "summary_updates": {"phone_number": "+56911112222"},
                    "credentials": {"access_token": "secret-access-token"},
                    "provider_accounts": [{
                        "provider_type": "mailbox",
                        "provider_name": "local_ms_pool",
                        "login_identifier": "bound@example.com",
                        "credentials": {"password": "mail-secret"},
                    }],
                    "provider_resources": [{
                        "provider_type": "mailbox",
                        "provider_name": "local_ms_pool",
                        "resource_type": "mailbox",
                        "resource_identifier": "mailbox-1",
                        "handle": "bound@example.com",
                    }],
                },
            }

    monkeypatch.setattr(runtime_module, "Session", lambda _engine: FakeSession())
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(
        runtime_module,
        "build_platform_account",
        lambda session, account_model: Account(
            platform="chatgpt",
            email=account_model.email,
            password="Secret123!",
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "patch_account_graph",
        lambda session, account_model, **kwargs: patch_args.update(kwargs),
    )

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=9,
            action_id="bind_email",
            params={},
        )
    )

    assert result.ok is True
    assert result.data == {"message": "邮箱添加并验证成功", "email": "bound@example.com"}
    assert model.email == "bound@example.com"
    assert patch_args["credential_updates"] == {"access_token": "secret-access-token"}
    assert patch_args["provider_resources"][0]["handle"] == "bound@example.com"
    assert committed


def test_bound_email_account_detail_keeps_original_phone_number():
    summary = build_account_display_summary(
        platform="chatgpt",
        email="bound@example.com",
        lifecycle_status="registered",
        validity_status="unknown",
        plan_state="unknown",
        plan_name="",
        display_status="registered",
        overview={"phone_number": "+56911112222"},
        provider_resources=[],
    )
    assert any(
        metric["key"] == "phone_number" and metric["value"] == "+56911112222"
        for metric in summary["secondary_metrics"]
    )
