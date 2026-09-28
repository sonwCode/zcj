"""Guards and callback builders shared by the registration flows.

``helpers.py`` is where a flow states what it needs before it spends anything: an email, a
mailbox, a reusable OAuth browser, an executor type the platform actually supports. Each
guard raises a typed error the flow can act on rather than failing later inside a vendor
call.

The OTP and link callbacks are the seam between a flow and a mailbox. Both stay inert
(returning ``None``) when the context cannot supply a mailbox at all, and the OTP one gains
an escape hatch: when the mailbox yields nothing, an operator can supply the code through
the manual broker instead of the run dying on a lease it already paid for.
"""
from __future__ import annotations

import pytest

from core.registration import helpers
from core.registration.errors import (
    BrowserReuseRequiredError,
    IdentityResolutionError,
    RegistrationUnsupportedError,
)
from core.registration.models import RegistrationContext


class _Config:
    def __init__(self, **kwargs):
        self.executor_type = kwargs.pop("executor_type", "protocol")
        self.proxy = kwargs.pop("proxy", None)
        self.extra = kwargs.pop("extra", {})


class _Identity:
    def __init__(self, **kwargs):
        self.email = kwargs.pop("email", "user@example.com")
        self.has_mailbox = kwargs.pop("has_mailbox", True)
        self.mailbox_account = kwargs.pop("mailbox_account", "acct")
        self.chrome_user_data_dir = kwargs.pop("chrome_user_data_dir", "")
        self.chrome_cdp_url = kwargs.pop("chrome_cdp_url", "")
        self.before_ids = kwargs.pop("before_ids", set())


class _Mailbox:
    def __init__(self, code="", link="", raises=None, current_ids=None):
        self.code = code
        self.link = link
        self.raises = raises
        self.current_ids = current_ids if current_ids is not None else {"seen"}
        self.calls: list[dict] = []

    def wait_for_code(self, account, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.code

    def get_current_ids(self, account):
        return self.current_ids

    def wait_for_link(self, account, **kwargs):
        self.calls.append(kwargs)
        return self.link


class _Platform:
    def __init__(self, mailbox=None):
        self.mailbox = mailbox


def make_ctx(*, mailbox=None, identity=None, config=None, platform=None, logs=None):
    recorded = logs if logs is not None else []
    return RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=platform if platform is not None else _Platform(mailbox),
        identity=identity if identity is not None else _Identity(),
        config=config if config is not None else _Config(),
        email=None,
        password=None,
        log_fn=recorded.append,
    )


# -- reusable OAuth browser ---------------------------------------------------


def test_a_user_data_dir_counts_as_reusable():
    assert helpers.has_reusable_oauth_browser(_Identity(chrome_user_data_dir="/tmp/profile")) is True


def test_a_cdp_url_counts_as_reusable():
    assert helpers.has_reusable_oauth_browser(_Identity(chrome_cdp_url="http://127.0.0.1:9222")) is True


def test_whitespace_is_not_a_reusable_browser():
    """A configured-but-empty value must not pass for a live session."""
    assert helpers.has_reusable_oauth_browser(_Identity(chrome_user_data_dir="   ")) is False
    assert helpers.has_reusable_oauth_browser(_Identity(chrome_cdp_url="  ")) is False


def test_an_identity_without_browser_fields_is_not_reusable():
    assert helpers.has_reusable_oauth_browser(object()) is False


def test_the_browser_reuse_guard_raises_for_a_bare_identity():
    with pytest.raises(BrowserReuseRequiredError):
        helpers.ensure_oauth_browser_reuse(make_ctx(identity=_Identity()), "需要复用浏览器")


def test_the_browser_reuse_guard_passes_with_a_session():
    helpers.ensure_oauth_browser_reuse(
        make_ctx(identity=_Identity(chrome_cdp_url="http://127.0.0.1:9222")), "需要复用浏览器"
    )


# -- timeout resolution -------------------------------------------------------


def test_the_first_configured_timeout_key_wins():
    assert helpers.resolve_timeout({"primary": 11, "secondary": 22}, ("primary", "secondary"), 99) == 11


