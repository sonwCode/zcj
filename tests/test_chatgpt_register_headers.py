r"""The password-stage request must carry the session's own browser identity.

A regression guard for a real bug: ``_register_password`` used to build its headers
by hand and hardcode ``accept-language: en-US``, ``sec-ch-ua-platform: "Windows"`` and
a locally re-assembled ``sec-ch-ua``. The warmup and sentinel calls used the geographic
profile, so the signup request was the one place that contradicted it:

* the profile can be Safari or Firefox, neither of which sends ``sec-ch-ua`` at all,
  yet the signup request sent three Chrome-only client-hint headers;
* ``sec-ch-ua-platform`` said ``"Windows"`` while every profile target is macOS or iOS;
* ``sec-ch-ua-mobile`` said ``?0`` even for an iOS profile;
* the rebuilt GREASE brand read ``Not.A/Brand`` where the profile says ``Not_A Brand``,
  and it could not reproduce the profile's GREASE-middle/first orderings at all;
* ``accept-language`` ignored the proxy region entirely.

These tests drive the real method against a stub engine so the assertion is about the
headers that are actually posted, not about a copy of the construction logic.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.identity_profile import resolve_profile
from platforms.chatgpt import register as register_module


class _CapturingSession:
    """Records the headers of the first ``post`` and answers 200 so the loop stops."""

    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "data": data})
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {},
        )


def _password_sequence():
    """``_register_password`` loops until three *distinct* candidates exist."""
    counter = {"n": 0}

    def _next():
        counter["n"] += 1
        return "Str0ng-Passw0rd!%d" % counter["n"]

    return _next


def _engine(profile):
    engine = SimpleNamespace(
        browser_profile=profile,
        session=_CapturingSession(),
        _device_id="did-123",
        _password_sentinel=None,
        email="someone@example.com",
        password="",
        _generate_password=_password_sequence(),
        _load_create_account_password_page=lambda: None,
        _check_sentinel=lambda *a, **k: None,
        _log=lambda *a, **k: None,
    )
    return engine


def _register_headers_for(profile):
    engine = _engine(profile)
    ok, _ = register_module.RegistrationEngine._register_password(engine)
    assert ok is True
    assert len(engine.session.calls) == 1
    return engine.session.calls[0]["headers"]


def _chrome_profile():
    """Chrome is the default family, so no pinning is needed."""
    profile = resolve_profile("US", seed="chrome-seed")
    assert profile.is_chromium
    return profile


@pytest.mark.parametrize("family", ["safari", "firefox"])
def _non_chromium_profile_factory(family):
    """Safari and Firefox send no client hints at all."""
    profile = resolve_profile("US", seed="nonchrome-seed", family=family)
    assert not profile.is_chromium
    assert profile.sec_ch_ua == ""
    return profile


def _non_chromium_profile():
    return _non_chromium_profile_factory("safari")


def test_a_non_chromium_profile_sends_no_client_hint_headers():
    """Safari and Firefox send none of them; sending them is the tell."""
    profile = _non_chromium_profile()
    assert profile.sec_ch_ua == ""

    headers = _register_headers_for(profile)
    lowered = {key.lower() for key in headers}

    assert "sec-ch-ua" not in lowered
    assert "sec-ch-ua-mobile" not in lowered
    assert "sec-ch-ua-platform" not in lowered


def test_the_client_hint_headers_come_verbatim_from_the_profile():
    """A hand-rebuilt GREASE brand drifts from what curl_cffi actually sends."""
    profile = _chrome_profile()

    headers = _register_headers_for(profile)
    lowered = {key.lower(): value for key, value in headers.items()}

    assert lowered["sec-ch-ua"] == profile.sec_ch_ua
    assert lowered["sec-ch-ua-platform"] == '"%s"' % profile.platform
    assert lowered["sec-ch-ua-mobile"] == ("?1" if profile.os_family == "iOS" else "?0")


def test_the_accept_language_follows_the_proxy_region():
    profile = _chrome_profile()

    headers = _register_headers_for(profile)
    lowered = {key.lower(): value for key, value in headers.items()}

    assert lowered["accept-language"] == profile.accept_language
    assert lowered["user-agent"] == profile.user_agent


@pytest.mark.parametrize("region,expected", [
    ("JP", "ja-JP,ja;q=0.9,en;q=0.8"),
    ("DE", "de-DE,de;q=0.9,en;q=0.8"),
])
def test_the_region_reaches_the_header_for_every_locale(region, expected):
    profile = resolve_profile(region, seed="region-seed")
    if not profile.is_chromium:
        pytest.skip("region did not yield a chromium profile")

    headers = _register_headers_for(profile)
    lowered = {key.lower(): value for key, value in headers.items()}

    assert lowered["accept-language"] == expected


def test_the_protocol_specific_headers_survive_the_profile_merge():
    """The profile provides identity headers; the protocol fields must still be set."""
    headers = _register_headers_for(_chrome_profile())
    lowered = {key.lower(): value for key, value in headers.items()}

    assert lowered["content-type"] == "application/json"
    assert lowered["accept"] == "application/json"
    assert lowered["sec-fetch-dest"] == "empty"
    assert lowered["sec-fetch-mode"] == "cors"
    assert lowered["sec-fetch-site"] == "same-origin"
    assert lowered["origin"] == "https://auth.openai.com"
    assert lowered["referer"] == "https://auth.openai.com/create-account/password"
    assert lowered["oai-device-id"] == "did-123"


def test_the_datadog_trace_headers_are_still_injected():
    headers = _register_headers_for(_chrome_profile())
    lowered = {key.lower() for key in headers}

    assert "traceparent" in lowered
    assert "x-datadog-origin" in lowered


def test_the_password_is_sent_but_never_logged():
    engine = _engine(_chrome_profile())
    logs = []
    engine._log = lambda message, *a, **k: logs.append(str(message))

    ok, password = register_module.RegistrationEngine._register_password(engine)

    assert ok is True
    body = json.loads(engine.session.calls[0]["data"])
    assert body["password"] == password
    assert body["username"] == "someone@example.com"
    assert all(password not in message for message in logs)
