from __future__ import annotations

from unittest.mock import MagicMock

from sqlmodel import Session, select

from core.account_graph import load_account_graphs, patch_account_graph
from core.base_platform import Account
from core.db import AccountModel, engine, save_account
from core.sub2api_sync import Sub2ApiClient
from core.sub2api_sync import _filter_models_for_group
from core.sub2api_sync import backfill_unsynced_accounts
from core.sub2api_sync import cleanup_invalid_synced_accounts
from core.sub2api_sync import delete_synced_account
from core.sub2api_sync import push_account_to_sub2api
from core.sub2api_sync import reconcile_sub2_remote_statuses
from core.sub2api_sync import repair_misclassified_registry_ineligible_accounts


def _config(*, auto_sync: bool = True, auto_delete: bool = True) -> dict:
    return {
        "url": "http://sub2api.test:8080",
        "email": "admin@sub2api.local",
        "password": "secret",
        "auto_sync": auto_sync,
        "auto_delete": auto_delete,
        "agent_region": "US",
        "proxy_id": 1,
        "group_id": 2,
        "group_name": "free",
        "model": "gpt-5.4",
    }


def _certificate() -> dict:
    return {
        "agent_runtime_id": "agent-test",
        "private_key_seed": "enp6enp6enp6enp6enp6enp6enp6enp6enp6enp6eno=",
        "task_id": "task-test",
        "account_id": "account-test",
        "chatgpt_user_id": "user-test",
        "email": "identity@test.com",
        "plan_type": "free",
        "chatgpt_account_is_fedramp": False,
    }


def _account(email: str) -> Account:
    return Account(
        platform="chatgpt",
        email=email,
        password="secret",
        token="access-token",
        extra={
            "access_token": "access-token",
            "id_token": "id-token",
            "refresh_token": "refresh-token",
            "oauth_credential_type": "codex_oauth",
        },
    )


def _model_id(email: str) -> int:
    with Session(engine) as session:
        model = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(AccountModel.email == email)
        ).one()
        return int(model.id or 0)


def _legacy_extra(account_id: int) -> dict:
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
        return dict((graph.get("overview") or {}).get("legacy_extra") or {})


def test_free_group_filters_sol_aliases_but_keeps_new_models():
    safe, dropped = _filter_models_for_group(
        ["gpt-5.6", "gpt-5.6-sol", "gpt-5.4", "gpt-6-next-preview"],
        "free",
    )
    assert safe == ["gpt-5.4", "gpt-6-next-preview"]
    assert dropped == ["gpt-5.6", "gpt-5.6-sol"]


def test_non_free_group_keeps_configured_sol_model():
    safe, dropped = _filter_models_for_group(["gpt-5.6", "gpt-5.6-sol"], "plus")
    assert safe == ["gpt-5.6", "gpt-5.6-sol"]
    assert dropped == []


def test_client_imports_exact_agent_identity_auth_json(monkeypatch):
    client = Sub2ApiClient("http://sub2api.test:8080", "admin@test", "secret")
    login = MagicMock(status_code=200)
    login.json.return_value = {
        "code": 0,
        "data": {"access_token": "admin-token"},
    }
    imported = MagicMock(status_code=200)
    imported.json.return_value = {
        "code": 0,
        "data": {
            "created": 1,
            "updated": 0,
            "failed": 0,
            "items": [{"account_id": 42, "action": "created"}],
        },
    }
    monkeypatch.setattr(client._session, "post", MagicMock(return_value=login))
    request = MagicMock(return_value=imported)
    monkeypatch.setattr(client._session, "request", request)
    auth_json = {
        "auth_mode": "agentIdentity",
        "agent_identity": {
            "agent_runtime_id": "agent-test",
            "agent_private_key": "private-test",
            "task_id": "task-test",
            "account_id": "account-test",
            "chatgpt_user_id": "user-test",
            "email": "free@test.com",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
        },
    }

    assert client.import_agent_identity(
        auth_json,
        name="free@test.com",
        proxy_id=7,
        models=["gpt-5.3-codex-spark", "gpt-5.4"],
    ) == 42
    body = request.call_args.kwargs["json"]
    assert body["content"]
    assert "access_token" not in body["content"]
    assert "refresh_token" not in body["content"]
    assert '"auth_mode":"agentIdentity"' in body["content"]
    assert body["proxy_id"] == 7
    assert body["credential_extras"] == {
        "model_mapping": {
            "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
            "gpt-5.4": "gpt-5.4",
        }
    }


