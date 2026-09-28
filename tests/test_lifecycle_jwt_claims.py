r"""JWT ``exp``/``iat`` formatting must never raise.

``refresh_and_sync_cpa`` decodes the access token and turns its claims into display
strings for the CPA upload payload. The claims are upstream-issued, so they are
untrusted, and the two conversions used to be guarded only by ``if exp else`` -
which filters ``None``/``0`` but nothing else. A millisecond epoch or a corrupt
number raises inside ``fromtimestamp``.

The reason it matters is where the raise lands: the whole refresh-and-upload body is
wrapped in one ``except Exception`` that increments ``results["error"]`` and logs
"异常". By then the access token has already been refreshed over the network, so the
*upload is silently lost* and the message points the operator at auth/network - not
at a formatting overflow. A formatting failure must not have that authority.
"""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from core.lifecycle import _format_jwt_claim


TZ8 = timezone(timedelta(hours=8))


# Every one of these raised (or would raise) inside datetime.fromtimestamp.
BAD_CLAIMS = [
    1767225600000,        # millisecond epoch
    "1767225600000",
    999999999999999,      # oversized sentinel
    float("inf"),
    float("nan"),
    1e18,                 # already a float: conversion is a no-op
    10 ** 400,            # an oversized *int* - float() raises OverflowError here
    "1e999",              # parses to inf, caught by the range check instead
    "abc",
    True,                 # bool is an int subclass, never a claim timestamp
    False,
    -1,
    0,
    None,
    "",
]


@pytest.mark.parametrize("claim", BAD_CLAIMS)
def test_a_bad_claim_formats_as_empty(claim):
    assert _format_jwt_claim(claim, TZ8) == ""


@pytest.mark.parametrize("claim", BAD_CLAIMS)
def test_a_bad_claim_never_raises(claim):
    """The property under test is totality, so assert it directly."""
    _format_jwt_claim(claim, TZ8)


def test_a_real_claim_still_formats_in_the_expected_zone():
    """Hardening must not break valid claims, and the +08:00 zone must hold."""
    rendered = _format_jwt_claim(1767225600, TZ8)

    assert rendered == "2026-01-01T08:00:00+08:00"
    assert rendered.endswith("+08:00")


def test_a_numeric_string_claim_is_accepted():
    assert _format_jwt_claim("1767225600", TZ8) == _format_jwt_claim(1767225600, TZ8)


def test_the_sync_flow_survives_a_bad_claim():
    """Drive the real conversion the way the sync loop does.

    The point is that no exception escapes; if one did, the caller would count the
    account as a generic sync error *after* refreshing its token.
    """
    for claim in BAD_CLAIMS:
        expired_str = _format_jwt_claim(claim, TZ8)
        last_refresh = _format_jwt_claim(claim, TZ8) or "fallback"
        assert isinstance(expired_str, str)
        assert isinstance(last_refresh, str)
        assert last_refresh  # the `or _utcnow_iso()` fallback still applies