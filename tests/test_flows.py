"""The four registration flows: the guards, the wiring, and the cleanup contract.

A flow is the seam between an adapter (per platform) and the machinery every platform
shares: preflight, the identity/mailbox/executor guards, the OTP and link callbacks, the
phone callbacks, and the result mapper. Each flow is a fixed sequence, so what matters
here is the order and the conditions - which guard fires, which callback is built, and
what still runs when a step raises.

The cleanup contract is the sharpest one: phone numbers are rented, so a run that fails
halfway must still hand the number back. That is why the browser and phone flows put the
runner inside a try with phone_cleanup in the finally, and it is pinned below by making
the runner raise and asserting the cleanup still ran.
"""
from __future__ import annotations

import pytest

from core.registration.adapters import (
    BrowserRegistrationAdapter,
    LinkSpec,
    OtpSpec,
    ProtocolMailboxAdapter,
    ProtocolOAuthAdapter,
)
from core.registration.errors import (
    BrowserReuseRequiredError,
    IdentityResolutionError,
    RegistrationUnsupportedError,
)
from core.registration.flows import (
    BrowserRegistrationFlow,
    ProtocolMailboxFlow,
    ProtocolOAuthFlow,
    ProtocolPhoneFlow,
)
from core.registration.models import (
    RegistrationCapability,
    RegistrationContext,
    RegistrationResult,
)


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
        self.identity_provider = kwargs.pop("identity_provider", "mailbox")
        self.chrome_user_data_dir = kwargs.pop("chrome_user_data_dir", "")
        self.chrome_cdp_url = kwargs.pop("chrome_cdp_url", "")
        self.before_ids = kwargs.pop("before_ids", set())


class _Mailbox:
    def __init__(self):
        self.calls: list[dict] = []

    def wait_for_code(self, account, **kwargs):
        self.calls.append(kwargs)
        return "123456"

    def get_current_ids(self, account):
        return {"seen"}

    def wait_for_link(self, account, **kwargs):
        self.calls.append(kwargs)
        return "https://example.com/v"


class _ExecutorCM:
    def __init__(self, value):
        self.value = value
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self.value

    def __exit__(self, *exc):
        self.exited += 1
        return False


class _Platform:
    def __init__(self, mailbox=None, captcha="captcha", executor="executor"):
        self.mailbox = mailbox if mailbox is not None else _Mailbox()
        self.name = "chatgpt"
        self._captcha = captcha
        self._executor = executor
        self.captcha_calls = 0
        self.executor_calls = 0
        self.last_cm = None

    def _make_captcha(self):
        self.captcha_calls += 1
        return self._captcha

    def _make_executor(self):
        self.executor_calls += 1
        self.last_cm = _ExecutorCM(self._executor)
        return self.last_cm


def make_ctx(**kwargs):
    platform = kwargs.pop("platform", None) or _Platform()
    identity = kwargs.pop("identity", None) or _Identity()
    config = kwargs.pop("config", None) or _Config()
    logs = kwargs.pop("logs", None)
    recorded = logs if logs is not None else []
    return RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=platform,
        identity=identity,
        config=config,
        email=None,
        password=None,
        log_fn=recorded.append,
    )


def mapper(ctx, raw):
    return RegistrationResult(email="user@example.com", password="pw", token=str(raw))


def boom(*args, **kwargs):
    raise RuntimeError("boom")


# -- browser flow: preflight and guards ---------------------------------------


def test_the_browser_flow_runs_the_preflight_when_present():
    seen = []
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "raw",
        preflight=lambda ctx: seen.append(ctx),
    )
    BrowserRegistrationFlow(adapter).run(make_ctx())
    assert len(seen) == 1


def test_the_browser_flow_tolerates_a_missing_preflight():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "raw",
    )
    assert BrowserRegistrationFlow(adapter).run(make_ctx()).token == "raw"


def test_a_missing_email_stops_the_browser_flow():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "raw",
    )
    with pytest.raises(IdentityResolutionError):
        BrowserRegistrationFlow(adapter).run(make_ctx(identity=_Identity(email="")))


def test_a_missing_mailbox_stops_the_browser_flow():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "raw",
    )
    with pytest.raises(IdentityResolutionError):
        BrowserRegistrationFlow(adapter).run(make_ctx(identity=_Identity(has_mailbox=False)))


def test_the_mailbox_guards_can_be_switched_off_by_capability():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "raw",
        capability=RegistrationCapability(
            browser_mailbox_requires_email=False,
            browser_mailbox_requires_mailbox=False,
        ),
    )
    ctx = make_ctx(identity=_Identity(email="", has_mailbox=False))
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "raw"