def test_a_blank_key_falls_through_to_the_next():
    assert helpers.resolve_timeout({"primary": "", "secondary": 22}, ("primary", "secondary"), 99) == 22
    assert helpers.resolve_timeout({"primary": None, "secondary": 22}, ("primary", "secondary"), 99) == 22


def test_no_configured_key_yields_the_default():
    assert helpers.resolve_timeout({}, ("primary", "secondary"), 99) == 99


def test_a_configured_timeout_is_coerced_to_int():
    assert helpers.resolve_timeout({"primary": "17"}, ("primary",), 99) == 17


# -- identity guards ----------------------------------------------------------


def test_a_missing_email_is_rejected():
    with pytest.raises(IdentityResolutionError):
        helpers.ensure_identity_email(make_ctx(identity=_Identity(email="")), "缺少邮箱")


def test_a_present_email_passes():
    helpers.ensure_identity_email(make_ctx(), "缺少邮箱")


def test_a_missing_mailbox_is_rejected():
    with pytest.raises(IdentityResolutionError):
        helpers.ensure_mailbox_identity(make_ctx(identity=_Identity(has_mailbox=False)), "缺少邮箱")


def test_a_present_mailbox_passes():
    helpers.ensure_mailbox_identity(make_ctx(), "缺少邮箱")


# -- executor allow-list ------------------------------------------------------


def test_a_supported_executor_passes():
    helpers.ensure_oauth_executor_allowed(
        make_ctx(config=_Config(executor_type="browser")), ("browser", "protocol"), "不支持"
    )


def test_an_unsupported_executor_is_rejected():
    with pytest.raises(RegistrationUnsupportedError):
        helpers.ensure_oauth_executor_allowed(
            make_ctx(config=_Config(executor_type="protocol")), ("browser",), "不支持"
        )


def test_an_empty_allow_list_means_everything_is_allowed():
    """An unset capability must not read as "nothing is permitted"."""
    helpers.ensure_oauth_executor_allowed(make_ctx(), None, "不支持")
    helpers.ensure_oauth_executor_allowed(make_ctx(), (), "不支持")


def test_the_rejection_names_the_supported_executors():
    with pytest.raises(RegistrationUnsupportedError) as caught:
        helpers.ensure_oauth_executor_allowed(
            make_ctx(config=_Config(executor_type="protocol")), ("browser", "cdp"), None
        )
    assert "browser" in str(caught.value) and "cdp" in str(caught.value)


def test_the_executor_type_defaults_to_protocol():
    ctx = make_ctx(config=_Config())
    assert ctx.executor_type == "protocol"
    object.__setattr__(ctx.config, "executor_type", "")
    assert ctx.executor_type == "protocol"


# -- manual otp opt-in --------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on", "enabled"])
def test_the_manual_fallback_accepts_the_documented_spellings(value):
    assert helpers.manual_otp_enabled(make_ctx(config=_Config(extra={"manual_otp_fallback": value}))) is True


@pytest.mark.parametrize("value", ["", "   ", "0", "false", "off", "no", "maybe"])
def test_the_manual_fallback_refuses_everything_else(value):
    assert helpers.manual_otp_enabled(make_ctx(config=_Config(extra={"manual_otp_fallback": value}))) is False


def test_an_explicit_task_setting_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ZCJ_MANUAL_OTP", "1")
    ctx = make_ctx(config=_Config(extra={"manual_otp_fallback": "0"}))
    assert helpers.manual_otp_enabled(ctx) is False


def test_the_environment_is_only_read_when_the_task_is_silent(monkeypatch):
    """An absent key falls through to the global switch; an explicit blank does not."""
    monkeypatch.setenv("ZCJ_MANUAL_OTP", "1")
    assert helpers.manual_otp_enabled(make_ctx()) is True
    assert helpers.manual_otp_enabled(make_ctx(config=_Config(extra={"manual_otp_fallback": ""}))) is False


def test_the_manual_fallback_is_off_without_the_environment(monkeypatch):
    monkeypatch.delenv("ZCJ_MANUAL_OTP", raising=False)
    assert helpers.manual_otp_enabled(make_ctx()) is False


# -- otp callback -------------------------------------------------------------


