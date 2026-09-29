"""The storage backend decides where production data lives.

``core/storage.py`` chooses the default database path and classifies the configured
dialect; ``core/db.py`` builds its engine from that choice at import time.  None of it
had a test, and the failure mode is quiet rather than loud: a default path that followed
the process working directory would have a cloud deployment (systemd/docker start the
process somewhere else) create a *second*, empty database and silently lose every account.

The default is therefore pinned from another working directory in a subprocess - that is
the only way to observe the path the module actually produces on a real startup.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest

from core.storage import (
    DATABASE_URL_ENV,
    database_url,
    default_database_url,
    describe,
    dialect_of,
    is_postgres,
    is_sqlite,
    is_sqlite_memory,
    secure_sqlite_file_permissions,
)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _probe(cwd, extra_env=None) -> str:
    """Run the module in a fresh interpreter and print the default it computes."""
    env = dict(os.environ)
    env.pop(DATABASE_URL_ENV, None)
    env.update(extra_env or {})
    env["PYTHONPATH"] = REPO
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from core.storage import default_database_url, database_url;"
            " print(default_database_url()); print(database_url())",
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) >= 2, result.stdout
    return lines[-2].strip() + "|" + lines[-1].strip()


def test_the_default_database_does_not_move_with_the_working_directory(tmp_path):
    """A deployment that starts elsewhere must reach the same database."""
    assert _probe(tmp_path) == _probe(REPO)


def test_the_default_database_is_inside_the_repository():
    url = default_database_url()

    assert url.startswith("sqlite:///")
    path = url[len("sqlite:///"):]
    assert os.path.abspath(path) == os.path.abspath(os.path.join(REPO, "account_manager.db"))


def test_an_explicit_url_wins_over_the_default(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite:////tmp/elsewhere.db")

    assert database_url() == "sqlite:////tmp/elsewhere.db"


def test_a_blank_setting_is_treated_as_unset():
    """An empty substitution in a compose file must not produce a broken URL."""
    assert _probe(REPO, {DATABASE_URL_ENV: "   "}).endswith(default_database_url())


@pytest.mark.parametrize(
    "url,expected",
    [
        ("sqlite:///x.db", "sqlite"),
        ("sqlite+pysqlite:///x.db", "sqlite"),
        ("SQLITE:///x.db", "sqlite"),
        ("postgresql://u:p@h/db", "postgresql"),
        ("postgres://u:p@h/db", "postgresql"),
        ("postgresql+psycopg://u:p@h/db", "postgresql"),
        ("mysql://u@h/db", "mysql"),
        ("weird", ""),
    ],
)
def test_the_dialect_is_classified(url, expected):
    assert dialect_of(url) == expected


@pytest.mark.parametrize(
    "url",
    ["sqlite://", "sqlite:///:memory:", "sqlite+pysqlite:///:memory:", "sqlite:///x.db"],
)
def test_sqlite_memory_detection(url):
    assert is_sqlite_memory(url) is ("memory" in url or url == "sqlite://")


def test_sqlite_database_sidecars_are_restricted(tmp_path):
    database = tmp_path / "accounts.db"
    for path in (database, tmp_path / "accounts.db-wal", tmp_path / "accounts.db-shm"):
        path.write_bytes(b"fixture")
        os.chmod(path, 0o644)

    secure_sqlite_file_permissions(f"sqlite:///{database}")

    for path in (database, tmp_path / "accounts.db-wal", tmp_path / "accounts.db-shm"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_memory_sqlite_does_not_touch_filesystem_permissions(tmp_path):
    fixture = tmp_path / "memory.db"
    fixture.write_bytes(b"fixture")
    os.chmod(fixture, 0o644)

    secure_sqlite_file_permissions("sqlite:///:memory:")

    assert stat.S_IMODE(fixture.stat().st_mode) == 0o644


def test_an_empty_url_falls_back_to_the_default():
    """Callers pass "" to mean "not configured"; that must not read as unknown."""
    assert dialect_of("") == dialect_of(default_database_url())


def test_the_sqlite_backend_is_flagged_as_single_writer():
    backend = describe("sqlite:///x.db")

    assert backend.is_sqlite is True
    assert backend.supports_concurrent_writers is False
    # The legacy-schema rewrite in core.db uses SQLite PRAGMA and only runs here.
    assert backend.supports_inline_migrations is True


def test_the_postgres_backend_skips_the_sqlite_only_migration():
    backend = describe("postgresql://u:p@h/db")

    assert backend.is_sqlite is False
    assert backend.supports_concurrent_writers is True
    assert backend.supports_inline_migrations is False


def test_an_unknown_dialect_is_built_but_not_trusted():
    backend = describe("mysql://u@h/db")

    assert backend.name == "mysql"
    assert backend.supports_inline_migrations is False


def test_describe_reports_the_url_it_was_given():
    backend = describe("sqlite:////tmp/x.db")

    assert backend.url == "sqlite:////tmp/x.db"
    assert "sqlite" in backend.to_dict()["notes"][0] or backend.to_dict()["notes"]


@pytest.mark.parametrize("url", ["sqlite:///x.db", "postgresql://u:p@h/db", "mysql://u@h/db"])
def test_the_predicates_agree_with_the_dialect(url):
    dialect = dialect_of(url)

    assert is_sqlite(url) == (dialect == "sqlite")
    assert is_postgres(url) == (dialect == "postgresql")
