"""Outcome branches the preflight gate could take with nothing watching them.

A check is only as good as its worst branch.  Three of them had an outcome no test ever
asserted - a state the earlier "is every check referenced?" audit could not see, because
``check_disk`` was referenced and still had *neither* of its outcomes checked:

* ``check_disk`` warns below 2GB free.  Inverted, a full disk - which breaks SQLite writes
  mid-run - would pass preflight, and no test would have noticed.
* ``check_vnc`` passes only on a loopback bind.  The WARN for a public bind is the whole
  point of the check, and it was unasserted.
* ``check_timezone_data`` fails when a region cannot be resolved.  That FAIL is what keeps
  a payload from claiming GMT+0000 behind a proxy that exited elsewhere; unasserted, the
  single most valuable check in the file could stop firing silently.
"""
from __future__ import annotations

import collections

import pytest

from scripts import cloud_preflight as preflight


_Usage = collections.namedtuple("_Usage", "total used free")


def _row(report, name):
    rows = [row for row in report.rows if row[1] == name]
    assert rows, report.rows
    return rows[0]


def _disk(monkeypatch, *, free, total=64 * 1024 ** 3, raises=None):
    def fake_disk_usage(_target):
        if raises is not None:
            raise raises
        return _Usage(total, total - free, free)

    monkeypatch.setattr(preflight.shutil, "disk_usage", fake_disk_usage)


def test_ample_disk_passes(monkeypatch):
    _disk(monkeypatch, free=preflight.DISK_WARN_BYTES)
    report = preflight.Report()
    preflight.check_disk(report)

    status, _name, detail, hint = _row(report, "磁盘空间")
    assert status == preflight.PASS
    assert hint == ""
    assert report.warnings == 0


def test_low_disk_warns(monkeypatch):
    """One byte under the threshold is the first size worth complaining about."""
    _disk(monkeypatch, free=preflight.DISK_WARN_BYTES - 1)
    report = preflight.Report()
    preflight.check_disk(report)

    status, _name, detail, hint = _row(report, "磁盘空间")
    assert status == preflight.WARN
    assert "剩余" in detail
    assert hint


def test_unreadable_disk_warns_with_the_reason(monkeypatch):
    _disk(monkeypatch, free=0, raises=OSError("no such device"))
    report = preflight.Report()
    preflight.check_disk(report)

    status, _name, detail, _hint = _row(report, "磁盘空间")
    assert status == preflight.WARN
    assert "no such device" in detail


def _vnc(monkeypatch, *, password="secret", bind="127.0.0.1", insecure=None):
    monkeypatch.setenv("VNC_ENABLED", "1")
    monkeypatch.setenv("VNC_BIND", bind)
    if password is None:
        monkeypatch.delenv("VNC_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("VNC_PASSWORD", password)
    if insecure is None:
        monkeypatch.delenv("ZCJ_ALLOW_INSECURE_VNC", raising=False)
    else:
        monkeypatch.setenv("ZCJ_ALLOW_INSECURE_VNC", insecure)


def test_vnc_on_loopback_passes(monkeypatch):
    _vnc(monkeypatch)
    report = preflight.Report()
    preflight.check_vnc(report)

    status, _name, detail, hint = _row(report, "VNC")
    assert status == preflight.PASS
    assert "127.0.0.1" in detail
    assert hint == ""


def test_vnc_on_a_public_bind_warns(monkeypatch):
    """A non-loopback bind is reachable by anyone who can route to the host."""
    _vnc(monkeypatch, bind="0.0.0.0")
    report = preflight.Report()
    preflight.check_vnc(report)

    status, _name, detail, hint = _row(report, "VNC")
    assert status == preflight.WARN
    assert "0.0.0.0" in detail
    assert "回环" in hint or "SSH" in hint


def test_vnc_without_a_password_fails(monkeypatch):
    """x11vnc -nopw hands the logged-in browser session to anyone on the port."""
    _vnc(monkeypatch, password=None)
    report = preflight.Report()
    preflight.check_vnc(report)

    status, _name, _detail, hint = _row(report, "VNC")
    assert status == preflight.FAIL
    assert hint


def _timezone(monkeypatch, *, regions, available):
    import core.identity_profile as profile

    monkeypatch.setattr(profile, "_REGIONS", regions, raising=False)
    monkeypatch.setattr(profile, "timezone_is_available", available, raising=False)


def test_an_unresolvable_region_fails(monkeypatch):
    """This is the failure that lets a payload claim GMT+0000 behind a distant proxy."""
    _timezone(monkeypatch, regions={"us": ("US", "en", "America/New_York")}, available=lambda _z: False)
    report = preflight.Report()
    preflight.check_timezone_data(report)

    status, _name, detail, hint = _row(report, "时区库")
    assert status == preflight.FAIL
    assert "1/1" in detail
    assert "tzdata" in hint


def test_every_region_resolving_passes(monkeypatch):
    _timezone(monkeypatch, regions={"us": ("US", "en", "America/New_York")}, available=lambda _z: True)
    report = preflight.Report()
    preflight.check_timezone_data(report)

    status, _name, detail, _hint = _row(report, "时区库")
    assert status == preflight.PASS
    assert "1/1" in detail
    assert report.failures == 0