def test_successful_registration_sync_persists_remote_id(monkeypatch):
    email = "sub2api-sync@test.com"
    account = _account(email)
    account.extra["auth_proxy_url"] = (
        "http://USER-zone-custom-region-CO-session-old-sessTime-180:secret"
        "@global.rotgb.711proxy.com:10000"
    )
    save_account(account)
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def import_codex_session(
            self,
            auth_json,
            *,
            name,
            proxy_id=0,
            group_ids=None,
            models=None,
            concurrency=10,
        ):
            captured["auth_json"] = auth_json
            captured["name"] = name
            captured["proxy_id"] = proxy_id
            captured["group_ids"] = group_ids
            captured["models"] = models
            captured["concurrency"] = concurrency
            return 73

        def test_account(self, remote_id, *, model=""):
            assert remote_id == 73
            captured["test_model"] = model
            return {"success": True, "error": ""}

        def get_account(self, remote_id):
            assert remote_id == 73
            return {"id": remote_id, "status": "active", "schedulable": True}

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)
    monkeypatch.setattr("platforms.chatgpt.agent_identity.has_identity_claims", lambda _token: True)
    def fake_register(*args, **kwargs):
        captured["register_kwargs"] = kwargs
        return _certificate()

    monkeypatch.setattr("platforms.chatgpt.agent_identity.register_identity", fake_register)
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.certificate_to_sub2api_export",
        lambda _certificate: {
            "agent_identity": {
                "agent_runtime_id": "agent-test",
                "agent_private_key": "private-test",
                "task_id": "task-test",
                "account_id": "account-test",
                "chatgpt_user_id": "user-test",
                "email": email,
                "plan_type": "free",
                "chatgpt_account_is_fedramp": False,
            }
        },
    )

    assert push_account_to_sub2api(
        account,
        sync_options={
            "sub2api_proxy_id": 9,
            "sub2api_proxy_region": "CO",
            "sub2api_model": "gpt-5.3-codex-spark",
        },
    ) is True
    assert captured["auth_json"]["auth_mode"] == "agentIdentity"
    assert set(captured["auth_json"]) == {"auth_mode", "agent_identity"}
    assert captured["auth_json"]["agent_identity"]["plan_type"] == "free"
    assert captured["register_kwargs"]["proxy_region"] == "CO"
    assert captured["proxy_id"] == 9
    assert captured["models"] == ["gpt-5.3-codex-spark"]
    legacy = _legacy_extra(_model_id(email))
    assert legacy["sub2api_sync"]["remote_account_id"] == 73
    assert legacy["sub2api_sync"]["proxy_id"] == 9
    assert legacy["sub2api_sync"]["proxy_region"] == "CO"
    assert legacy["sub2api_sync"]["models"] == ["gpt-5.3-codex-spark"]
    assert legacy["sub2api_sync"]["auth_mode"] == "agent_identity"
    assert legacy["sub2api_sync"]["status"] == "imported_active"
    assert legacy["sub2api_sync"]["remote_schedulable"] is True
    assert captured["test_model"] == "gpt-5.3-codex-spark"
    assert legacy["agent_identity_certificate"]["agent_runtime_id"] == "agent-test"


def test_registry_disabled_marks_account_ineligible_without_import(monkeypatch):
    from platforms.chatgpt.agent_identity import AgentIdentityError

    email = "sub2api-registry-disabled@test.com"
    account = _account(email)
    save_account(account)
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_created"] = True

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)
    monkeypatch.setattr("platforms.chatgpt.agent_identity.has_identity_claims", lambda _token: True)
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.register_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AgentIdentityError(
                "HTTP 403 code=agent_registry_not_enabled Agent registry is not enabled."
            )
        ),
    )

    assert push_account_to_sub2api(account) is False
    assert "client_created" not in captured
    state = _legacy_extra(_model_id(email))["sub2api_sync"]
    assert state.get("remote_account_id", 0) == 0
    assert state["status"] == "registry_pending"
    assert state["agent_registry_status"] == "pending"
    assert state["registry_retry_count"] == 1
    assert state["next_retry_at"]
    assert "agent_registry_not_enabled" in state["last_error"]
    assert "agent_identity_certificate" not in _legacy_extra(_model_id(email))
    assert account.extra["sub2api_sync"]["status"] == "registry_pending"
    assert "agent_registry_not_enabled" in account.extra["sub2api_sync"]["last_error"]
    with Session(engine) as session:
        graph = load_account_graphs(session, [_model_id(email)])[_model_id(email)]
    assert graph["validity_status"] == "unknown"


