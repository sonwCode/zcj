"""Coherence invariants for the generated browser identity.

``core/identity_profile.py`` exists to remove contradictions between the browser
identity and the proxy exit: a Japanese timezone with an American locale, a Safari
UA with Chrome client hints, or a ``Date.toString()`` that claims GMT+0000 while the
route exits in Tokyo. Each of those is individually invisible and jointly a
fingerprint, so the properties worth testing are the *relationships* between fields
rather than any single value.

Everything here is offline: the module is a pure function of (region, seed, target)
by design, precisely so the cloud preflight can call it without a network.
"""
from __future__ import annotations

import re

import pytest

from core import identity_profile as ip


def _profile(target: str = "chrome142", region: str = "US", seed: str = "s"):
    return ip.resolve_profile(region, seed=seed, impersonate=target)


# -- family / client-hint coherence -----------------------------------------

def test_only_chromium_profiles_carry_client_hints():
    """Safari and Firefox send none of the sec-ch-ua family; emitting them is the tell."""
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        if profile.family == "chrome":
            assert profile.sec_ch_ua, f"{target} is chrome and must carry client hints"
            assert profile.is_chromium is True
        else:
            assert profile.sec_ch_ua == "", f"{target} is {profile.family} and must not"


def test_non_chromium_headers_omit_every_client_hint():
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        if profile.family == "chrome":
            continue
        headers = {k.lower() for k in profile.headers()}
        for hint in ("sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"):
            assert hint not in headers, f"{target} must not send {hint}"


def test_safari_omits_the_priority_trio_that_chrome_and_firefox_send():
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        nav = {k.lower() for k in profile.headers(navigation=True)}
        if profile.family.startswith("safari"):
            assert "priority" not in nav
            assert "sec-fetch-user" not in nav
            assert "upgrade-insecure-requests" not in nav
            assert "te" not in nav
        else:
            assert "priority" in nav


def test_firefox_sends_te_trailers_and_safari_does_not():
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        nav = profile.headers(navigation=True)
        if profile.family == "firefox":
            assert nav.get("TE") == "trailers"
        else:
            assert "TE" not in nav


def test_connection_header_is_never_emitted():
    """The transport is HTTP/2, where a real browser does not send Connection."""
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        for navigation in (False, True):
            headers = {k.lower() for k in profile.headers(navigation=navigation)}
            assert "connection" not in headers


def test_sec_ch_ua_mobile_tracks_the_os():
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        if not profile.is_chromium:
            continue
        expected = "?1" if profile.os_family == "iOS" else "?0"
        assert profile.headers()["sec-ch-ua-mobile"] == expected


def test_sec_ch_ua_platform_quotes_the_platform_name():
    profile = _profile("chrome142")
    assert profile.headers()["sec-ch-ua-platform"] == chr(34) + profile.platform + chr(34)


# -- UA / OS / version coherence --------------------------------------------

def test_the_ua_version_matches_the_declared_browser_version():
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        assert profile.browser_version, f"{target} has no parseable version"
        assert profile.browser_version in profile.user_agent


def test_the_user_agent_agrees_with_the_declared_os_family():
    """The P0-3 fix: a Chrome target for another OS must not claim macOS."""
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        ua = profile.user_agent
        if profile.os_family == "Windows":
            assert "Windows" in ua, f"{target} claims Windows but the UA says otherwise"
        elif profile.os_family == "macOS":
            assert "Macintosh" in ua or "Mac OS X" in ua
        elif profile.os_family == "iOS":
            assert "iPhone" in ua or "iPad" in ua


# -- region coherence --------------------------------------------------------

def test_timezone_label_is_filled_in_for_every_known_region():
    for region in ip.known_regions():
        profile = ip.resolve_profile(region, seed=region, impersonate="chrome142")
        assert profile.region == region
        assert profile.timezone_label, f"{region} produced an empty timezone label"


