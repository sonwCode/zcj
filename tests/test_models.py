"""The registration value objects: the derived defaults that silently pick a path.

These are plain dataclasses, so most of their surface is just field assignment and not
worth asserting. What is worth asserting is the small amount of behaviour hiding in the
properties, because each one turns a missing or falsy value into a workable default
instead of raising:

- executor_type collapses a missing attribute, an empty string, None and even 0 all to
  "protocol". A config that forgot to set an executor type therefore quietly runs the
  protocol path rather than failing loudly. That is a deliberate convenience and it is
  pinned here so nobody "tightens" it into a crash by accident.
- extra hands back a fresh copy on every access. Callers pass the dict around and mutate
  it freely; if this ever returned the live object, one caller could corrupt the config
  seen by every later step.

The mutable-default cases (metadata, extra) are the classic dataclass trap: they must be
per-instance, never shared between objects.
"""
from __future__ import annotations

import pytest

from core.registration.models import (
    RegistrationArtifacts,
    RegistrationCapability,
    RegistrationContext,
    RegistrationResult,
)


class _Config:
    """Stands in for a platform config; attributes are set only when asked for."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def make_context(config=None, log_fn=None):
    return RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=object(),
        identity=object(),
        config=config if config is not None else _Config(),
        email=None,
        password=None,
        log_fn=log_fn if log_fn is not None else (lambda message: None),
    )


# -- executor_type: every falsy shape means "protocol" -------------------------


def test_the_executor_type_is_read_from_the_config():
    context = make_context(_Config(executor_type="headless"))
    assert context.executor_type == "headless"


def test_a_missing_executor_type_falls_back_to_protocol():
    assert make_context(_Config()).executor_type == "protocol"


@pytest.mark.parametrize("falsy", ["", None, 0, False])
def test_a_falsy_executor_type_falls_back_to_protocol(falsy):
    """Not an error: an unset type quietly selects the protocol path."""
    assert make_context(_Config(executor_type=falsy)).executor_type == "protocol"


def test_the_executor_type_is_always_a_string():
    """Downstream compares it as a string, so a non-string config value must not leak."""
    assert make_context(_Config(executor_type=123)).executor_type == "123"


# -- proxy --------------------------------------------------------------------


def test_the_proxy_is_read_from_the_config():
    assert make_context(_Config(proxy="http://127.0.0.1:7890")).proxy == "http://127.0.0.1:7890"


def test_a_missing_proxy_is_none():
    assert make_context(_Config()).proxy is None


def test_an_empty_proxy_is_passed_through_unchanged():
    """Unlike the executor type this one is not defaulted - the caller decides."""
    assert make_context(_Config(proxy="")).proxy == ""


# -- extra: always a fresh copy ----------------------------------------------


def test_extra_is_read_from_the_config():
    assert make_context(_Config(extra={"a": 1})).extra == {"a": 1}


def test_extra_hands_back_a_copy_every_time():
    """Mutating what you got back must not reach the config every later step sees."""
    config = _Config(extra={"a": 1})
    context = make_context(config)
    context.extra["a"] = 999
    context.extra["b"] = 2
    assert config.extra == {"a": 1}


def test_two_reads_of_extra_do_not_share_storage():
    context = make_context(_Config(extra={"a": 1}))
    assert context.extra is not context.extra


@pytest.mark.parametrize("missing", ["absent", None])
def test_a_missing_or_none_extra_is_an_empty_dict(missing):
    config = _Config() if missing == "absent" else _Config(extra=None)
    assert make_context(config).extra == {}


def test_extra_of_a_non_mapping_is_not_silently_kept():
    """A caller passing something odd should get a dict, not that object verbatim."""
    context = make_context(_Config(extra=()))
    assert isinstance(context.extra, dict) and context.extra == {}


# -- log ----------------------------------------------------------------------


def test_log_routes_the_message_to_the_log_function():
    seen = []
    make_context(log_fn=seen.append).log("hello")
    assert seen == ["hello"]


def test_log_does_not_swallow_the_return_value():
    seen = []
    make_context(log_fn=seen.append).log("first")
    make_context(log_fn=seen.append).log("second")
    assert seen == ["first", "second"]


# -- capability defaults ------------------------------------------------------


def test_the_capability_defaults_keep_the_guards_on():
    """A platform that says nothing gets the strict behaviour, not the permissive one."""
    capability = RegistrationCapability()
    assert capability.browser_mailbox_requires_email is True
    assert capability.browser_mailbox_requires_mailbox is True
    assert capability.protocol_mailbox_requires_email is True
    assert capability.protocol_mailbox_requires_mailbox is True


def test_the_capability_defaults_do_not_claim_oauth():
    capability = RegistrationCapability()
    assert capability.oauth_allowed_executor_types is None
    assert capability.oauth_headless_requires_browser_reuse is False


def test_the_capability_is_per_instance():
    first = RegistrationCapability()
    second = RegistrationCapability()
    first.browser_mailbox_requires_email = False
    assert second.browser_mailbox_requires_email is True


# -- mutable defaults must not be shared --------------------------------------


def test_artifacts_metadata_is_per_instance():
    first = RegistrationArtifacts()
    first.metadata["stage"] = "otp"
    assert RegistrationArtifacts().metadata == {}


def test_result_extra_is_per_instance():
    first = RegistrationResult(email="a@example.com", password="pw")
    first.extra["token"] = "t"
    assert RegistrationResult(email="b@example.com", password="pw").extra == {}


# -- artifacts and result shape -----------------------------------------------


def test_artifacts_start_empty():
    artifacts = RegistrationArtifacts()
    assert artifacts.otp_callback is None
    assert artifacts.verification_link_callback is None
    assert artifacts.phone_callback is None
    assert artifacts.phone_cleanup is None
    assert artifacts.captcha_solver is None
    assert artifacts.executor is None
    assert artifacts.raw_result is None


def test_a_result_only_needs_an_email_and_a_password():
    result = RegistrationResult(email="a@example.com", password="pw")
    assert result.email == "a@example.com" and result.password == "pw"
    assert result.user_id == ""
    assert result.region == ""
    assert result.token == ""
    assert result.status is None
    assert result.trial_end_time == 0


def test_a_result_is_filled_in_after_construction():
    """The flow builds a result, then the pipeline stamps ids and tokens onto it."""
    result = RegistrationResult(email="a@example.com", password="pw")
    result.user_id = "u1"
    result.token = "tok"
    result.region = "JP"
    assert (result.user_id, result.token, result.region) == ("u1", "tok", "JP")


# -- slots --------------------------------------------------------------------


def test_the_context_uses_slots():
    """Slots are why a typo on a context attribute raises instead of silently sticking."""
    context = make_context()
    assert not hasattr(context, "__dict__")
    with pytest.raises(AttributeError):
        context.not_a_real_field = 1
