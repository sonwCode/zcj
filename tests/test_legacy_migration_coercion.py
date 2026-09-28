r"""Legacy rows must not be able to break the account-schema migration.

``_migrate_legacy_accounts_schema`` reads the old ``accounts`` table and rebounds every
row into the current graph. SQLite column affinity does not enforce a type, so a column
declared ``INTEGER`` can still hold ``TEXT`` written by an older version:

    accounts.trial_end_time = "2025-12-01"

The migration coerced that with a bare ``int(row["trial_end_time"] or 0)``, so one such
row raised ``ValueError`` inside the per-row loop - aborting the migration for every
remaining row and, because ``init_db`` calls it during startup, taking the process with
it. The very next line already loaded ``extra_json`` from the same row through the total
``_load_json`` helper, so the guarding idiom was right there.
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

import core.account_graph as account_graph
import core.db as db
from core.db import _load_int


@pytest.mark.parametrize(
    "value,expected",
    [
        (1767225600, 1767225600),
        ("1767225600", 1767225600),
        (" 1767225600 ", 1767225600),
        (None, 0),
        ("", 0),
        (0, 0),
        ("2025-12-01", 0),
        ("abc", 0),
        ("12.5", 0),
        ([1], 0),
        ({"a": 1}, 0),
        (float("inf"), 0),
        (float("nan"), 0),
    ],
)
def test_load_int_is_total(value, expected):
    assert _load_int(value) == expected


def test_load_int_can_take_a_custom_default():
    assert _load_int("abc", default=-1) == -1


LEGACY_DDL = """
CREATE TABLE accounts (
    id INTEGER NOT NULL PRIMARY KEY,
    platform VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    password VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    region VARCHAR,
    token VARCHAR,
    status VARCHAR,
    trial_end_time INTEGER,
    cashier_url VARCHAR,
    extra_json VARCHAR
)
"""


@pytest.fixture()
def legacy_engine(tmp_path, monkeypatch):
    """A temp DB carrying the legacy schema, wired into ``core.db``."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS accounts")
        connection.exec_driver_sql(LEGACY_DDL)
    monkeypatch.setattr(db, "engine", engine)
    return engine


def _insert_legacy_row(engine, *, trial_end_time):
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO accounts (platform, email, password, user_id, created_at,"
            " updated_at, trial_end_time) VALUES ('chatgpt', 'a@b.c', 'p', 'u',"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :t)",
            {"t": trial_end_time},
        )


def test_a_text_trial_end_time_does_not_break_the_migration(legacy_engine):
    """The regression: this raised ValueError and aborted the whole migration."""
    _insert_legacy_row(legacy_engine, trial_end_time="2025-12-01")

    db._migrate_legacy_accounts_schema()

    with legacy_engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(accounts)")}
    assert "trial_end_time" not in columns  # legacy column retired by the rebuild
    assert {"id", "platform", "email", "password", "user_id"} <= columns


def test_a_malformed_row_does_not_stop_the_following_rows(legacy_engine, monkeypatch):
    """One bad row must not rob every later row of its migration."""
    recorded: list[int] = []
    real_sync = account_graph.sync_legacy_account_graph

    def _recording_sync(session, *, trial_end_time=0, **kwargs):
        recorded.append(trial_end_time)
        return real_sync(session, trial_end_time=trial_end_time, **kwargs)

    monkeypatch.setattr(account_graph, "sync_legacy_account_graph", _recording_sync)
    _insert_legacy_row(legacy_engine, trial_end_time="2025-12-01")
    _insert_legacy_row(legacy_engine, trial_end_time=1767225600)
    _insert_legacy_row(legacy_engine, trial_end_time="abc")

    db._migrate_legacy_accounts_schema()

    assert recorded == [0, 1767225600, 0]


def test_a_numeric_trial_end_time_is_preserved(legacy_engine, monkeypatch):
    recorded: list[int] = []
    real_sync = account_graph.sync_legacy_account_graph

    def _recording_sync(session, *, trial_end_time=0, **kwargs):
        recorded.append(trial_end_time)
        return real_sync(session, trial_end_time=trial_end_time, **kwargs)

    monkeypatch.setattr(account_graph, "sync_legacy_account_graph", _recording_sync)
    _insert_legacy_row(legacy_engine, trial_end_time=1767225600)

    db._migrate_legacy_accounts_schema()

    assert recorded == [1767225600]