def test_no_mailbox_means_no_callback():
    assert helpers.build_otp_callback(make_ctx(mailbox=None)) is None


def test_no_mailbox_account_means_no_callback():
    ctx = make_ctx(mailbox=_Mailbox(), identity=_Identity(mailbox_account=""))
    assert helpers.build_otp_callback(ctx) is None


def test_the_callback_returns_the_code_and_logs_it():
    logs: list[str] = []
    mailbox = _Mailbox(code="123456")
    ctx = make_ctx(mailbox=mailbox, logs=logs)
    code = helpers.build_otp_callback(ctx, success_label="验证码")()
    assert code == "123456"
    assert any("123456" in line for line in logs)


def test_the_callback_passes_the_seen_message_ids():
    seen = {"m1", "m2"}
    mailbox = _Mailbox(code="123456")
    ctx = make_ctx(mailbox=mailbox, identity=_Identity(before_ids=seen))
    helpers.build_otp_callback(ctx, keyword="code")()
    assert mailbox.calls[0]["before_ids"] == seen
    assert mailbox.calls[0]["keyword"] == "code"


def test_omitted_options_are_not_forwarded_to_the_mailbox():
    """Passing timeout=None would override the mailbox's own default."""
    mailbox = _Mailbox(code="123456")
    helpers.build_otp_callback(make_ctx(mailbox=mailbox))()
    assert "timeout" not in mailbox.calls[0]
    assert "code_pattern" not in mailbox.calls[0]


def test_configured_options_are_forwarded_to_the_mailbox():
    mailbox = _Mailbox(code="123456")
    helpers.build_otp_callback(
        make_ctx(mailbox=mailbox), timeout=45, code_pattern=r"\d{6}"
    )()
    assert mailbox.calls[0]["timeout"] == 45
    assert mailbox.calls[0]["code_pattern"] == r"\d{6}"


def test_a_mailbox_error_propagates_without_the_fallback(monkeypatch):
    """Silently swallowing it would hide a broken mailbox configuration.

    A stub broker is installed even though the fallback is off. If the error were
    wrongly diverted into the manual channel, the stub would answer instantly and the
    test would fail on the missing exception; without the stub the run would instead
    block on the real broker for the full 300s window, which is not a usable failure.
    """
    from core.manual_otp import ManualOtpBroker

    broker = ManualOtpBroker()
    touched: list[str] = []
    monkeypatch.setattr("core.manual_otp.manual_otp_broker", broker)
    monkeypatch.setattr(
        broker, "wait",
        lambda request_id, **kwargs: touched.append(request_id) or "",
    )

    mailbox = _Mailbox(raises=RuntimeError("imap 拒绝"))
    ctx = make_ctx(mailbox=mailbox)
    with pytest.raises(RuntimeError):
        helpers.build_otp_callback(ctx)()
    assert touched == [], "the mailbox error was diverted instead of surfacing"


def test_a_mailbox_error_hands_over_to_the_manual_channel(monkeypatch):
    from core.manual_otp import ManualOtpBroker, STATUS_WAITING
    broker = ManualOtpBroker()
    monkeypatch.setattr("core.manual_otp.manual_otp_broker", broker)

    def answer(_request):
        waiting = broker.list_waiting()
        if waiting:
            broker.submit("654321", request_id=waiting[0]["request_id"])

    import threading
    timer = threading.Timer(0.05, lambda: None)
    mailbox = _Mailbox(raises=RuntimeError("imap 拒绝"))
    ctx = make_ctx(mailbox=mailbox, config=_Config(extra={"task_uuid": "task-1"}))
    callback = helpers.build_otp_callback(ctx, manual_fallback=True, manual_timeout=5)

    import core.manual_otp as manual
    original_wait = broker.wait

    def wait_then_answer(request_id, **kwargs):
        answer(request_id)
        return "654321"

    monkeypatch.setattr(broker, "wait", wait_then_answer)
    assert callback() == "654321"