def test_a_phone_identity_skips_the_mailbox_guards(monkeypatch):
    """A phone run carries no mailbox, so demanding one would break every phone platform."""
    import core.registration.flows as flows

    monkeypatch.setattr(flows, "build_phone_callbacks", lambda ctx, service=None: ("cb", None))
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "raw",
        capability=RegistrationCapability(
            browser_mailbox_requires_email=True,
            browser_mailbox_requires_mailbox=True,
        ),
    )
    ctx = make_ctx(
        identity=_Identity(identity_provider="phone", email="", has_mailbox=False)
    )
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "raw"


# -- browser flow: the oauth early return -------------------------------------


def test_an_oauth_browser_run_returns_before_the_worker_path():
    ran = []
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: ran.append("worker") or "worker",
        browser_register_runner=lambda w, ctx, art: ran.append("runner") or "raw",
        oauth_runner=lambda ctx: "oauth-raw",
    )
    ctx = make_ctx(identity=_Identity(identity_provider="oauth_browser"))
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "oauth-raw"
    assert ran == [], "the worker path ran alongside the oauth path"


def test_an_oauth_browser_run_skips_the_mailbox_guards():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        oauth_runner=lambda ctx: "oauth-raw",
    )
    ctx = make_ctx(identity=_Identity(identity_provider="oauth_browser", email="", has_mailbox=False))
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "oauth-raw"


def test_an_oauth_browser_identity_without_a_runner_falls_through():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "raw",
        oauth_runner=None,
    )
    ctx = make_ctx(identity=_Identity(identity_provider="oauth_browser"))
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "raw"


def test_an_unsupported_oauth_executor_is_rejected():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        oauth_runner=lambda ctx: "oauth-raw",
        capability=RegistrationCapability(oauth_allowed_executor_types=("browser",)),
    )
    ctx = make_ctx(
        identity=_Identity(identity_provider="oauth_browser"),
        config=_Config(executor_type="protocol"),
    )
    with pytest.raises(RegistrationUnsupportedError):
        BrowserRegistrationFlow(adapter).run(ctx)


def test_a_headless_oauth_run_needs_a_reusable_browser():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        oauth_runner=lambda ctx: "oauth-raw",
        capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
    )
    ctx = make_ctx(
        identity=_Identity(identity_provider="oauth_browser"),
        config=_Config(executor_type="headless"),
    )
    with pytest.raises(BrowserReuseRequiredError):
        BrowserRegistrationFlow(adapter).run(ctx)


def test_a_headless_oauth_run_passes_with_a_browser_session():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        oauth_runner=lambda ctx: "oauth-raw",
        capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
    )
    ctx = make_ctx(
        identity=_Identity(identity_provider="oauth_browser", chrome_cdp_url="http://127.0.0.1:9222"),
        config=_Config(executor_type="headless"),
    )
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "oauth-raw"


def test_a_non_headless_oauth_run_does_not_need_a_browser():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        oauth_runner=lambda ctx: "oauth-raw",
        capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
    )
    ctx = make_ctx(
        identity=_Identity(identity_provider="oauth_browser"),
        config=_Config(executor_type="browser"),
    )
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "oauth-raw"


# -- browser flow: artifacts --------------------------------------------------


def test_the_browser_flow_builds_a_captcha_only_for_the_mailbox_provider():
    platform = _Platform()
    seen = {}

    def build(ctx, artifacts):
        seen["captcha"] = artifacts.captcha_solver
        return "worker"

    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=build,
        browser_register_runner=lambda w, ctx, art: "raw",
        use_captcha_for_mailbox=True,
    )
    BrowserRegistrationFlow(adapter).run(
        make_ctx(platform=platform, identity=_Identity(identity_provider="mailbox"))
    )
    assert seen["captcha"] == "captcha" and platform.captcha_calls == 1


def test_a_non_mailbox_provider_gets_no_captcha():
    platform = _Platform()
    seen = {}

    def build(ctx, artifacts):
        seen["captcha"] = artifacts.captcha_solver
        return "worker"

    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=build,
        browser_register_runner=lambda w, ctx, art: "raw",
        use_captcha_for_mailbox=True,
        capability=RegistrationCapability(
            browser_mailbox_requires_email=False,
            browser_mailbox_requires_mailbox=False,
        ),
    )
    BrowserRegistrationFlow(adapter).run(
        make_ctx(platform=platform, identity=_Identity(identity_provider="passkey"))
    )
    assert seen["captcha"] is None and platform.captcha_calls == 0


