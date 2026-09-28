"""A read-only database *file* must fail the preflight, not pass it.

``check_database`` only asked whether the containing directory was writable.  On a
bind-mounted data volume the common shape is the opposite: the directory is writable but
``account_manager.db`` arrived root-owned and read-only.  The preflight then printed

    数据库      /data          PASS
    数据库文件   .../account_manager.db (8.0KB)

with zero failures, while the application died on its first write with
``sqlite3.OperationalError: attempt to write a readonly database``.

That is the gate failing open on the exact condition it exists to catch, so the existing
file - not just its directory - has to be checked when it is already there.
"""
from __future__ import annotations

import os

import pytest

from scripts import cloud_preflight as preflight


def _rows(report):
    return [(row[0], row[1]) for row in report.rows]


def _touch_db(path):
    """Create a real, empty SQLite file so the check sees an existing database."""
    import sqlite3

    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.commit()
    connection.close()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason=(
        "root bypasses the permission bits: chmod 0444 leaves the file writable for uid 0, "
        "so os.access(..., os.W_OK) is correctly true and check_database correctly reports it "
        "as writable. A non-root runner exercises this assertion."
    ),
)
def test_a_read_only_existing_database_is_a_failure(monkeypatch, tmp_path):
    path = tmp_path / "account_manager.db"
    _touch_db(path)
    os.chmod(str(path), 0o444)
    monkeypatch.setenv("ACCOUNT_MANAGER_DATABASE_URL", "sqlite:///" + str(path))
    report = preflight.Report()

    try:
        preflight.check_database(report)
    finally:
        os.chmod(str(path), 0o644)

    assert (preflight.FAIL, "数据库文件") in _rows(report), report.rows


def test_a_writable_existing_database_still_passes(monkeypatch, tmp_path):
    """Hardening must not turn the normal case into a failure."""
    path = tmp_path / "account_manager.db"
    _touch_db(path)
    monkeypatch.setenv("ACCOUNT_MANAGER_DATABASE_URL", "sqlite:///" + str(path))
    report = preflight.Report()

    preflight.check_database(report)

    assert preflight.FAIL not in [status for status, _ in _rows(report)], report.rows
    assert (preflight.PASS, "数据库") in _rows(report), report.rows


def test_a_not_yet_created_database_only_needs_a_writable_directory(monkeypatch, tmp_path):
    """On first boot the file does not exist; the directory is the right thing to check."""
    monkeypatch.setenv("ACCOUNT_MANAGER_DATABASE_URL", "sqlite:///" + str(tmp_path / "new.db"))
    report = preflight.Report()

    preflight.check_database(report)

    assert preflight.FAIL not in [status for status, _ in _rows(report)], report.rows
