r"""A stored ``sub2api_sync`` state must not be able to break the sync sweeps.

``remote_account_id`` lives in ``overview.legacy_extra.sub2api_sync`` - the stored
overview blob - and it is *caller supplied*: ``save_account`` copies an
``account_overview`` payload straight into that blob (asserted below), so the value is
not guaranteed to be an int.

``core/sub2api_sync.py`` already has ``_as_positive_int`` for exactly this field and uses
it at two sites, but three others read the same key with a bare ``int()`` - including
``cleanup_invalid_synced_accounts``, which walks every account in one pass, so one bad row
used to abort the whole cleanup.
"""
from __future__ import annotations

from sqlmodel import Session, select

from core.account_graph import load_account_graphs
from core.base_platform import Account
from core.db import AccountModel, engine, save_account
from core.sub2api_sync import _as_positive_int


def _seed_account(email, *, state):
    save_account(
        Account(
            platform="chatgpt",
            email=email,
            password="secret",
            extra={"account_overview": {"legacy_extra": {"sub2api_sync": state}}},
        )
    )
    with Session(engine) as session:
        model = session.exec(select(AccountModel).where(AccountModel.email == email)).one()
        return int(model.id or 0)


def _enable_auto_delete(monkeypatch):
    from core.config_store import config_store

    monkeypatch.setattr(
        config_store,
        "get",
        lambda key, default="": {
            "sub2api_url": "https://sub2api.example.com",
            "sub2api_auto_delete_invalid": "true",
        }.get(key, default),
    )


def test_a_caller_supplied_state_is_what_gets_read_back():
    """The reachability premise: this value really is caller supplied."""
    account_id = _seed_account(
        "sub2api-supplied@example.com",
        state={"remote_account_id": "abc", "proxy_id": 7},
    )

    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]

    state = graph["overview"]["legacy_extra"]["sub2api_sync"]
    assert state["remote_account_id"] == "abc"


def test_as_positive_int_is_total():
    """The in-file helper must survive the shapes the blob can hold.

    ``int(float("inf"))`` is what raises ``OverflowError``; a Python ``int`` never
    overflows, so an oversized integer is returned as-is rather than zeroed - that is
    the function contract, and it is not a crash path.
    """
    assert _as_positive_int("abc") == 0
    assert _as_positive_int(None) == 0
    assert _as_positive_int(float("inf")) == 0
    assert _as_positive_int(float("-inf")) == 0
    assert _as_positive_int(float("nan")) == 0
    assert _as_positive_int(0) == 0
    assert _as_positive_int(-5) == 0
    # bool is a subclass of int: a stored ``true`` must not read as account #1.
    assert _as_positive_int(True) == 0
    assert _as_positive_int(False) == 0
    assert _as_positive_int("12") == 12
    assert _as_positive_int(12) == 12
    assert _as_positive_int(10 ** 400) == 10 ** 400


def test_cleanup_survives_a_malformed_stored_state(monkeypatch):
    """The sweep: one unparseable row must not abort the pass for the others."""
    from core.sub2api_sync import cleanup_invalid_synced_accounts

    _enable_auto_delete(monkeypatch)
    _seed_account("sub2api-bad@example.com", state={"remote_account_id": "abc"})
    _seed_account("sub2api-good@example.com", state={"remote_account_id": 0})

    results = cleanup_invalid_synced_accounts()

    # Both rows are inspected, and neither can be a deletion target, so both are skipped.
    assert results["skipped"] == 2


def test_delete_synced_account_tolerates_a_malformed_state(monkeypatch):
    from core.sub2api_sync import delete_synced_account

    _enable_auto_delete(monkeypatch)
    account_id = _seed_account("sub2api-del@example.com", state={"remote_account_id": "abc"})

    assert delete_synced_account(account_id) is False