def test_the_browser_flow_builds_the_otp_callback_from_its_spec():
    mailbox = _Mailbox()
    seen = {}

    def build(ctx, artifacts):
        seen["otp"] = artifacts.otp_callback
        return "worker"

    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=build,
        browser_register_runner=lambda w, ctx, art: "raw",
        otp_spec=OtpSpec(keyword="code", timeout=30),
    )
    BrowserRegistrationFlow(adapter).run(make_ctx(platform=_Platform(mailbox=mailbox)))
    assert seen["otp"] is not None
    assert seen["otp"]() == "123456"
    assert mailbox.calls[0]["keyword"] == "code"
    assert mailbox.calls[0]["timeout"] == 30


def test_the_browser_flow_builds_the_link_callback_from_its_spec():
    seen = {}

    def build(ctx, artifacts):
        seen["link"] = artifacts.verification_link_callback
        return "worker"

    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=build,
        browser_register_runner=lambda w, ctx, art: "raw",
        link_spec=LinkSpec(keyword="verify"),
    )
    BrowserRegistrationFlow(adapter).run(make_ctx())
    assert seen["link"]() == "https://example.com/v"


def test_the_browser_flow_leaves_callbacks_none_without_specs():
    seen = {}

    def build(ctx, artifacts):
        seen["otp"] = artifacts.otp_callback
        seen["link"] = artifacts.verification_link_callback
        return "worker"

    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=build,
        browser_register_runner=lambda w, ctx, art: "raw",
    )
    BrowserRegistrationFlow(adapter).run(make_ctx())
    assert seen["otp"] is None and seen["link"] is None


def test_a_phone_registration_gets_no_otp_callback(monkeypatch):
    """A phone run's code arrives by SMS; waiting on a mailbox would hang it."""
    import core.registration.flows as flows

    monkeypatch.setattr(flows, "build_phone_callbacks", lambda ctx, service=None: ("cb", None))
    seen = {}

    def build(ctx, artifacts):
        seen["otp"] = artifacts.otp_callback
        return "worker"

    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=build,
        browser_register_runner=lambda w, ctx, art: "raw",
        otp_spec=OtpSpec(keyword="code"),
    )
    ctx = make_ctx(identity=_Identity(identity_provider="phone"))
    assert BrowserRegistrationFlow(adapter).run(ctx).token == "raw"
    assert seen["otp"] is None, "a phone run must not wait on a mailbox"


def test_the_browser_flow_sets_the_raw_result_for_the_mapper():
    seen = {}

    def result_mapper(ctx, raw):
        seen["raw"] = raw
        return RegistrationResult(email="e", password="p")

    adapter = BrowserRegistrationAdapter(
        result_mapper=result_mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=lambda w, ctx, art: "the-raw",
    )
    BrowserRegistrationFlow(adapter).run(make_ctx())
    assert seen["raw"] == "the-raw"


# -- browser flow: failure paths ----------------------------------------------


def test_a_missing_worker_builder_is_reported():
    adapter = BrowserRegistrationAdapter(result_mapper=mapper)
    with pytest.raises(RuntimeError, match="未实现浏览器注册适配器"):
        BrowserRegistrationFlow(adapter).run(make_ctx())


def test_a_missing_register_runner_is_reported():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=None,
    )
    with pytest.raises(RuntimeError, match="未实现浏览器注册适配器"):
        BrowserRegistrationFlow(adapter).run(make_ctx())


def test_a_worker_builder_returning_none_is_reported():
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: None,
        browser_register_runner=lambda w, ctx, art: "raw",
    )
    with pytest.raises(RuntimeError, match="未实现浏览器注册适配器"):
        BrowserRegistrationFlow(adapter).run(make_ctx())


def test_the_browser_flow_hands_the_number_back_when_the_runner_fails(monkeypatch):
    import core.registration.flows as flows

    cleaned = []
    monkeypatch.setattr(
        flows, "build_phone_callbacks",
        lambda ctx, service=None: ("phone", lambda: cleaned.append(1)),
    )
    adapter = BrowserRegistrationAdapter(
        result_mapper=mapper,
        browser_worker_builder=lambda ctx, art: "worker",
        browser_register_runner=boom,
        capability=RegistrationCapability(
            browser_mailbox_requires_email=False,
            browser_mailbox_requires_mailbox=False,
        ),
    )
    with pytest.raises(RuntimeError, match="boom"):
        BrowserRegistrationFlow(adapter).run(make_ctx())
    assert cleaned == [1], "phone_cleanup did not run after the runner raised"