def test_web_session_without_refresh_can_create_agent_identity_but_never_bearer_fallback(monkeypatch):
    email = "sub2api-web-token@test.com"
    account = _account(email)
    account.extra.pop("refresh_token", None)
    account.extra["oauth_credential_type"] = "chatgpt_web"
    save_account(account)
    captured = {}

    def fake_register_identity(*args, **kwargs):
        captured["register_identity"] = True
        return _certificate()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def import_codex_session(self, auth_json, **kwargs):
            captured["auth_json"] = auth_json
            return 99

        def test_account(self, remote_id, *, model=""):
            return {"success": True, "error": ""}

        def get_account(self, remote_id):
            return {"id": remote_id, "status": "active", "schedulable": True}

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)
    monkeypatch.setattr("platforms.chatgpt.agent_identity.has_identity_claims", lambda _token: True)
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.register_identity",
        fake_register_identity,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.certificate_to_sub2api_export",
        lambda _certificate: {
            "agent_identity": {
                "agent_runtime_id": "agent-test",
                "agent_private_key": "private-test",
                "task_id": "task-test",
                "account_id": "account-test",
                "chatgpt_user_id": "user-test",
                "email": email,
                "plan_type": "free",
                "chatgpt_account_is_fedramp": False,
            }
        },
    )

    assert push_account_to_sub2api(account) is True
    assert captured["register_identity"] is True
    assert captured["auth_json"]["auth_mode"] == "agentIdentity"
    assert "access_token" not in captured["auth_json"]
    state = _legacy_extra(_model_id(email))["sub2api_sync"]
    assert state["status"] == "imported_active"
    assert state["remote_account_id"] == 99
    with Session(engine) as session:
        graph = load_account_graphs(session, [_model_id(email)])[_model_id(email)]
    assert graph["validity_status"] == "unknown"


def test_repair_restores_legacy_registry_ineligible_account_to_unknown():
    email = "sub2api-legacy-ineligible@test.com"
    save_account(_account(email))
    account_id = _model_id(email)
    with Session(engine) as session:
        stored = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            stored,
            summary_updates={
                "valid": False,
                "validity_status": "invalid",
                "validity_reason": "sub2_ineligible: Agent Registry is not enabled",
                "legacy_extra": {
                    "sub2api_sync": {
                        "remote_account_id": 0,
                        "status": "invalid",
                        "last_error": "sub2_ineligible: Agent Registry is not enabled",
                    }
                },
            },
        )
        session.commit()

    assert repair_misclassified_registry_ineligible_accounts() == 1
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
    assert graph["validity_status"] == "unknown"
    state = _legacy_extra(account_id)["sub2api_sync"]
    assert state["status"] == "registry_pending"
    assert state["agent_registry_status"] == "pending"


def test_invalid_synced_account_is_deleted_once(monkeypatch):
    email = "sub2api-delete@test.com"
    account = _account(email)
    save_account(account)
    account_id = _model_id(email)
    with Session(engine) as session:
        stored = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            stored,
            summary_updates={
                "validity_status": "invalid",
                "legacy_extra": {
                    "sub2api_sync": {
                        "remote_account_id": 91,
                        "status": "active",
                    }
                },
            },
        )
        session.commit()

    deleted = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def delete_account(self, remote_id):
            deleted.append(remote_id)

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)

    result = cleanup_invalid_synced_accounts(limit=100)

    assert result["deleted"] == 1
    assert deleted == [91]
    state = _legacy_extra(account_id)["sub2api_sync"]
    assert state["remote_account_id"] == 0
    assert state["deleted_remote_account_id"] == 91
    assert delete_synced_account(account_id) is False


