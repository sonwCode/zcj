"""Outcome branches of check_database that no test ever asserted.

The earlier "is every check referenced?" audit could see check_database only through
the row label asserted by test_full_run_produces_every_check - the check ran, but not
one of its seven outcomes was pinned.  The costliest omission is the last branch: a
bind-mounted volume routinely delivers the SQLite file root-owned and read-only while its
directory stays writable, so the directory check passes and SQLite only fails on the first
write, after the service has already reported a clean start.
"""
from __future__ import annotations

from scripts import cloud_preflight as preflight


def _row(report, name):
    rows = [row for row in report.rows if row[1] == name]
    assert rows, report.rows
    return rows[0]


def _db(monkeypatch, *, url):
    monkeypatch.setenv("ACCOUNT_MANAGER_DATABASE_URL", url)


def _tree(monkeypatch, *, isdir=True, dir_writable=True, exists=True, file_writable=True):
    monkeypatch.setattr(preflight.os.path, "isdir", lambda _p: isdir)
    monkeypatch.setattr(preflight.os.path, "exists", lambda _p, _e=exists: _e)
    monkeypatch.setattr(preflight.os, "access", lambda p, _mode: dir_writable if p == "/data" else file_writable)


def test_a_non_sqlite_url_is_informational(monkeypatch):
    """Postgres deployments must not be reported as a broken SQLite volume."""
    _db(monkeypatch, url="postgresql://user@db/app")
    report = preflight.Report()
    preflight.check_database(report)

    status, _name, detail, _hint = _row(report, "数据库")
    assert status == preflight.INFO
    assert "SQLite" in detail
    assert report.failures == 0


def test_a_missing_data_directory_fails(monkeypatch):
    _db(monkeypatch, url="sqlite:////data/account_manager.db")
    _tree(monkeypatch, isdir=False)
    report = preflight.Report()
    preflight.check_database(report)

    status, _name, detail, hint = _row(report, "数据库")
    assert status == preflight.FAIL
    assert "/data" in detail
    assert hint


def test_an_unwritable_data_directory_fails(monkeypatch):
    """A volume mounted read-only is the classic compose mistake."""
    _db(monkeypatch, url="sqlite:////data/account_manager.db")
    _tree(monkeypatch, dir_writable=False)
    report = preflight.Report()
    preflight.check_database(report)

    status, _name, detail, hint = _row(report, "数据库")
    assert status == preflight.FAIL
    assert "不可写" in detail
    assert hint


def test_a_writable_directory_passes(monkeypatch):
    _db(monkeypatch, url="sqlite:////data/account_manager.db")
    _tree(monkeypatch, exists=False)
    report = preflight.Report()
    preflight.check_database(report)

    status, _name, detail, hint = _row(report, "数据库")
    assert status == preflight.PASS
    assert detail == "/data"
    assert hint == ""
    assert report.failures == 0


def test_an_absent_database_file_is_informational(monkeypatch):
    """First boot: the directory is writable and the file is created on startup."""
    _db(monkeypatch, url="sqlite:////data/account_manager.db")
    _tree(monkeypatch, exists=False)
    report = preflight.Report()
    preflight.check_database(report)

    status, _name, detail, _hint = _row(report, "数据库文件")
    assert status == preflight.INFO
    assert "尚未创建" in detail
    assert report.failures == 0


def test_a_read_only_database_file_fails(monkeypatch):
    """The branch that matters: writable directory, read-only file.

    SQLite opens fine and fails on the first write, so without this assertion the gate
    would report a clean start on a deployment that cannot persist anything.
    """
    _db(monkeypatch, url="sqlite:////data/account_manager.db")
    _tree(monkeypatch, file_writable=False)
    report = preflight.Report()
    preflight.check_database(report)

    status, _name, detail, hint = _row(report, "数据库文件")
    assert status == preflight.FAIL
    assert "不可写" in detail
    assert "权限" in hint


def test_a_writable_database_file_is_informational(monkeypatch):
    """A present, writable file is reported with its size - not as a pass."""
    _db(monkeypatch, url="sqlite:////data/account_manager.db")
    _tree(monkeypatch)
    monkeypatch.setattr(preflight.os.path, "getsize", lambda _p: 2048)
    report = preflight.Report()
    preflight.check_database(report)

    status, _name, detail, _hint = _row(report, "数据库文件")
    assert status == preflight.INFO
    assert "2.0KB" in detail
    assert report.failures == 0
