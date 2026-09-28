"""The Sentinel self-check and the preflight gate built on it.

Two failure modes are pinned here, and they are not symmetric.

The loud one: ZCJ reimplements the OpenAI Sentinel SDK VM in Python, so a new
``sdk.js`` shape makes the payload structurally wrong, the server rejects it, and the
registration dies as a generic signup error - after the proxy and the mailbox were
already paid for. ``check_sentinel`` exists to make that visible before procurement.

The quiet one: a check that verified nothing must never report success.
``PreflightReport.ok`` only inspects ``required`` checks, and ``SentinelReport.ok`` is
``all()`` over its own tuple - which is vacuously true when that tuple is empty. The
reachable fail-open path (``check_sentinel`` cannot import the module at all) is
deliberate and marked ``required=False``; the vacuous one is not reachable today and
is guarded so it stays that way.
"""
from __future__ import annotations

import base64
import json

import pytest

from core.registration import preflight, sentinel_check


# -- report semantics ---------------------------------------------------------


def test_a_failing_check_makes_the_report_fail():
    report = sentinel_check.SentinelReport((
        sentinel_check.SentinelCheck("prefix", True, "ok"),
        sentinel_check.SentinelCheck("shape", False, "字段数 18，期望 19"),
    ))
    assert report.ok is False
    assert [c.name for c in report.failures] == ["shape"]


def test_the_summary_names_every_failure():
    report = sentinel_check.SentinelReport((
        sentinel_check.SentinelCheck("a", False, "第一个坏了"),
        sentinel_check.SentinelCheck("b", True, "fine"),
        sentinel_check.SentinelCheck("c", False, "第三个坏了"),
    ))
    summary = report.summary()
    assert "a: 第一个坏了" in summary and "c: 第三个坏了" in summary
    assert "fine" not in summary


def test_a_passing_report_summarises_as_a_pass():
    report = sentinel_check.SentinelReport((sentinel_check.SentinelCheck("x", True),))
    assert report.ok is True and "全部通过" in report.summary()


def test_an_empty_report_is_vacuously_ok():
    """Documents the hazard rather than endorsing it: nothing was verified here."""
    empty = sentinel_check.SentinelReport(())
    assert empty.ok is True
    assert empty.failures == ()
    assert "0 项" in empty.summary()


def test_the_self_test_never_returns_an_empty_report():
    """The guard that keeps the vacuous case from ever becoming reachable."""
    report = sentinel_check.run_sentinel_self_test()
    assert report.checks, "a report with no checks would advertise success without testing"


def test_a_report_serialises_for_the_api():
    payload = sentinel_check.SentinelReport((
        sentinel_check.SentinelCheck("a", True, "ok"),
        sentinel_check.SentinelCheck("b", False, "bad"),
    )).to_dict()
    assert payload["ok"] is False
    assert payload["failures"] == ["b"]
    assert [c["name"] for c in payload["checks"]] == ["a", "b"]
    assert payload["checks"][1] == {"name": "b", "ok": False, "detail": "bad"}
    assert isinstance(payload["summary"], str)


def test_a_check_is_frozen():
    check = sentinel_check.SentinelCheck("a", True)
    with pytest.raises(Exception):
        check.ok = False  # type: ignore[misc]


# -- helpers ------------------------------------------------------------------


def test_decode_unwraps_the_requirements_prefix():
    body = json.dumps(["a", "b"])
    token = sentinel_check.REQ_PREFIX + base64.b64encode(body.encode()).decode()
    assert sentinel_check._decode(token) == ["a", "b"]


def test_decode_rejects_a_payload_that_is_not_json():
    with pytest.raises(Exception):
        sentinel_check._decode(sentinel_check.REQ_PREFIX + base64.b64encode(b"nope").decode())


@pytest.mark.parametrize("text,expected", [
    ("Mon Jan 01 2024 12:00:00 GMT+0900 (Japan Standard Time)", "+0900"),
    ("Mon Jan 01 2024 12:00:00 GMT-0400 (Eastern Standard Time)", "-0400"),
    ("GMT+0000", "+0000"),
    ("no offset in this string", ""),
    ("", ""),
    (None, ""),
])
def test_offset_extraction(text, expected):
    assert sentinel_check._offset_of(text) == expected


def test_the_sdk_stamp_reports_what_the_solver_was_written_against():
    stamp = sentinel_check.sentinel_sdk_stamp()
    assert set(stamp) == {"sdk_version", "frame_version", "sdk_url", "payload_length"}
    assert stamp["payload_length"] == sentinel_check.PAYLOAD_LENGTH
    assert stamp["sdk_url"].endswith("/sdk.js")