def test_unknown_synced_account_is_not_deleted(monkeypatch):
    email = "sub2api-unknown@test.com"
    save_account(_account(email))
    account_id = _model_id(email)
    with Session(engine) as session:
        stored = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            stored,
            summary_updates={
                "validity_status": "unknown",
                "legacy_extra": {
                    "sub2api_sync": {
                        "remote_account_id": 92,
                        "status": "active",
                    }
                },
            },
        )
        session.commit()

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    result = cleanup_invalid_synced_accounts(limit=100)

    assert result["deleted"] == 0
    assert _legacy_extra(account_id)["sub2api_sync"]["remote_account_id"] == 92


def test_invalidated_agent_token_marks_local_account_invalid(monkeypatch):
    from platforms.chatgpt.agent_identity import AgentIdentityError

    email = "sub2api-token-invalidated@test.com"
    account = _account(email)
    account.extra["auth_proxy_url"] = (
        "http://USER-zone-custom-region-CO:secret@global.rotgb.711proxy.com:10000"
    )
    save_account(account)
    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("platforms.chatgpt.agent_identity.has_identity_claims", lambda _token: True)
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.register_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AgentIdentityError("HTTP 401 code=token_invalidated")
        ),
    )

    assert push_account_to_sub2api(account) is False
    account_id = _model_id(email)
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
    assert graph["validity_status"] == "unknown"
    assert _legacy_extra(account_id)["sub2api_sync"]["status"] == "invalid"


def test_remote_401_marks_local_account_invalid(monkeypatch):
    email = "sub2api-remote-401@test.com"
    save_account(_account(email))
    account_id = _model_id(email)
    with Session(engine) as session:
        stored = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            stored,
            summary_updates={
                "validity_status": "unknown",
                "legacy_extra": {
                    "sub2api_sync": {
                        "remote_account_id": 172,
                        "status": "active",
                    }
                },
            },
        )
        session.commit()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_account(self, remote_id):
            assert remote_id == 172
            return {
                "id": 172,
                "status": "error",
                "schedulable": False,
                "error_message": 'Authentication failed (401): {"detail":"Unauthorized"}',
            }

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)

    result = reconcile_sub2_remote_statuses(limit=100)

    assert result["invalid"] == 1
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
    assert graph["validity_status"] == "unknown"
    state = _legacy_extra(account_id)["sub2api_sync"]
    assert state["status"] == "invalid"
    assert state["remote_status"] == "error"


def test_remote_rate_limit_does_not_mark_local_account_invalid(monkeypatch):
    email = "sub2api-remote-429@test.com"
    save_account(_account(email))
    account_id = _model_id(email)
    with Session(engine) as session:
        stored = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            stored,
            summary_updates={
                "validity_status": "valid",
                "legacy_extra": {
                    "sub2api_sync": {
                        "remote_account_id": 173,
                        "status": "active",
                    }
                },
            },
        )
        session.commit()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_account(self, remote_id):
            assert remote_id == 173
            return {
                "id": 173,
                "status": "error",
                "schedulable": False,
                "error_message": "Rate limited (429), retry later",
            }

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)

    result = reconcile_sub2_remote_statuses(limit=100)

    assert result["invalid"] == 0
    assert result["healthy"] == 1
    assert result["cooling"] == 1
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
    assert graph["validity_status"] == "valid"
    assert _legacy_extra(account_id)["sub2api_sync"]["status"] == "imported_cooling"


def test_legacy_bearer_fallback_is_invalid_even_before_remote_401(monkeypatch):
    email = "sub2api-legacy-bearer@test.com"
    save_account(_account(email))
    account_id = _model_id(email)
    with Session(engine) as session:
        stored = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            stored,
            summary_updates={
                "validity_status": "valid",
                "legacy_extra": {
                    "sub2api_sync": {
                        "remote_account_id": 180,
                        "status": "active",
                        "auth_mode": "bearer_fallback",
                    }
                },
            },
        )
        session.commit()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_account(self, remote_id):
            assert remote_id == 180
            return {"id": 180, "status": "active", "schedulable": True}

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)

    result = reconcile_sub2_remote_statuses(limit=100)

    assert result["invalid"] == 1
    with Session(engine) as session:
        graph = load_account_graphs(session, [account_id])[account_id]
    assert graph["validity_status"] == "valid"
    assert _legacy_extra(account_id)["sub2api_sync"]["status"] == "invalid"