@pytest.mark.parametrize("region", ip.known_regions())
def test_every_known_region_has_a_resolvable_timezone(region):
    """The trap this guards: it passes on a dev box and breaks on a bare server image.

    When ZoneInfo cannot resolve the zone the renderer silently falls back to UTC, so
    the payload claims GMT+0000 while the proxy exits elsewhere - the exact
    contradiction the module exists to remove. tzdata is therefore a real deployment
    dependency, not a nicety.
    """
    profile = ip.resolve_profile(region, seed=region, impersonate="chrome142")
    assert ip.timezone_is_available(profile.timezone), (
        f"{region} maps to {profile.timezone}, which this host cannot resolve - "
        "install tzdata or the Sentinel payload will claim UTC for a non-UTC exit"
    )
    assert profile.timezone_resolved is True


def test_unknown_or_empty_regions_fall_back_to_the_default():
    for candidate in ("", None, "   ", "ZZ", "not-a-country", "12"):
        assert ip.normalize_region(candidate) == ip.DEFAULT_REGION


def test_region_matching_is_case_insensitive():
    assert ip.normalize_region("jp") == "JP"
    assert ip.normalize_region(" jp ") == "JP"


# -- js Date.toString() ------------------------------------------------------

_JS_DATE = re.compile(
    r"[A-Z][a-z]{2} [A-Z][a-z]{2} [0-9]{2} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} "
    r"GMT[+-][0-9]{4} \(.+\)"
)


def test_js_date_string_has_no_comma_after_the_weekday():
    """JavaScript emits Mon Jan 01 2024 ... ; the old code emitted Mon, 01 Jan 2024 ... ."""
    rendered = _profile().js_date_string()
    assert _JS_DATE.fullmatch(rendered), rendered
    assert "," not in rendered.split(" GMT")[0]


def test_js_date_string_offset_follows_the_profile_timezone():
    jp = ip.resolve_profile("JP", seed="jp", impersonate="chrome142")
    rendered = jp.js_date_string()
    assert jp.timezone_label in rendered
    assert "+0900" in rendered, "a Tokyo profile must not render a zero offset"


# -- determinism and overrides ----------------------------------------------

def test_the_same_seed_produces_an_identical_profile():
    a = ip.resolve_profile("DE", seed="same", impersonate="chrome142")
    b = ip.resolve_profile("DE", seed="same", impersonate="chrome142")
    assert a == b


def test_an_explicit_target_beats_a_family_request():
    assert ip.resolve_impersonate("firefox144", seed="x", family="safari") == "firefox144"


def test_a_family_request_stays_inside_that_family():
    for family in ip.known_families():
        for seed in ("a", "b", "c", "d"):
            target = ip.resolve_impersonate("", seed=seed, family=family)
            assert _profile(target).family == family


def test_overrides_only_replace_the_fields_that_were_provided():
    base = ip.resolve_profile("US", seed="o", impersonate="chrome142")
    overridden = ip.with_overrides(
        base, screen="1280x720", navigator_language="en-GB",
        timezone="Europe/London", hardware_concurrency=12,
    )
    assert overridden.screen == "1280x720"
    assert overridden.navigator_language == "en-GB"
    assert overridden.timezone == "Europe/London"
    assert overridden.hardware_concurrency == 12
    assert overridden.region == base.region
    assert overridden.user_agent == base.user_agent


def test_overrides_ignore_an_unusable_screen_value():
    base = ip.resolve_profile("US", seed="o2", impersonate="chrome142")
    assert ip.with_overrides(base, screen="not-a-size") is base


def test_overrides_clear_the_label_when_the_timezone_is_replaced():
    """A stale label would contradict the new zone in Date.toString()."""
    base = ip.resolve_profile("US", seed="o3", impersonate="chrome142")
    assert ip.with_overrides(base, timezone="Asia/Tokyo").timezone_label == ""


def test_profile_for_extra_prefers_the_proxy_route_country():
    profile = ip.profile_for_extra({"proxy_route_country": "JP", "region": "US"}, seed="e")
    assert profile.region == "JP"


def test_screen_and_memory_are_plausible_for_every_target():
    for target in ip.known_impersonate_targets():
        profile = _profile(target)
        assert re.fullmatch(r"[0-9]+x[0-9]+", profile.screen), profile.screen
        assert profile.hardware_concurrency > 0
        assert profile.device_memory > 0
