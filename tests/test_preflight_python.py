"""The interpreter check must reject a Python the code cannot actually run on.

``check_python`` gates the deployment on ``sys.version_info >= (3, 10)``.  Without a test,
a comparison that silently accepted 3.9 - or that crashed while formatting the version -
would let a cloud host through preflight and then fail at import time, which is the most
expensive place to discover it: after the service is already meant to be serving.

``sys.version_info`` is a structseq, so the stub below has to behave like one: the check
both compares it as a tuple and reads ``.major`` / ``.minor`` / ``.micro`` off it.
"""
from __future__ import annotations

import collections

import pytest

from scripts import cloud_preflight as preflight


_VersionInfo = collections.namedtuple("_VersionInfo", "major minor micro releaselevel serial")


@pytest.fixture
def fake_version(monkeypatch):
    """Replace sys.version_info with a structseq-alike the test controls."""

    def install(major, minor, micro):
        monkeypatch.setattr(
            preflight.sys,
            "version_info",
            _VersionInfo(major, minor, micro, "final", 0),
        )

    return install


def _row(report):
    return report.rows[0]


def test_python_below_the_floor_fails(fake_version):
    """3.9 cannot run X | None annotations at runtime, so it must not pass."""
    fake_version(3, 9, 18)
    report = preflight.Report()
    preflight.check_python(report)

    status, name, detail, hint = _row(report)
    assert status == preflight.FAIL
    assert name == "Python 版本"
    assert detail == "3.9.18"
    assert hint, "a failure has to say what to upgrade to"
    assert report.failures == 1


@pytest.mark.parametrize("version", [(3, 10, 0), (3, 11, 9), (3, 12, 1), (3, 13, 0)])
def test_python_at_or_above_the_floor_passes(fake_version, version):
    """The floor is inclusive: 3.10.0 itself is fine."""
    fake_version(*version)
    report = preflight.Report()
    preflight.check_python(report)

    status, name, detail, hint = _row(report)
    assert status == preflight.PASS
    assert detail == "%d.%d.%d" % version
    assert hint == ""
    assert report.failures == 0


def test_the_reported_version_is_the_one_that_ran(fake_version):
    """A wrong version in the report sends the operator to the wrong interpreter."""
    fake_version(3, 7, 11)
    report = preflight.Report()
    preflight.check_python(report)

    assert _row(report)[2] == "3.7.11"
