"""The X display check must find the socket a given DISPLAY really uses.

X resolves ``:99`` and ``:99.0`` to the *same* unix socket, ``/tmp/.X11-unix/X99``: the
dot separates the display from the *screen*, and the screen is not part of the socket
name.  ``check_display`` appended the whole remainder after the colon, so a perfectly
usable ``DISPLAY=:99.0`` was reported as a WARN claiming the headful browser would fail to
start - contradicting reality, and teaching operators to ignore the warning that matters
when the socket truly is absent.
"""
from __future__ import annotations

import os

import pytest

from scripts import cloud_preflight as preflight


@pytest.fixture
def x11_dir(tmp_path, monkeypatch):
    """A stand-in for /tmp/.X11-unix with one live socket named X99."""
    monkeypatch.setattr(preflight, "X11_SOCKET_DIR", str(tmp_path))
    (tmp_path / "X99").write_text("")
    return tmp_path


def _row(report):
    return report.rows[0][:3]


@pytest.mark.parametrize("display", [":99", ":99.0", ":99.1"])
def test_a_screen_suffix_still_finds_the_display_socket(x11_dir, monkeypatch, display):
    """The screen number selects a screen, not a different socket."""
    monkeypatch.setenv("DISPLAY", display)
    report = preflight.Report()

    preflight.check_display(report)

    assert _row(report)[0] == preflight.PASS, _row(report)


def test_a_missing_display_is_still_warned_about(x11_dir, monkeypatch):
    """Hardening must not silence the real case this check exists for."""
    monkeypatch.setenv("DISPLAY", ":77")
    report = preflight.Report()

    preflight.check_display(report)

    assert _row(report)[0] == preflight.WARN, _row(report)


def test_an_unset_display_is_informational(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    report = preflight.Report()

    preflight.check_display(report)

    assert _row(report)[0] == preflight.INFO, _row(report)


@pytest.mark.parametrize(
    "display,expected",
    [
        (":99", "X99"),
        (":99.0", "X99"),
        (":0.0", "X0"),
        ("localhost:99", "X99"),
        ("127.0.0.1:99", "X99"),
        (":99.12", "X99"),
    ],
)
def test_the_socket_name_is_derived_correctly(display, expected):
    """A host prefix is a transport choice; the socket name carries neither it nor the screen."""
    assert preflight.x11_socket_name(display) == expected
