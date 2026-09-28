"""The preflight must judge APP_PASSWORD the same way the entrypoint does.

``docker-entrypoint.sh`` refuses to start only when the bind address is *not* loopback
and ``APP_PASSWORD`` is empty; a loopback-only deployment is explicitly allowed
(the entrypoint even names ``APP_HOST=127.0.0.1`` as the escape hatch, and
``docs/cloud-deployment.md`` states the rule the same way).

``check_auth`` ignored ``APP_HOST``, so ``APP_HOST=127.0.0.1`` with no password was
reported as FAIL - the tool contradicting the very gate it exists to explain, and telling
the operator credentials are exposed to "anyone who can reach the port" when the port is
only reachable on loopback.
"""
from __future__ import annotations

import pytest

from scripts import cloud_preflight as preflight


def _status(report):
    return [row[0] for row in report.rows if row[1] == "APP_PASSWORD"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("APP_PASSWORD", "ZCJ_ALLOW_INSECURE", "APP_HOST"):
        monkeypatch.delenv(name, raising=False)


def test_a_password_makes_the_check_pass(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    report = preflight.Report()

    preflight.check_auth(report)

    assert _status(report) == [preflight.PASS]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_a_loopback_bind_without_a_password_is_not_a_failure(monkeypatch, host):
    """The entrypoint starts in this configuration, so the preflight must not claim FAIL."""
    monkeypatch.setenv("APP_HOST", host)
    report = preflight.Report()

    preflight.check_auth(report)

    assert _status(report) == [preflight.WARN], report.rows


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_a_public_bind_without_a_password_is_a_failure(monkeypatch, host):
    monkeypatch.setenv("APP_HOST", host)
    report = preflight.Report()

    preflight.check_auth(report)

    assert _status(report) == [preflight.FAIL], report.rows


def test_the_entrypoint_default_bind_is_judged_public(monkeypatch):
    """APP_HOST unset means the entrypoint uses 0.0.0.0, so it must FAIL."""
    report = preflight.Report()

    preflight.check_auth(report)

    assert _status(report) == [preflight.FAIL], report.rows


def test_the_insecure_escape_hatch_is_still_only_a_warning(monkeypatch):
    monkeypatch.setenv("ZCJ_ALLOW_INSECURE", "1")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    report = preflight.Report()

    preflight.check_auth(report)

    assert _status(report) == [preflight.WARN], report.rows


def test_a_password_wins_over_a_public_bind(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    report = preflight.Report()

    preflight.check_auth(report)

    assert _status(report) == [preflight.PASS], report.rows