# -- the self test ------------------------------------------------------------


EXPECTED_CHECKS = {
    "prefix", "decode", "shape", "screen", "date_format", "timezone",
    "user_agent", "client_hints", "timezone_data", "sdk_url", "locale",
    "clock", "pow",
}


def test_the_self_test_passes_offline_and_names_every_check():
    """It must stay offline and cheap: it runs before any resource is leased."""
    report = sentinel_check.run_sentinel_self_test()
    assert report.ok is True, report.summary()
    assert {c.name for c in report.checks} == EXPECTED_CHECKS


def test_the_self_test_token_is_a_requirements_token():
    """payload[0..] is index-addressed, so the shape check is what protects the rest."""
    report = sentinel_check.run_sentinel_self_test()
    prefix = next(c for c in report.checks if c.name == "prefix")
    assert prefix.ok is True
    assert "gAAAAAC" in prefix.detail


def test_the_self_test_reports_a_coherent_shape():
    report = sentinel_check.run_sentinel_self_test()
    shape = next(c for c in report.checks if c.name == "shape")
    assert shape.ok is True
    assert str(sentinel_check.PAYLOAD_LENGTH) in shape.detail


def test_the_payload_timezone_matches_the_resolved_profile():
    """The contradiction this catches: a payload that claims UTC for a non-UTC exit."""
    report = sentinel_check.run_sentinel_self_test()
    tz = next(c for c in report.checks if c.name == "timezone")
    assert tz.ok is True
    assert "?" not in tz.detail


def test_the_pow_check_demands_a_solved_token():
    report = sentinel_check.run_sentinel_self_test()
    pow_check = next(c for c in report.checks if c.name == "pow")
    assert pow_check.ok is True
    assert sentinel_check.POW_DIFFICULTY in pow_check.detail


def test_an_unimportable_solver_is_reported_as_a_failing_import_check(monkeypatch):
    """The loud path: the module imports but the platform solver does not."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "platforms.chatgpt.register":
            raise ImportError("no solver")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    report = sentinel_check.run_sentinel_self_test()
    assert report.ok is False
    assert [c.name for c in report.checks] == ["import"]


# -- the preflight gate -------------------------------------------------------


def test_an_optional_failure_does_not_fail_the_preflight():
    """The distinguishing case: every required check passes, only the optional one fails.

    A report where both kinds fail cannot tell the two implementations apart, so the
    optional-only failure is the one that actually pins the required flag.
    """
    report = preflight.PreflightReport((
        preflight.PreflightCheck("must", True, "fine", required=True),
        preflight.PreflightCheck("may", False, "broken", required=False),
    ))
    assert report.ok is True
    assert report.failures == ()
    assert [c.name for c in report.checks] == ["must", "may"]


def test_a_required_failure_fails_the_preflight():
    report = preflight.PreflightReport((
        preflight.PreflightCheck("must", False, "broken", required=True),
        preflight.PreflightCheck("may", False, "also broken", required=False),
    ))
    assert report.ok is False
    assert [c.name for c in report.failures] == ["must"]


def test_a_failing_sentinel_self_test_fails_the_preflight(monkeypatch):
    monkeypatch.setattr(
        sentinel_check, "run_sentinel_self_test",
        lambda *a, **k: sentinel_check.SentinelReport(
            (sentinel_check.SentinelCheck("shape", False, "字段数 18，期望 19"),)
        ),
    )
    check = preflight.check_sentinel()
    assert check.name == "sentinel"
    assert check.ok is False
    assert check.required is True, "a broken solver must be able to stop procurement"
    assert "shape" in check.detail


def test_a_healthy_solver_passes_the_preflight():
    check = preflight.check_sentinel()
    assert (check.ok, check.required) == (True, True)


def test_an_unimportable_check_module_skips_instead_of_failing(monkeypatch):
    """Deliberate fail-open: a diagnostic must not block registration by itself."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "core.registration.sentinel_check":
            raise ImportError("gone")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    check = preflight.check_sentinel()
    assert check.ok is True and check.required is False
    assert "跳过" in check.detail


def test_sentinel_is_opt_in_and_off_by_default():
    names = [c.name for c in preflight.run_preflight().checks]
    assert "sentinel" not in names
    with_sentinel = [c.name for c in preflight.run_preflight(sentinel=True).checks]
    assert "sentinel" in with_sentinel


def test_the_base_preflight_checks_are_always_present():
    names = [c.name for c in preflight.run_preflight().checks]
    assert names[:3] == ["python", "curl_cffi", "http_stack"]