# -- protocol mailbox flow ----------------------------------------------------


def test_the_mailbox_flow_runs_the_preflight_when_present():
    seen = []
    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=lambda ctx, art: "worker",
        register_runner=lambda w, ctx, art: "raw",
        preflight=lambda ctx: seen.append(ctx),
    )
    ProtocolMailboxFlow(adapter).run(make_ctx())
    assert len(seen) == 1


def test_the_mailbox_flow_enforces_its_identity_guards():
    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=lambda ctx, art: "worker",
        register_runner=lambda w, ctx, art: "raw",
    )
    with pytest.raises(IdentityResolutionError):
        ProtocolMailboxFlow(adapter).run(make_ctx(identity=_Identity(email="")))
    with pytest.raises(IdentityResolutionError):
        ProtocolMailboxFlow(adapter).run(make_ctx(identity=_Identity(has_mailbox=False)))


def test_the_mailbox_flow_honours_the_capability_switches():
    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=lambda ctx, art: "worker",
        register_runner=lambda w, ctx, art: "raw",
        capability=RegistrationCapability(
            protocol_mailbox_requires_email=False,
            protocol_mailbox_requires_mailbox=False,
        ),
    )
    ctx = make_ctx(identity=_Identity(email="", has_mailbox=False))
    assert ProtocolMailboxFlow(adapter).run(ctx).token == "raw"


def test_the_mailbox_flow_enters_the_executor_only_when_asked():
    platform = _Platform()
    seen = {}

    def build(ctx, artifacts):
        seen["executor"] = artifacts.executor
        return "worker"

    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=build,
        register_runner=lambda w, ctx, art: "raw",
    )
    ProtocolMailboxFlow(adapter).run(make_ctx(platform=platform))
    assert seen["executor"] is None and platform.executor_calls == 0


def test_the_mailbox_flow_provides_the_executor_when_asked():
    platform = _Platform()
    seen = {}

    def build(ctx, artifacts):
        seen["executor"] = artifacts.executor
        return "worker"

    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=build,
        register_runner=lambda w, ctx, art: "raw",
        use_executor=True,
    )
    ProtocolMailboxFlow(adapter).run(make_ctx(platform=platform))
    assert seen["executor"] == "executor" and platform.executor_calls == 1


def test_the_mailbox_flow_closes_the_executor_after_a_failure():
    platform = _Platform()

    def build(ctx, artifacts):
        raise RuntimeError("worker boom")

    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=build,
        register_runner=lambda w, ctx, art: "raw",
        use_executor=True,
    )
    with pytest.raises(RuntimeError, match="worker boom"):
        ProtocolMailboxFlow(adapter).run(make_ctx(platform=platform))
    assert platform.executor_calls == 1
    assert platform.last_cm.exited == 1, "the executor was not released"


def test_the_mailbox_flow_builds_a_captcha_when_asked():
    platform = _Platform()
    seen = {}

    def build(ctx, artifacts):
        seen["captcha"] = artifacts.captcha_solver
        return "worker"

    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=build,
        register_runner=lambda w, ctx, art: "raw",
        use_captcha=True,
    )
    ProtocolMailboxFlow(adapter).run(make_ctx(platform=platform))
    assert seen["captcha"] == "captcha" and platform.captcha_calls == 1


def test_the_mailbox_flow_builds_its_callbacks():
    seen = {}

    def build(ctx, artifacts):
        seen["otp"] = artifacts.otp_callback
        seen["link"] = artifacts.verification_link_callback
        return "worker"

    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=build,
        register_runner=lambda w, ctx, art: "raw",
        otp_spec=OtpSpec(keyword="code"),
        link_spec=LinkSpec(keyword="verify"),
    )
    ProtocolMailboxFlow(adapter).run(make_ctx())
    assert seen["otp"] is not None and seen["link"] is not None


def test_the_mailbox_flow_leaves_callbacks_none_without_specs():
    seen = {}

    def build(ctx, artifacts):
        seen["otp"] = artifacts.otp_callback
        seen["link"] = artifacts.verification_link_callback
        return "worker"

    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=build,
        register_runner=lambda w, ctx, art: "raw",
    )
    ProtocolMailboxFlow(adapter).run(make_ctx())
    assert seen["otp"] is None and seen["link"] is None


