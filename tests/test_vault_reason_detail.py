"""The vault keeps the specific reason a key file could not be provisioned.

``_load()`` used to replace whatever ``_read_or_create_key_file()`` had recorded
(for example ``key file error: PermissionError``) with the generic
``no usable vault key``.  Both are a FAIL for the preflight, but the reason is what
sends the operator to the right fix, and it is also surfaced by the stats API.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import vault as V


def _fresh(directory: Path) -> None:
    os.environ["ACCOUNT_MANAGER_DATABASE_URL"] = "sqlite:///" + str(directory / "account_manager.db")
    for name in ("ZCJ_VAULT_KEY", "ZCJ_VAULT_KEY_FILE", "ZCJ_VAULT_DISABLED"):
        os.environ.pop(name, None)


def test_an_uncreatable_key_file_keeps_the_specific_reason(tmp_path, monkeypatch):
    """A permission failure must not be reported as a missing key."""
    unwritable = tmp_path / "data"
    unwritable.mkdir()
    _fresh(unwritable)
    monkeypatch.setattr(V, "_database_dir", lambda: unwritable)
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))

    status = V._Vault().status()

    assert status["enabled"] is False
    assert "key file error" in status["reason"]
    assert "PermissionError" in status["reason"]


def test_a_missing_key_is_still_reported_generically(tmp_path, monkeypatch):
    """When nothing more specific is known the generic reason is correct."""
    data = tmp_path / "data"
    data.mkdir()
    _fresh(data)
    monkeypatch.setattr(V, "_database_dir", lambda: data)
    monkeypatch.setattr(V, "_decode_key", lambda raw: None)
    (data / ".zcj_vault_key").write_text("garbage", encoding="utf-8")

    status = V._Vault().status()

    assert status["enabled"] is False
    assert status["reason"] == "no usable vault key"
