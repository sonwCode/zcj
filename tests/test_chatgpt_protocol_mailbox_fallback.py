from __future__ import annotations



import json
import pytest
from types import SimpleNamespace



from platforms.chatgpt.register import RegistrationResult as ProtocolRegistrationResult





class _MailboxAccount:

    email = "new@example.com"

    account_id = "mailbox-1"





class _Mailbox:

    def __init__(self):

        self.before_ids_seen = None



    def get_current_ids(self, account):

        assert account is not None

        return {"old-message"}



    def wait_for_code(self, account, keyword="", timeout=600, code_pattern=None, before_ids=None):

        assert account is not None

        self.before_ids_seen = before_ids

        return "123456"


def test_protocol_mailbox_adapter_reuses_proxy_preflight_country(monkeypatch):
    import platforms.chatgpt.protocol_mailbox as protocol_mailbox
    from platforms.chatgpt.plugin import ChatGPTPlatform

    engine_kwargs = []

    class FakeEngine:
        def __init__(self, **kwargs):
            engine_kwargs.append(kwargs)

    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)
    platform = ChatGPTPlatform()
    platform.mailbox = _Mailbox()
    ctx = SimpleNamespace(
        identity=SimpleNamespace(mailbox_account=_MailboxAccount()),
        proxy="http://proxy.local",
        extra={"proxy_route_country": "co"},
        log=lambda _message: None,
        platform=SimpleNamespace(is_cancel_requested=lambda: False),
    )

    platform.build_protocol_mailbox_adapter().worker_builder(ctx, SimpleNamespace())

    assert engine_kwargs[0]["proxy_url"] == "http://proxy.local"
    assert engine_kwargs[0]["preflight_location"] == "CO"





def test_protocol_mailbox_retries_invalid_state_with_same_mailbox(monkeypatch):
    import platforms.chatgpt.protocol_mailbox as protocol_mailbox



    logs = []

    mailbox = _Mailbox()



    engine_count = 0

    class FakeEngine:

        def __init__(self, **kwargs):

            nonlocal engine_count

            engine_count += 1

            self.email = ""

            self.password = ""



        def run(self):

            if engine_count == 2:

                return ProtocolRegistrationResult(

                    success=True,

                    email=self.email,

                    password=self.password,

                    account_id="acct_123",

                    access_token="access-token",

                )

            return ProtocolRegistrationResult(

                success=False,

                email=self.email,

                password=self.password,

                error_message="OAuth state expired",

                error_code="oauth_invalid_state",

            )



    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)

    worker = protocol_mailbox.ChatGPTProtocolMailboxWorker(

        mailbox=mailbox,

        mailbox_account=_MailboxAccount(),

        provider="cfworker_admin_api",

        proxy_url="http://proxy.local",

        log_fn=logs.append,

    )



    result = worker.run(email="new@example.com", password="Secret123!")



    assert result.success is True

    assert result.account_id == "acct_123"

    assert result.access_token == "access-token"

    assert engine_count == 2

    assert any("同邮箱重建会话" in line for line in logs)





def test_protocol_mailbox_keeps_non_otp_errors(monkeypatch):

    import platforms.chatgpt.protocol_mailbox as protocol_mailbox



    class FakeEngine:

        def __init__(self, **kwargs):

            self.email = ""

            self.password = ""



        def run(self):

            return ProtocolRegistrationResult(

                success=False,

                email=self.email,

                password=self.password,

                error_message="IP 位置不支持",

                error_code="unsupported_region",

            )



    monkeypatch.setattr(protocol_mailbox, "RegistrationEngine", FakeEngine)



    worker = protocol_mailbox.ChatGPTProtocolMailboxWorker(

        mailbox=_Mailbox(),

        mailbox_account=_MailboxAccount(),

        provider="cfworker_admin_api",

        log_fn=lambda message: None,

    )



    with pytest.raises(protocol_mailbox.ChatGPTProtocolRegistrationError, match="IP 位置不支持") as exc_info:

        worker.run(email="new@example.com", password="Secret123!")

    assert exc_info.value.code == "unsupported_region"

    assert exc_info.value.proxy_failure is True





def test_protocol_mailbox_mapper_preserves_protocol_metadata():

    from platforms.chatgpt.plugin import ChatGPTPlatform



    class Ctx:

        password = "Secret123!"



    result = ProtocolRegistrationResult(

        success=True,

        email="new@example.com",

        password="Secret123!",

        account_id="acct_123",

        workspace_id="ws_123",

        access_token="access-token",

        refresh_token="refresh-token",

        id_token="id-token",

        session_token="session-token",

        metadata={

            "cookies": "session=abc",

            "profile": {"email": "new@example.com"},

            "expires_at": "2026-05-20T00:00:00Z",

        },

    )



    mapped = ChatGPTPlatform().build_protocol_mailbox_adapter().result_mapper(Ctx(), result)



    assert mapped.extra["cookies"] == "session=abc"

    assert mapped.extra["profile"] == {"email": "new@example.com"}

    assert mapped.extra["expires_at"] == "2026-05-20T00:00:00Z"


def test_protocol_mailbox_mapper_persists_followup_auth_context():
    from platforms.chatgpt.plugin import ChatGPTPlatform

    class Ctx:
        password = "Secret123!"

    class Cookies:
        jar = None

        @staticmethod
        def items():
            return [("oai-did", "device-cookie"), ("login_session", "login-cookie")]

    platform = ChatGPTPlatform()
    platform._last_protocol_mailbox_worker = SimpleNamespace(
        engine=SimpleNamespace(
            session=SimpleNamespace(cookies=Cookies()),
            _device_id="stable-device-id",
        )
    )
    result = ProtocolRegistrationResult(
        success=True,
        email="new@example.com",
        password="Secret123!",
        account_id="acct_123",
        access_token="access-token",
        session_token="session-token",
        metadata={},
    )

    mapped = platform.build_protocol_mailbox_adapter().result_mapper(Ctx(), result)

    records = json.loads(mapped.extra["auth_cookies"])
    assert {item["name"] for item in records} == {"oai-did", "login_session"}
    assert mapped.extra["oai_device_id"] == "stable-device-id"
    assert "login_session=login-cookie" in mapped.extra["cookies"]


def test_mailbox_service_resets_message_baseline_for_post_registration_otp():
    from platforms.chatgpt.protocol_mailbox import _MailboxEmailService

    class Mailbox:
        def __init__(self):
            self.calls = 0

        def get_current_ids(self, account):
            self.calls += 1
            return {f"message-{self.calls}"}

    mailbox = Mailbox()
    service = _MailboxEmailService(
        mailbox=mailbox,
        mailbox_account=_MailboxAccount(),
        provider="local_ms_pool",
    )
    service.create_email()
    assert service._before_ids == {"message-1"}

    service.begin_new_otp_wait()

    assert service._before_ids == {"message-2"}