def test_client_parses_successful_account_test_sse(monkeypatch):
    client = Sub2ApiClient("http://sub2api.test:8080", "admin@test", "secret")
    client._access_token = "admin-token"
    response = MagicMock(status_code=200)
    response.text = (
        'data: {"type":"test_start","model":"gpt-5.4"}\n\n'
        'data: {"type":"test_complete","success":true}\n\n'
    )
    monkeypatch.setattr(client._session, "post", MagicMock(return_value=response))

    assert client.test_account(42, model="gpt-5.4") == {"success": True, "error": ""}


def test_registry_pending_becomes_ineligible_after_retry_window(monkeypatch):
    from platforms.chatgpt.agent_identity import AgentIdentityError

    email = "sub2api-registry-final@test.com"
    account = _account(email)
    save_account(account)
    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("platforms.chatgpt.agent_identity.has_identity_claims", lambda _token: True)
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.register_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AgentIdentityError("HTTP 403 code=agent_registry_not_enabled")
        ),
    )

    for _ in range(5):
        assert push_account_to_sub2api(account) is False

    state = _legacy_extra(_model_id(email))["sub2api_sync"]
    assert state["status"] == "registry_ineligible"
    assert state["registry_retry_count"] == 5
    assert state["next_retry_at"] == ""


def test_imported_remote_401_is_deleted_and_never_marked_active(monkeypatch):
    email = "sub2api-import-401@test.com"
    account = _account(email)
    account.extra["agent_identity_certificate"] = _certificate()
    save_account(account)
    deleted = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def import_codex_session(self, auth_json, **kwargs):
            return 181

        def test_account(self, remote_id, *, model=""):
            return {"success": False, "error": 'API returned 401: {"detail":"Unauthorized"}'}

        def get_account(self, remote_id):
            return {
                "id": remote_id,
                "status": "error",
                "schedulable": False,
                "error_message": 'Authentication failed (401): {"detail":"Unauthorized"}',
            }

        def delete_account(self, remote_id):
            deleted.append(remote_id)

        def close(self):
            pass

    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr("core.sub2api_sync.Sub2ApiClient", FakeClient)
    monkeypatch.setattr("platforms.chatgpt.agent_identity.has_identity_claims", lambda _token: True)
    monkeypatch.setattr(
        "platforms.chatgpt.agent_identity.certificate_to_sub2api_export",
        lambda _certificate: {
            "agent_identity": {
                "agent_runtime_id": "agent-test",
                "agent_private_key": "private-test",
                "task_id": "task-test",
                "account_id": "account-test",
                "chatgpt_user_id": "user-test",
                "email": email,
                "plan_type": "free",
                "chatgpt_account_is_fedramp": False,
            }
        },
    )

    assert push_account_to_sub2api(account) is False
    assert deleted == [181]
    state = _legacy_extra(_model_id(email))["sub2api_sync"]
    assert state["status"] == "invalid"
    assert state["remote_account_id"] == 0
    assert state["deleted_remote_account_id"] == 181


def test_backfill_skips_account_before_next_retry(monkeypatch):
    email = "sub2api-backfill-wait@test.com"
    save_account(_account(email))
    account_id = _model_id(email)
    with Session(engine) as session:
        stored = session.get(AccountModel, account_id)
        patch_account_graph(
            session,
            stored,
            summary_updates={
                "legacy_extra": {
                    "sub2api_sync": {
                        "status": "registry_pending",
                        "next_retry_at": "2099-01-01T00:00:00Z",
                    }
                }
            },
        )
        session.commit()

    attempted = []
    monkeypatch.setattr("core.sub2api_sync._get_config", _config)
    monkeypatch.setattr(
        "core.sub2api_sync.push_account_to_sub2api",
        lambda account, **kwargs: attempted.append(account.email) or True,
    )

    result = backfill_unsynced_accounts(limit=100)
    assert result["skipped"] >= 1
    assert email not in attempted
