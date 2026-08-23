from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select

from core.account_graph import load_account_graphs, sync_account_graph
from core.base_platform import Account
from core.db import (
    AccountCredentialModel,
    AccountModel,
    AccountOverviewModel,
    engine,
    save_account,
)


def test_web_access_token_is_stored_as_a_platform_credential():
    save_account(
        Account(
            platform="chatgpt",
            email="web-token@example.com",
            password="secret",
            token="codex-access",
            extra={
                "access_token": "codex-access",
                "web_access_token": "chatgpt-web-access",
                "custom_marker": "keep-me",
            },
        )
    )

    with Session(engine) as session:
        stored = session.exec(
            select(AccountModel).where(AccountModel.email == "web-token@example.com")
        ).one()
        account_id = int(stored.id or 0)
        graph = load_account_graphs(session, [account_id])[account_id]

    credentials = {item["key"]: item["value"] for item in graph["credentials"]}
    legacy = dict((graph.get("overview") or {}).get("legacy_extra") or {})
    assert credentials["access_token"] == "codex-access"
    assert credentials["web_access_token"] == "chatgpt-web-access"
    assert "web_access_token" not in legacy
    assert legacy["custom_marker"] == "keep-me"


def test_startup_graph_sync_promotes_legacy_web_access_token():
    with Session(engine) as session:
        model = AccountModel(
            platform="chatgpt",
            email="legacy-web-token@example.com",
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
                "legacy_extra": {
                    "web_access_token": "legacy-web-access",
                    "custom_marker": "keep-me",
                },
            }
        )
        session.add(overview)
        session.commit()

        sync_account_graph(session, model)
        session.commit()

        credential = session.exec(
            select(AccountCredentialModel)
            .where(AccountCredentialModel.account_id == int(model.id or 0))
            .where(AccountCredentialModel.key == "web_access_token")
        ).one()
        refreshed_overview = session.get(AccountOverviewModel, int(model.id or 0))

    assert credential.value == "legacy-web-access"
    legacy = dict(refreshed_overview.get_summary().get("legacy_extra") or {})
    assert "web_access_token" not in legacy
    assert legacy["custom_marker"] == "keep-me"


def test_concurrent_saves_of_same_identity_create_one_account_row():
    def _save(index: int) -> int:
        model = save_account(
            Account(
                platform="chatgpt",
                email="same-identity@example.com",
                password=f"secret-{index}",
                extra={"access_token": f"token-{index}"},
            )
        )
        return int(model.id or 0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        account_ids = list(pool.map(_save, range(16)))

    assert len(set(account_ids)) == 1
    with Session(engine) as session:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(AccountModel.email == "same-identity@example.com")
        ).all()
    assert len(rows) == 1
