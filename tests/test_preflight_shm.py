"""The /dev/shm check must warn before Chrome dies, not after.

Docker gives a container 64MB of /dev/shm unless told otherwise, and Chromium crashes once
its renderer runs out - typically several tabs into a registration run, long after preflight
said everything was fine.  The check exists to catch that up front, so its four outcomes all
matter: a missing /dev/shm (non-Linux) is informational, an unreadable one is a warning, a
small one is a warning, and only a comfortably sized one passes.
"""
from __future__ import annotations

import collections

import pytest

from scripts import cloud_preflight as preflight


_Usage = collections.namedtuple("_Usage", "total used free")


def _install(monkeypatch, *, exists=True, total=None, raises=None):
    monkeypatch.setattr(preflight.os.path, "isdir", lambda _path: exists)

    def fake_disk_usage(_path):
        if raises is not None:
            raise raises
        return _Usage(total, 0, total)

    monkeypatch.setattr(preflight.shutil, "disk_usage", fake_disk_usage)


def _row(report):
    return report.rows[0]


def test_absent_shm_is_informational_not_a_failure(monkeypatch):
    """macOS has no /dev/shm; that must not fail the gate on a developer machine."""
    _install(monkeypatch, exists=False)
    report = preflight.Report()
    preflight.check_shm(report)

    status, name, _detail, hint = _row(report)
    assert status == preflight.INFO
    assert name == "/dev/shm"
    assert report.failures == 0
    assert hint


def test_unreadable_shm_warns_with_the_reason(monkeypatch):
    """An unreadable /dev/shm is unknown, not fine - say why rather than passing it."""
    _install(monkeypatch, exists=True, raises=OSError("permission denied"))
    report = preflight.Report()
    preflight.check_shm(report)

    status, _name, detail, _hint = _row(report)
    assert status == preflight.WARN
    assert "permission denied" in detail


def test_small_shm_warns_and_names_the_fix(monkeypatch):
    """64MB is the Docker default and the reason registrations crash mid-run."""
    _install(monkeypatch, exists=True, total=64 * 1024 * 1024)
    report = preflight.Report()
    preflight.check_shm(report)

    status, _name, detail, hint = _row(report)
    assert status == preflight.WARN
    assert detail == "64.0MB"
    assert "shm_size" in hint


def test_the_threshold_is_inclusive(monkeypatch):
    """SHM_WARN_BYTES is the first acceptable size, so the boundary must pass."""
    _install(monkeypatch, exists=True, total=preflight.SHM_WARN_BYTES)
    report = preflight.Report()
    preflight.check_shm(report)

    assert _row(report)[0] == preflight.PASS

    _install(monkeypatch, exists=True, total=preflight.SHM_WARN_BYTES - 1)
    under = preflight.Report()
    preflight.check_shm(under)

    assert _row(under)[0] == preflight.WARN


def test_ample_shm_passes(monkeypatch):
    """A 1GB /dev/shm, as the compose file is meant to set, is a pass."""
    _install(monkeypatch, exists=True, total=1024 * 1024 * 1024)
    report = preflight.Report()
    preflight.check_shm(report)

    status, _name, detail, hint = _row(report)
    assert status == preflight.PASS
    assert detail == "1.0GB"
    assert hint == ""
    assert report.warnings == 0