def test_an_empty_mailbox_result_hands_over_to_the_manual_channel(monkeypatch):
    from core.manual_otp import ManualOtpBroker
    broker = ManualOtpBroker()
    monkeypatch.setattr("core.manual_otp.manual_otp_broker", broker)
    opened: list[dict] = []
    monkeypatch.setattr(broker, "open", lambda **kwargs: opened.append(kwargs) or _Stub())
    monkeypatch.setattr(broker, "wait", lambda request_id, **kwargs: "654321")

    ctx = make_ctx(mailbox=_Mailbox(code=""), config=_Config(extra={"task_uuid": "task-1"}))
    callback = helpers.build_otp_callback(
        ctx, keyword="code", manual_fallback=True, manual_timeout=7
    )
    assert callback() == "654321"
    assert opened[0]["task_id"] == "task-1"
    assert opened[0]["keyword"] == "code"
    assert opened[0]["timeout"] == 7


def test_the_manual_channel_uses_the_task_id_when_no_uuid_is_set(monkeypatch):
    from core.manual_otp import ManualOtpBroker
    broker = ManualOtpBroker()
    monkeypatch.setattr("core.manual_otp.manual_otp_broker", broker)
    opened: list[dict] = []
    monkeypatch.setattr(broker, "open", lambda **kwargs: opened.append(kwargs) or _Stub())
    monkeypatch.setattr(broker, "wait", lambda request_id, **kwargs: "")

    ctx = make_ctx(mailbox=_Mailbox(code=""), config=_Config(extra={"task_id": "task-2"}))
    assert helpers.build_otp_callback(ctx, manual_fallback=True, manual_timeout=1)() == ""
    assert opened[0]["task_id"] == "task-2"
    assert opened[0]["email"] == "user@example.com"


def test_the_manual_channel_window_falls_back_to_the_otp_timeout(monkeypatch):
    from core.manual_otp import ManualOtpBroker
    broker = ManualOtpBroker()
    monkeypatch.setattr("core.manual_otp.manual_otp_broker", broker)
    opened: list[dict] = []
    monkeypatch.setattr(broker, "open", lambda **kwargs: opened.append(kwargs) or _Stub())
    monkeypatch.setattr(broker, "wait", lambda request_id, **kwargs: "")

    ctx = make_ctx(mailbox=_Mailbox(code=""))
    assert helpers.build_otp_callback(ctx, timeout=42, manual_fallback=True)() == ""
    assert opened[0]["timeout"] == 42


class _Stub:
    request_id = "req-1"
    email = "user@example.com"


# -- link callback ------------------------------------------------------------


def test_no_mailbox_means_no_link_callback():
    assert helpers.build_link_callback(make_ctx(mailbox=None)) is None


def test_the_link_callback_returns_the_link():
    mailbox = _Mailbox(link="https://example.com/verify?token=abc")
    callback = helpers.build_link_callback(make_ctx(mailbox=mailbox), keyword="verify")
    assert callback() == "https://example.com/verify?token=abc"
    assert mailbox.calls[0]["keyword"] == "verify"


def test_the_link_callback_asks_the_mailbox_for_the_current_ids():
    """The link path re-reads the mailbox; the OTP path reuses the identity snapshot."""
    mailbox = _Mailbox(link="https://example.com/x", current_ids={"m9"})
    helpers.build_link_callback(make_ctx(mailbox=mailbox))()
    assert mailbox.calls[0]["before_ids"] == {"m9"}


def test_a_short_link_is_logged_whole():
    logs: list[str] = []
    mailbox = _Mailbox(link="https://example.com/short")
    helpers.build_link_callback(make_ctx(mailbox=mailbox, logs=logs))()
    assert any("https://example.com/short" in line for line in logs)


def test_a_long_link_is_truncated_in_the_log_but_returned_whole():
    """The operator needs a preview; the flow needs the full URL."""
    logs: list[str] = []
    link = "https://example.com/verify?token=" + "x" * 200
    mailbox = _Mailbox(link=link)
    returned = helpers.build_link_callback(
        make_ctx(mailbox=mailbox, logs=logs), preview_chars=40
    )()
    assert returned == link
    logged = " ".join(logs)
    assert "..." in logged
    assert "x" * 200 not in logged


def test_an_empty_link_is_returned_as_is():
    mailbox = _Mailbox(link="")
    assert helpers.build_link_callback(make_ctx(mailbox=mailbox))() == ""
