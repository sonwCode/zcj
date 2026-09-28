r"""A stored overview must not be able to break account reads.

``overview`` is a JSON blob on ``AccountOverviewModel``. Two read paths coerced its
``trial_end_time`` with a bare ``int(...)``:

* ``core/platform_accounts.build_platform_account``, and
* ``infrastructure/accounts_repository._record_from_model``.

Both sit next to a guarded conversion of the very same data - the former wraps
``AccountStatus(...)`` in ``try/except ValueError`` on the line above - so the guarding
idiom was established and simply missed. A stored value of ``"2025-12-01"`` (an older
version, a hand-edited row, or a legacy import) therefore raises ``ValueError`` while
building a platform account, which is on the account-listing path.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core.account_graph import load_account_graphs
from core.db import AccountModel, AccountOverviewModel, engine
from core.platform_accounts import build_platform_account


def _seed(text_trial_end_time):
    """Insert an account whose stored overview holds ``text_trial_end_time``."""
    with Session(engine) as session:
        model = AccountModel(
            platform="chatgpt",
            email="stored-overview@example.com",
            password="secret",
        )
        session.add(model)
        session.commit()
        session.refresh(model)
        overview = AccountOverviewModel(account_id=int(model.id or 0))
        overview.set_summary(
            {
                "platform": "chatgpt",
                "lifecycle_status": "registered",
                "validity_status": "unknown",
                "trial_end_time": text_trial_end_time,
            }
        )
        session.add(overview)
        session.commit()
        session.refresh(model)
        return int(model.id or 0)


def test_a_text_trial_end_time_does_not_break_build_platform_account():
    """The regression: this raised ValueError out of the account read."""
    account_id = _seed("2025-12-01")

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        account = build_platform_account(session, model)

    assert account.trial_end_time == 0


def test_a_numeric_trial_end_time_is_preserved():
    account_id = _seed(1767225600)

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        account = build_platform_account(session, model)

    assert account.trial_end_time == 1767225600


@pytest.mark.parametrize(
    "stored",
    [
        "abc",
        "12.5",
        "2025-12-01",
        "",
        None,
        # json.loads accepts these by default, so a stored overview can genuinely
        # carry them. int(inf) raises OverflowError, which a (TypeError, ValueError)
        # catch would miss.
        float("inf"),
        float("-inf"),
        float("nan"),
    ],
)
def test_more_malformed_shapes_do_not_break_the_read(stored):
    account_id = _seed(stored)

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        account = build_platform_account(session, model)

    assert account.trial_end_time == 0


def test_load_account_graphs_still_exposes_the_raw_overview():
    """The fix must not rewrite stored data - only the read coercion changes."""
    account_id = _seed("2025-12-01")

    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]

    assert graph["overview"]["trial_end_time"] == "2025-12-01"


def test_to_record_coerces_a_text_trial_end_time():
    """The second read path: the row-to-record mapper used by the listing."""
    from infrastructure.accounts_repository import _to_record

    account_id = _seed("2025-12-01")
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        graph = load_account_graphs(session, [account_id])[account_id]
        record = _to_record(model, graph)

    assert record.trial_end_time == 0


def test_one_bad_row_does_not_break_the_account_listing():
    """The user-visible severity: a listing must survive one malformed row."""
    from domain.accounts import AccountQuery
    from infrastructure.accounts_repository import AccountsRepository

    _seed("2025-12-01")
    with Session(engine) as session:
        good = AccountModel(
            platform="chatgpt",
            email="good-row@example.com",
            password="secret",
        )
        session.add(good)
        session.commit()

    total, records = AccountsRepository().list(AccountQuery(page=1, page_size=50))

    assert total == 2
    assert len(records) == 2
    assert {record.trial_end_time for record in records} == {0}


@pytest.mark.parametrize("stored", [True, False])
def test_a_boolean_is_not_treated_as_an_epoch(stored):
    """bool is a subclass of int; a stored ``true`` is not a timestamp.

    Without the explicit bool check, ``int(True)`` would silently become an epoch of 1
    (1970-01-01) instead of "unset".
    """
    account_id = _seed(stored)

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        account = build_platform_account(session, model)

    assert account.trial_end_time == 0


def test_the_startup_resync_survives_a_stored_text_value():
    """The write path: sync_account_graph feeds the stored overview back through the
    normaliser, so a legacy text value used to abort the re-sync before the read even
    happened."""
    from core.account_graph import sync_account_graph

    account_id = _seed("2025-12-01")

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        sync_account_graph(session, model)
        session.commit()
        graph = load_account_graphs(session, [account_id])[account_id]

    assert graph["overview"]["trial_end_time"] == 0


# --- The same stored value, read by the periodic sweeps -----------------------


def _seed_trial(stored, *, email, lifecycle="trial"):
    """Insert a trial account whose stored overview holds ``stored``."""
    with Session(engine) as session:
        model = AccountModel(platform="chatgpt", email=email, password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        overview = AccountOverviewModel(
            account_id=int(model.id or 0),
            lifecycle_status=lifecycle,
        )
        overview.set_summary(
            {
                "platform": "chatgpt",
                "lifecycle_status": lifecycle,
                "validity_status": "unknown",
                "trial_end_time": stored,
            }
        )
        session.add(overview)
        session.commit()
        return int(model.id or 0)


def test_the_scheduler_trial_sweep_survives_a_malformed_row():
    """``Scheduler.check_trial_expiry`` walks every account; one bad stored value used to
    abort the sweep, so no later trial account got expired."""
    from core.scheduler import Scheduler

    _seed_trial("2025-12-01", email="trial-bad@example.com")
    _seed_trial(1, email="trial-expired@example.com")

    Scheduler().check_trial_expiry()

    with Session(engine) as session:
        expired = session.exec(
            select(AccountOverviewModel).where(
                AccountOverviewModel.lifecycle_status == "expired"
            )
        ).all()

    # The healthy row must still have been processed despite the malformed one.
    assert len(expired) == 1


def test_flag_expiring_trials_survives_a_malformed_row():
    """``flag_expiring_trials`` walks every trial overview in one pass."""
    from core.lifecycle import flag_expiring_trials

    _seed_trial("not-a-timestamp", email="trial-flag-bad@example.com")

    results = flag_expiring_trials()

    assert results["skipped"] >= 1