# -- protocol phone flow ------------------------------------------------------


def test_the_phone_flow_requires_a_sms_provider(monkeypatch):
    import core.registration.flows as flows

    monkeypatch.setattr(flows, "build_phone_callbacks", lambda ctx, service=None: (None, None))
    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=lambda ctx, art: "worker",
        register_runner=lambda w, ctx, art: "raw",
    )
    with pytest.raises(RuntimeError, match="接码平台"):
        ProtocolPhoneFlow(adapter).run(make_ctx())


def test_the_phone_flow_runs_the_runner_with_the_callback(monkeypatch):
    import core.registration.flows as flows

    monkeypatch.setattr(flows, "build_phone_callbacks", lambda ctx, service=None: ("cb", None))
    seen = {}

    def build(ctx, artifacts):
        seen["phone"] = artifacts.phone_callback
        return "worker"

    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=build,
        register_runner=lambda w, ctx, art: "raw",
    )
    result = ProtocolPhoneFlow(adapter).run(make_ctx())
    assert result.token == "raw" and seen["phone"] == "cb"


def test_the_phone_flow_hands_the_number_back_when_the_runner_fails(monkeypatch):
    import core.registration.flows as flows

    cleaned = []
    monkeypatch.setattr(
        flows, "build_phone_callbacks",
        lambda ctx, service=None: ("cb", lambda: cleaned.append(1)),
    )
    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=lambda ctx, art: "worker",
        register_runner=boom,
    )
    with pytest.raises(RuntimeError, match="boom"):
        ProtocolPhoneFlow(adapter).run(make_ctx())
    assert cleaned == [1], "phone_cleanup did not run after the runner raised"


def test_the_phone_flow_runs_the_preflight_when_present(monkeypatch):
    import core.registration.flows as flows

    monkeypatch.setattr(flows, "build_phone_callbacks", lambda ctx, service=None: ("cb", None))
    seen = []
    adapter = ProtocolMailboxAdapter(
        result_mapper=mapper,
        worker_builder=lambda ctx, art: "worker",
        register_runner=lambda w, ctx, art: "raw",
        preflight=lambda ctx: seen.append(ctx),
    )
    ProtocolPhoneFlow(adapter).run(make_ctx())
    assert len(seen) == 1


# -- protocol oauth flow ------------------------------------------------------


def test_the_oauth_flow_runs_the_runner_and_maps_the_result():
    seen = {}

    def result_mapper(ctx, raw):
        seen["raw"] = raw
        return RegistrationResult(email="e", password="p", token="mapped")

    adapter = ProtocolOAuthAdapter(
        oauth_runner=lambda ctx: "oauth-raw",
        result_mapper=result_mapper,
    )
    result = ProtocolOAuthFlow(adapter).run(make_ctx())
    assert result.token == "mapped" and seen["raw"] == "oauth-raw"


def test_the_oauth_flow_runs_the_preflight_when_present():
    seen = []
    adapter = ProtocolOAuthAdapter(
        oauth_runner=lambda ctx: "raw",
        result_mapper=mapper,
        preflight=lambda ctx: seen.append(ctx),
    )
    ProtocolOAuthFlow(adapter).run(make_ctx())
    assert len(seen) == 1


def test_the_oauth_flow_rejects_an_unsupported_executor():
    adapter = ProtocolOAuthAdapter(
        oauth_runner=lambda ctx: "raw",
        result_mapper=mapper,
        capability=RegistrationCapability(oauth_allowed_executor_types=("browser",)),
    )
    with pytest.raises(RegistrationUnsupportedError):
        ProtocolOAuthFlow(adapter).run(make_ctx(config=_Config(executor_type="protocol")))


def test_the_oauth_flow_requires_a_browser_for_headless():
    adapter = ProtocolOAuthAdapter(
        oauth_runner=lambda ctx: "raw",
        result_mapper=mapper,
        capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
    )
    with pytest.raises(BrowserReuseRequiredError):
        ProtocolOAuthFlow(adapter).run(make_ctx(config=_Config(executor_type="headless")))


def test_the_oauth_flow_finds_the_browser_through_the_user_data_dir():
    adapter = ProtocolOAuthAdapter(
        oauth_runner=lambda ctx: "raw",
        result_mapper=mapper,
        capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
    )
    ctx = make_ctx(
        identity=_Identity(chrome_user_data_dir="/tmp/profile"),
        config=_Config(executor_type="headless"),
    )
    assert ProtocolOAuthFlow(adapter).run(ctx).token == "raw"
