"""The preflight must say whether credentials are actually encrypted at rest.

``core/vault.py`` encrypts stored account passwords, provider keys, mailbox tokens and
proxy credentials, and it documents its own failure mode as "degrades to pass-through".
That is quiet on the write path: with PyNaCl missing, ``encrypt_value("hunter2")`` returns
``"hunter2"`` and the application runs normally while every credential is stored in
plaintext.  The only surface for it is ``vault_status()`` behind an authenticated API, so
a deployment with a broken image or an unwritable key file looks healthy.

The preflight exists for exactly this ("把环境问题打进日志而不是让它变成线上才发现的静默退化"),
so it has to report the vault state at deploy time.
"""
from __future__ import annotations

import builtins
import os

import pytest

from core import vault as vault_module
from scripts import cloud_preflight as preflight


def _row(report):
    rows = [row for row in report.rows if row[1] == "凭据加密"]
    assert rows, report.rows
    return rows[0]


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Point the vault at a temp database and forget any earlier state."""
    monkeypatch.setenv("ACCOUNT_MANAGER_DATABASE_URL", "sqlite:///" + str(tmp_path / "x.db"))
    monkeypatch.delenv("ZCJ_VAULT_KEY", raising=False)
    monkeypatch.delenv("ZCJ_VAULT_KEY_FILE", raising=False)
    monkeypatch.delenv("ZCJ_VAULT_DISABLED", raising=False)
    vault_module._VAULT.__init__()
    yield
    monkeypatch.undo()
    vault_module._VAULT.__init__()


def test_a_working_vault_passes(isolated_vault):
    report = preflight.Report()

    preflight.check_vault(report)

    row = _row(report)
    assert row[0] == preflight.PASS, row
    assert "nacl" in row[2], row


def test_a_missing_pynacl_is_a_failure(isolated_vault, monkeypatch):
    """Credentials would be written as plaintext and nothing else would complain."""
    real_import = builtins.__import__

    def _no_nacl(name, *args, **kwargs):
        if name.startswith("nacl"):
            raise ImportError("no nacl")
        return real_import(name, *args, **kwargs)

    vault_module._VAULT.__init__()
    monkeypatch.setattr(builtins, "__import__", _no_nacl)
    vault_module._VAULT.__init__()

    report = preflight.Report()
    preflight.check_vault(report)

    row = _row(report)
    assert row[0] == preflight.FAIL, row
    assert "PyNaCl" in row[2] or "PyNaCl" in row[3], row


def test_an_unwritable_key_file_is_a_failure(isolated_vault, monkeypatch):
    """A read-only data volume must be caught before the service starts writing plaintext."""
    monkeypatch.setenv("ZCJ_VAULT_KEY_FILE", "/proc/nope/.zcj_vault_key")
    vault_module._VAULT.__init__()

    report = preflight.Report()
    preflight.check_vault(report)

    row = _row(report)
    assert row[0] == preflight.FAIL, row


def test_an_explicit_disable_is_only_a_warning(isolated_vault, monkeypatch):
    """ZCJ_VAULT_DISABLED=1 is a deliberate operator choice, not a broken deployment."""
    monkeypatch.setenv("ZCJ_VAULT_DISABLED", "1")
    vault_module._VAULT.__init__()

    report = preflight.Report()
    preflight.check_vault(report)

    row = _row(report)
    assert row[0] == preflight.WARN, row


def test_the_check_is_part_of_the_report(isolated_vault):
    """A check nobody runs reports nothing."""
    report = preflight.run_checks()

    assert any(row[1] == "凭据加密" for row in report.rows), report.rows
