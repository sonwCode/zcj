"""Preflight checks for a cloud deployment.

These are the checks that catch a server-only failure before the service starts: an
empty APP_PASSWORD, a stripped image without tzdata, more than one uvicorn worker, and
an unauthenticated VNC listener.
"""
from __future__ import annotations

import pytest

from scripts import cloud_preflight as preflight


def _status(report, name: str) -> str:
    for status, row_name, _detail, _hint in report.rows:
        if row_name == name:
            return status
    raise AssertionError("check %r not found in %r" % (name, [r[1] for r in report.rows]))


def test_human_readable_sizes():
    assert preflight._human(512) == "512.0B"
    assert preflight._human(2048) == "2.0KB"
    assert preflight._human(64 * 1024 * 1024) == "64.0MB"
    assert preflight._human(3 * 1024 ** 3) == "3.0GB"


def test_report_counts_by_status():
    report = preflight.Report()
    report.add(preflight.PASS, "a")
    report.add(preflight.WARN, "b")
    report.add(preflight.FAIL, "c")
    report.add(preflight.FAIL, "d")
    assert (report.passes, report.warnings, report.failures) == (1, 1, 2)


def test_missing_password_is_a_failure(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("ZCJ_ALLOW_INSECURE", raising=False)
    report = preflight.Report()
    preflight.check_auth(report)
    assert _status(report, "APP_PASSWORD") == preflight.FAIL


def test_password_present_passes(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    report = preflight.Report()
    preflight.check_auth(report)
    assert _status(report, "APP_PASSWORD") == preflight.PASS


def test_explicit_insecure_opt_in_only_warns(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("ZCJ_ALLOW_INSECURE", "1")
    report = preflight.Report()
    preflight.check_auth(report)
    assert _status(report, "APP_PASSWORD") == preflight.WARN


@pytest.mark.parametrize("workers", ["2", "4", "8"])
def test_multiple_workers_is_a_failure(monkeypatch, workers):
    monkeypatch.setenv("UVICORN_WORKERS", workers)
    report = preflight.Report()
    preflight.check_single_process(report)
    assert _status(report, "进程模型") == preflight.FAIL


def test_single_worker_passes(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    report = preflight.Report()
    preflight.check_single_process(report)
    assert _status(report, "进程模型") == preflight.PASS


def test_disabling_both_retention_halves_warns(monkeypatch):
    monkeypatch.setenv("ZCJ_TASK_EVENT_RETENTION_DAYS", "0")
    monkeypatch.setenv("ZCJ_TASK_EVENT_MAX_ROWS", "0")
    report = preflight.Report()
    preflight.check_retention(report)
    assert _status(report, "日志保留") == preflight.WARN


def test_default_retention_passes(monkeypatch):
    monkeypatch.delenv("ZCJ_TASK_EVENT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("ZCJ_TASK_EVENT_MAX_ROWS", raising=False)
    report = preflight.Report()
    preflight.check_retention(report)
    assert _status(report, "日志保留") == preflight.PASS


def test_vnc_without_password_is_a_failure(monkeypatch):
    monkeypatch.setenv("VNC_ENABLED", "1")
    monkeypatch.delenv("VNC_PASSWORD", raising=False)
    monkeypatch.delenv("ZCJ_ALLOW_INSECURE_VNC", raising=False)
    report = preflight.Report()
    preflight.check_vnc(report)
    assert _status(report, "VNC") == preflight.FAIL


def test_vnc_disabled_is_informational(monkeypatch):
    monkeypatch.delenv("VNC_ENABLED", raising=False)
    report = preflight.Report()
    preflight.check_vnc(report)
    assert _status(report, "VNC") == preflight.INFO


def test_timezone_data_is_resolvable_on_this_host(monkeypatch):
    """A host without tzdata must be reported, not silently tolerated.

    ``js_date_string()`` catches the failure and falls back to UTC, which puts the
    Sentinel payload back at GMT+0000 while the proxy exit is elsewhere. This host has
    the zone database, so the check should pass - the failure path is exercised in CI
    by pointing PYTHONTZPATH at an empty directory.
    """
    report = preflight.Report()
    preflight.check_timezone_data(report)
    assert _status(report, "时区库") == preflight.PASS


def test_full_run_produces_every_check():
    report = preflight.run_checks()
    names = [row[1] for row in report.rows]
    for expected in (
        "Python 版本",
        "核心依赖",
        "时区库",
        "APP_PASSWORD",
        "进程模型",
        "X display",
        "浏览器",
        "/dev/shm",
        "数据库",
        "磁盘空间",
        "日志保留",
        "VNC",
    ):
        assert expected in names, "missing check: %s" % expected
    assert report.render()
