"""The cloud preflight must inspect the database the application will really open.

``scripts/cloud_preflight.py`` is the pre-deploy gate: it is what tells an operator that
the data volume is missing or read-only *before* the service starts and silently writes to
container-local storage.  Its ``_database_path()`` only looked at
``ACCOUNT_MANAGER_DATABASE_URL``, so on the documented default configuration - variable
unset, SQLite next to the checkout - it returned "" and the whole SQLite section was
skipped with an INFO line:

    数据库  非 SQLite 或未配置   跳过 SQLite 专项检查。

That is the gate failing open on the most common setup, and ``check_disk`` then measured
the working directory instead of the volume that actually holds the database.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from core.storage import default_database_url
from scripts import cloud_preflight as preflight


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_VAR = "ACCOUNT_MANAGER_DATABASE_URL"


def _rows(report):
    return [(row[0], row[1]) for row in report.rows]


def test_an_unset_variable_falls_back_to_the_real_default_database(monkeypatch):
    """Unset means "use the default", not "no database to check".

    The preflight must resolve the same path the application will open.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)

    resolved = preflight._database_path()

    assert resolved, "an unset variable must not skip the SQLite checks"
    assert resolved == default_database_url()[len("sqlite:///"):]


def test_an_unset_variable_still_produces_a_real_database_verdict(monkeypatch):
    """The section must report PASS/FAIL, never silently opt out."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    report = preflight.Report()

    preflight.check_database(report)

    statuses = [status for status, name in _rows(report) if name == "数据库"]
    assert statuses, _rows(report)
    assert preflight.INFO not in statuses, _rows(report)


def test_a_missing_database_directory_is_a_failure(monkeypatch, tmp_path):
    """The whole point of the gate: an unmounted volume must FAIL, not pass quietly."""
    missing = tmp_path / "not-mounted" / "account_manager.db"
    monkeypatch.setenv(ENV_VAR, "sqlite:///" + str(missing))
    report = preflight.Report()

    preflight.check_database(report)

    assert (preflight.FAIL, "数据库") in _rows(report), _rows(report)


def test_a_writable_database_directory_passes(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, "sqlite:///" + str(tmp_path / "account_manager.db"))
    report = preflight.Report()

    preflight.check_database(report)

    assert (preflight.PASS, "数据库") in _rows(report), _rows(report)


def test_a_non_sqlite_url_still_skips_the_sqlite_section(monkeypatch):
    """PostgreSQL has no directory to inspect; skipping is correct there."""
    monkeypatch.setenv(ENV_VAR, "postgresql://u:p@h/db")
    report = preflight.Report()

    preflight.check_database(report)

    assert _rows(report) == [(preflight.INFO, "数据库")]


def test_disk_space_is_measured_where_the_database_lives(monkeypatch, tmp_path):
    """Measuring the cwd would report free space for the wrong filesystem."""
    monkeypatch.setenv(ENV_VAR, "sqlite:///" + str(tmp_path / "account_manager.db"))
    seen = {}
    real = preflight.shutil.disk_usage

    def _spy(path):
        seen["path"] = path
        return real(path)

    monkeypatch.setattr(preflight.shutil, "disk_usage", _spy)
    preflight.check_disk(preflight.Report())

    assert seen.get("path") == str(tmp_path)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_variable_is_treated_as_unset(monkeypatch, blank):
    """A compose substitution that expands to spaces must not skip the gate.

    ``core.storage.database_url`` strips the same setting, so the preflight has to strip
    it too or the two disagree about which database is in play.
    """
    monkeypatch.setenv(ENV_VAR, blank)

    resolved = preflight._database_path()

    assert resolved == default_database_url()[len("sqlite:///"):]
