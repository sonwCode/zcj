r"""The usage-panel formatters must never raise.

``_format_reset_at`` / ``_format_maybe_timestamp`` run inside
``build_account_display_summary``, which the row-to-record mapper calls for every
account read. They are fed straight from the upstream Codex usage ``overview`` blob,
so a single unexpected number could take out the whole account listing rather than
just drop one label.

The values below are the ones that actually blow up in ``datetime.fromtimestamp``:
a millisecond epoch, an oversized sentinel, and ``float("inf")``. They raise
``ValueError`` / ``OSError`` / ``OverflowError`` - and the original guard only caught
``TypeError``/``ValueError`` around ``int()``, which is the wrong place entirely.
"""
from __future__ import annotations

import pytest

from core.account_display import _format_maybe_timestamp, _format_reset_at


# Every one of these raised before the fix.
PATHOLOGICAL = [
    1767225600000,            # millisecond epoch (year 57971 in seconds)
    "1767225600000",          # same, arriving as a JSON string
    999999999999999,          # oversized sentinel
    253402300799000000,       # out of range for the C library (OSError)
    float("inf"),
    float("nan"),
    float("1e18"),
    True,                     # bool is an int subclass, never a timestamp
    False,
]


@pytest.mark.parametrize("value", PATHOLOGICAL)
def test_a_pathological_timestamp_renders_as_empty(value):
    assert _format_reset_at(value) == ""
    assert _format_maybe_timestamp(value) == ""


def test_a_pathological_timestamp_does_not_escape_the_summary_builder():
    """The real blast radius: one account must not break the whole listing."""
    from core.account_display import build_account_display_summary

    for value in PATHOLOGICAL:
        summary = build_account_display_summary(
            platform="chatgpt",
            email="someone@example.com",
            lifecycle_status="registered",
            validity_status="unknown",
            plan_state="free",
            plan_name="",
            display_status="registered",
            overview={
                "next_reset_at": value,
                "primary_window": {"reset_at": value, "used_percent": 10},
            },
            provider_resources=[],
        )
        assert isinstance(summary, dict)


def test_a_real_timestamp_still_formats():
    """Hardening must not turn valid input into an empty cell."""
    assert _format_reset_at(1767225600) != ""
    assert _format_maybe_timestamp(1767225600) != ""
    assert _format_maybe_timestamp("1767225600") != ""
    assert _format_reset_at(1767225600) == _format_reset_at("1767225600")


def test_a_non_timestamp_string_is_passed_through_unchanged():
    """An ISO string is already displayable and must not be mangled."""
    assert _format_maybe_timestamp("2024-01-15T10:30:00Z") == "2024-01-15T10:30:00Z"
    assert _format_maybe_timestamp("abc") == "abc"


def test_zero_and_negative_render_empty():
    for value in (0, "0", -1, None, ""):
        assert _format_reset_at(value) == ""
        assert _format_maybe_timestamp(value) == ""


# --- the sibling guard in the same module: _quota_period_label -----------------


@pytest.mark.parametrize(
    "window_seconds",
    [float("inf"), float("-inf"), float("nan"), "abc", "2025-12-01", None],
)
def test_quota_period_label_survives_an_unusable_window(window_seconds):
    """``_quota_period_label`` reads the same stored overview blob.

    Its guard already caught ``TypeError``/``ValueError`` but not ``OverflowError``,
    which ``int(float("inf"))`` raises - and ``json.loads`` accepts ``Infinity``, so a
    stored overview can genuinely carry it.
    """
    from core.account_display import _quota_period_label

    label = _quota_period_label({"primary_window": {"limit_window_seconds": window_seconds}})

    assert isinstance(label, str) and label
