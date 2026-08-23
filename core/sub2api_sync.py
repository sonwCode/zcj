"""Synchronize ChatGPT Agent Identity accounts with a Sub2API instance."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests


logger = logging.getLogger(__name__)


_REGISTRY_RETRY_DELAYS_SECONDS = (120, 600, 1800, 7200)
_CREDENTIAL_RETRY_DELAYS_SECONDS = (600, 1800, 7200, 21600)
_SYNC_RETRY_DELAYS_SECONDS = (120, 600, 1800, 7200, 21600)

# The Sub2API free group must not receive Sol. Sub2API has exposed Sol under
# both a descriptive id (for example gpt-5.6-sol) and the short alias
# gpt-5.6 at different times, so filtering only the literal -sol suffix
# is not sufficient. Keep this rule narrow: paid/non-free groups may
# still use their configured Sol model.
_FREE_GROUP_NAMES = {
    "free",
    "free_account",
    "free-account",
    "chatgpt free",
    "chatgpt_free",
    "chatgpt-free",
}
_FREE_SOL_MODEL_ALIASES = {
    "gpt-5-6",
    "gpt-5-6-sol",
    "gpt-5-6-sol-latest",
    "gpt-5-6-codex-sol",
}


class Sub2ApiSyncError(RuntimeError):
    pass


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: int) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=max(int(seconds), 0))
    return value.isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _retry_state(
    current_state: dict[str, Any],
    *,
    pending_status: str,
    counter_key: str,
    delays: tuple[int, ...],
    final_status: str = "",
) -> dict[str, Any]:
    attempt = max(int(current_state.get(counter_key) or 0), 0) + 1
    state = {
        **current_state,
        "status": pending_status,
        counter_key: attempt,
        "retry_count": attempt,
        "last_attempt_at": _utcnow_iso(),
    }
    if final_status and attempt > len(delays):
        state["status"] = final_status
        state["next_retry_at"] = ""
        return state
    delay = delays[min(attempt - 1, len(delays) - 1)]
    state["next_retry_at"] = _iso_after(delay)
    return state


def _as_bool(value: Any, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "enabled"}


def _as_positive_int(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _positive_int_list(value: Any) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value or "").replace("\n", ",").split(",")
    values: list[int] = []
    for item in raw_items:
        number = _as_positive_int(item)
        if number and number not in values:
            values.append(number)
    return values


def _model_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value or "").replace("\n", ",").split(",")
    models: list[str] = []
    for item in raw_items:
        model = str(item or "").strip()
        if model and model.lower() not in {"auto", "all", "*"} and model not in models:
            models.append(model)
    return models


def _normalized_model_id(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _is_free_group(group_name: Any) -> bool:
    normalized = _normalized_model_id(group_name).replace("-", "_")
    return normalized in {name.replace("-", "_") for name in _FREE_GROUP_NAMES}


def _is_sol_model(model: Any) -> bool:
    normalized = _normalized_model_id(model)
    if not normalized:
        return False
    if normalized in _FREE_SOL_MODEL_ALIASES:
        return True
    # Match a standalone sol component without catching unrelated words
    # such as solver.
    return bool(re.search(r"(?:^|-)sol(?:-|$)", normalized))


def _filter_models_for_group(models: Any, group_name: Any) -> tuple[list[str], list[str]]:
    normalized_models = _model_list(models)
    if not _is_free_group(group_name):
        return normalized_models, []
    safe_models = [model for model in normalized_models if not _is_sol_model(model)]
    dropped_models = [model for model in normalized_models if _is_sol_model(model)]
    return safe_models, dropped_models


def _unwrap_response(response: requests.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise Sub2ApiSyncError(f"Sub2API returned invalid JSON (HTTP {response.status_code})") from exc
    if not isinstance(payload, dict):
        raise Sub2ApiSyncError("Sub2API returned an invalid response object")
    if response.status_code < 200 or response.status_code >= 300:
        message = str(payload.get("message") or payload.get("error") or response.text or "request failed")
        raise Sub2ApiSyncError(f"Sub2API HTTP {response.status_code}: {message[:300]}")
    if "code" in payload:
        if int(payload.get("code") or 0) != 0:
            raise Sub2ApiSyncError(str(payload.get("message") or "Sub2API request failed"))
        return payload.get("data")
    return payload


class Sub2ApiClient:
    def __init__(self, base_url: str, email: str, password: str, *, timeout: int = 30):
        root = str(base_url or "").strip().rstrip("/")
        if root.endswith("/api/v1"):
            root = root[:-7]
        self.base_url = root
        self.email = str(email or "").strip()
        self.password = str(password or "")
        self.timeout = max(int(timeout), 5)
        self._access_token = ""
        self._session = requests.Session()
        self._session.trust_env = False

    def close(self) -> None:
        self._session.close()

    def _login(self) -> None:
        response = self._session.post(
            f"{self.base_url}/api/v1/auth/login",
            json={"email": self.email, "password": self.password},
            timeout=self.timeout,
        )
        data = _unwrap_response(response)
        token = str((data or {}).get("access_token") or "") if isinstance(data, dict) else ""
        if not token:
            raise Sub2ApiSyncError("Sub2API login response did not include access_token")
        self._access_token = token

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> Any:
        if not self._access_token:
            self._login()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        response = self._session.request(
            method,
            f"{self.base_url}/api/v1{path}",
            headers=headers,
            json=body,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            self._access_token = ""
            self._login()
            headers["Authorization"] = f"Bearer {self._access_token}"
            response = self._session.request(
                method,
                f"{self.base_url}/api/v1{path}",
                headers=headers,
                json=body,
                timeout=self.timeout,
            )
        if method.upper() == "DELETE" and response.status_code == 404:
            return {"deleted": True, "already_missing": True}
        return _unwrap_response(response)

    def import_codex_session(
        self,
        session_json: dict[str, Any],
        *,
        name: str,
        proxy_id: int = 0,
        group_ids: Any = None,
        models: Any = None,
        concurrency: int = 10,
    ) -> int:
        body: dict[str, Any] = {
            "content": json.dumps(session_json, ensure_ascii=False, separators=(",", ":")),
            "name": str(name or "ChatGPT Free"),
            "concurrency": max(_as_positive_int(concurrency), 1),
            "priority": 50,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True,
            "update_existing": True,
        }
        normalized_proxy_id = _as_positive_int(proxy_id)
        if normalized_proxy_id:
            body["proxy_id"] = normalized_proxy_id
        normalized_group_ids = _positive_int_list(group_ids)
        if normalized_group_ids:
            body["group_ids"] = normalized_group_ids
        selected_models = _model_list(models)
        if selected_models:
            body["credential_extras"] = {
                "model_mapping": {model: model for model in selected_models},
            }
        data = self._request(
            "POST",
            "/admin/accounts/import/codex-session",
            body=body,
        )
        if not isinstance(data, dict):
            raise Sub2ApiSyncError("Sub2API import returned an invalid result")
        items = data.get("items") if isinstance(data.get("items"), list) else []
        account_id = next(
            (
                int(item.get("account_id") or 0)
                for item in items
                if isinstance(item, dict) and int(item.get("account_id") or 0) > 0
            ),
            0,
        )
        if account_id <= 0 or int(data.get("failed") or 0) > 0:
            errors = data.get("errors") if isinstance(data.get("errors"), list) else []
            message = str(errors[0].get("message") or "") if errors and isinstance(errors[0], dict) else ""
            raise Sub2ApiSyncError(message or "Sub2API did not create or update the account")
        return account_id

    def import_agent_identity(
        self,
        auth_json: dict[str, Any],
        *,
        name: str,
        proxy_id: int = 0,
        group_ids: Any = None,
        models: Any = None,
        concurrency: int = 10,
    ) -> int:
        return self.import_codex_session(
            auth_json,
            name=name,
            proxy_id=proxy_id,
            group_ids=group_ids,
            models=models,
            concurrency=concurrency,
        )

    def resolve_group_id(self, name: str, *, platform: str = "openai") -> int:
        target_name = str(name or "").strip().lower()
        target_platform = str(platform or "").strip().lower()
        if not target_name:
            return 0
        data = self._request("GET", "/admin/groups")
        items = data if isinstance(data, list) else []
        if isinstance(data, dict):
            items = data.get("items") if isinstance(data.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name") or "").strip().lower()
            item_platform = str(item.get("platform") or "").strip().lower()
            if item_name == target_name and (not target_platform or item_platform == target_platform):
                return _as_positive_int(item.get("id"))
        return 0

    def resolve_group_models(self, group_id: int) -> list[str]:
        normalized_group_id = _as_positive_int(group_id)
        if not normalized_group_id:
            return []
        models: list[str] = []
        group = self._request("GET", f"/admin/groups/{normalized_group_id}")
        if isinstance(group, dict):
            list_config = group.get("models_list_config")
            if isinstance(list_config, dict):
                models.extend(_model_list(list_config.get("models")))

        # Agent Identity accounts cannot use Sub2's upstream sync endpoint. Reuse
        # mappings already proven on another account in the same group instead.
        try:
            data = self._request("GET", "/admin/accounts?page=1&page_size=100")
            items = data if isinstance(data, list) else []
            if isinstance(data, dict):
                items = data.get("items") if isinstance(data.get("items"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_group_ids = _positive_int_list(item.get("group_ids"))
                if normalized_group_id not in item_group_ids:
                    continue
                account_id = _as_positive_int(item.get("id"))
                if not account_id:
                    continue
                detail = self._request("GET", f"/admin/accounts/{account_id}")
                credentials = detail.get("credentials") if isinstance(detail, dict) else None
                mapping = credentials.get("model_mapping") if isinstance(credentials, dict) else None
                if isinstance(mapping, dict):
                    models.extend(_model_list(list(mapping.keys())))
        except Sub2ApiSyncError:
            pass
        return _model_list(models)

    def delete_account(self, account_id: int) -> None:
        self._request("DELETE", f"/admin/accounts/{int(account_id)}")

    def get_account(self, account_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/admin/accounts/{int(account_id)}")
        if not isinstance(data, dict):
            raise Sub2ApiSyncError("Sub2API account detail returned an invalid result")
        return data

    def test_account(self, account_id: int, *, model: str = "") -> dict[str, Any]:
        if not self._access_token:
            self._login()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        body = {"model_id": str(model or ""), "prompt": "hi", "mode": "default"}

        def send() -> requests.Response:
            return self._session.post(
                f"{self.base_url}/api/v1/admin/accounts/{int(account_id)}/test",
                headers=headers,
                json=body,
                timeout=max(self.timeout, 90),
            )

        response = send()
        if response.status_code == 401:
            self._access_token = ""
            self._login()
            headers["Authorization"] = f"Bearer {self._access_token}"
            response = send()
        if response.status_code < 200 or response.status_code >= 300:
            return {
                "success": False,
                "error": f"Sub2API account test HTTP {response.status_code}: {response.text[:500]}",
            }

        success = False
        error = ""
        for line in response.text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "").strip().lower()
            if event_type == "test_complete" and bool(event.get("success")):
                success = True
            elif event_type == "error":
                error = str(event.get("error") or event.get("text") or "account test failed")[:500]
        if not success and not error:
            error = "Sub2API account test did not report success"
        return {"success": success, "error": error}


def _get_config() -> dict[str, Any]:
    from core.config_store import config_store

    return {
        "url": config_store.get("sub2api_url", ""),
        "email": config_store.get("sub2api_admin_email", ""),
        "password": config_store.get("sub2api_admin_password", ""),
        "auto_sync": _as_bool(config_store.get("sub2api_auto_sync", "false")),
        "auto_delete": _as_bool(config_store.get("sub2api_auto_delete_invalid", "false")),
        "agent_region": str(config_store.get("sub2api_agent_identity_region", "US") or "US").strip().upper(),
        "proxy_id": _as_positive_int(config_store.get("sub2api_proxy_id", "0")),
        "group_id": _as_positive_int(config_store.get("sub2api_group_id", "0")),
        "group_name": str(config_store.get("sub2api_group_name", "free") or "free").strip(),
        "model": str(config_store.get("sub2api_default_model", "") or "").strip(),
    }


def sub2api_auto_sync_enabled() -> bool:
    config = _get_config()
    return bool(config["auto_sync"] and config["url"])


def _sync_context(extra: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    overview = extra.get("account_overview") if isinstance(extra.get("account_overview"), dict) else {}
    legacy = overview.get("legacy_extra") if isinstance(overview.get("legacy_extra"), dict) else {}
    certificate = extra.get("agent_identity_certificate") or legacy.get("agent_identity_certificate")
    sync_state = extra.get("sub2api_sync") or legacy.get("sub2api_sync")
    return (
        dict(certificate) if isinstance(certificate, dict) else {},
        dict(sync_state) if isinstance(sync_state, dict) else {},
    )


def _local_account_id(platform: str, email: str) -> int:
    from sqlmodel import Session, select

    from core.db import AccountModel, engine

    with Session(engine) as session:
        model = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .where(AccountModel.email == email)
        ).first()
        return int(model.id or 0) if model else 0


def _persist_state(
    local_account_id: int,
    *,
    certificate: dict[str, Any] | None = None,
    sync_state: dict[str, Any] | None = None,
) -> None:
    if int(local_account_id or 0) <= 0:
        return
    from sqlmodel import Session

    from core.account_graph import load_account_graphs, patch_account_graph
    from core.db import AccountModel, engine

    with Session(engine) as session:
        model = session.get(AccountModel, int(local_account_id))
        if not model:
            return
        graph = load_account_graphs(session, [int(local_account_id)]).get(int(local_account_id), {})
        overview = dict(graph.get("overview") or {})
        legacy = dict(overview.get("legacy_extra") or {})
        if certificate is not None:
            legacy["agent_identity_certificate"] = dict(certificate)
        if sync_state is not None:
            legacy["sub2api_sync"] = dict(sync_state)
        patch_account_graph(session, model, summary_updates={"legacy_extra": legacy})
        session.add(model)
        session.commit()


def _is_terminal_identity_error(error: Exception) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "token_invalidated",
            "account_deactivated",
            "authentication token has been invalidated",
        )
    )


def _remote_terminal_reason(remote: dict[str, Any]) -> str:
    status = str(remote.get("status") or "").strip().lower()
    message = str(remote.get("error_message") or remote.get("error") or "").strip()
    normalized = message.lower()
    terminal_markers = (
        "authentication failed (401)",
        '"status":401',
        '"status": 401',
        "401 unauthorized",
        '"detail":"unauthorized"',
        '"detail": "unauthorized"',
        "token_invalidated",
        "authentication token has been invalidated",
        "account_deactivated",
        "account has been deactivated",
        "invalid authentication token",
        "access token expired",
    )
    if status == "error" and any(marker in normalized for marker in terminal_markers):
        return message or "Sub2API reported a terminal authentication error"
    if status in {"disabled", "inactive"} and any(
        marker in normalized
        for marker in (
            "token_invalidated",
            "authentication failed",
            "unauthorized",
            "expired",
            "deactivated",
        )
    ):
        return message or f"Sub2API account status is {status}"
    return ""


def _is_agent_registry_disabled(error: Exception) -> bool:
    text = str(error or "").lower()
    return (
        "agent_registry_not_enabled" in text
        or "agent registry is not enabled" in text
    )


def _is_credentials_pending_error(error: Exception) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "tokens do not include agent identity claims",
            "missing codex credential",
            "credentials_pending",
            "oauth credential is incomplete",
        )
    )


def _remote_cooling_reason(remote: dict[str, Any], test_error: str = "") -> str:
    message = str(remote.get("error_message") or remote.get("error") or test_error or "").strip()
    normalized = message.lower()
    if any(
        marker in normalized
        for marker in (
            "429",
            "rate limit",
            "rate_limit",
            "usage_limit_reached",
            "quota exceeded",
            "retry later",
        )
    ):
        return message or "Sub2API account is waiting for quota reset"

    now = datetime.now(timezone.utc)
    for key in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"):
        until = _parse_iso(remote.get(key))
        if until and until > now:
            return f"{key}={until.isoformat().replace('+00:00', 'Z')}"
    return ""


def _classify_remote_state(
    remote: dict[str, Any],
    test_result: dict[str, Any] | None = None,
) -> tuple[str, str]:
    test_error = str((test_result or {}).get("error") or "").strip()
    terminal_reason = _remote_terminal_reason(remote)
    if not terminal_reason and test_error:
        terminal_reason = _remote_terminal_reason(
            {"status": "error", "error_message": test_error}
        )
    if terminal_reason:
        return "invalid", terminal_reason

    cooling_reason = _remote_cooling_reason(remote, test_error)
    if cooling_reason:
        return "imported_cooling", cooling_reason

    status = str(remote.get("status") or "unknown").strip().lower()
    schedulable = remote.get("schedulable") is True
    if test_result is not None and not bool(test_result.get("success")):
        return "imported_unschedulable", test_error or "Sub2API account test failed"
    if status == "active" and schedulable:
        return "imported_active", ""
    return "imported_unschedulable", str(
        remote.get("error_message")
        or remote.get("error")
        or f"Sub2API account is status={status}, schedulable={schedulable}"
    )[:500]


def _remote_state_fields(remote: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "remote_status": str(remote.get("status") or "unknown").strip().lower(),
        "remote_schedulable": remote.get("schedulable") is True,
        "remote_error_message": str(
            remote.get("error_message") or remote.get("error") or ""
        )[:500],
        "remote_checked_at": _utcnow_iso(),
    }
    for key in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"):
        value = remote.get(key)
        if value:
            fields[f"remote_{key}"] = value
        else:
            fields.pop(f"remote_{key}", None)
    return fields


def _build_oauth_codex_session_json(account: Any, extra: dict[str, Any]) -> dict[str, Any] | None:
    """Build Sub2API codex-session import payload from OAuth tokens (post phone-bind).

    Requires both access_token and refresh_token. Web-session-only accounts must not
    be uploaded — they produce token_revoked / 401 on Codex API.
    """
    access_token = str(extra.get("access_token") or getattr(account, "token", "") or "").strip()
    refresh_token = str(extra.get("refresh_token") or "").strip()
    id_token = str(extra.get("id_token") or "").strip()
    # Reject obvious non-codex refresh tokens (empty / too short / placeholder)
    if not access_token or not refresh_token or len(refresh_token) < 20:
        return None
    # Prefer not to upload pure web access tokens that look like session cookies
    if access_token.count(".") < 2:
        return None
    email = str(getattr(account, "email", "") or extra.get("email") or "").strip()
    account_id = str(
        getattr(account, "user_id", "")
        or extra.get("chatgpt_account_id")
        or extra.get("account_id")
        or ""
    ).strip()
    payload: dict[str, Any] = {
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
        },
        "auth_mode": "oauth",
        "auth_provider": "openai",
    }
    if id_token:
        payload["id_token"] = id_token
        payload["tokens"]["id_token"] = id_token
    if account_id:
        payload["chatgpt_account_id"] = account_id
        payload["account_id"] = account_id
        payload["account"] = {"id": account_id}
    if email:
        payload["user"] = {"email": email}
    return payload


def push_account_to_sub2api(
    account: Any,
    *,
    log_fn: Callable[[str], None] | None = None,
    sync_options: dict[str, Any] | None = None,
    force_update: bool = False,
) -> bool:
    log = log_fn or logger.info
    config = _get_config()
    if not config["auto_sync"] or not config["url"]:
        return False
    if str(getattr(account, "platform", "") or "").lower() != "chatgpt":
        return False
    if not config["email"] or not config["password"]:
        log("  [Sub2API] Auto sync is enabled but admin credentials are incomplete")
        return False

    extra = dict(getattr(account, "extra", {}) or {})
    options = dict(sync_options or {})
    proxy_id = _as_positive_int(options.get("sub2api_proxy_id") or config.get("proxy_id"))
    proxy_region = str(
        options.get("sub2api_proxy_region")
        or options.get("sub2api_agent_identity_region")
        or config.get("agent_region")
        or "US"
    ).strip().upper()
    models = _model_list(options.get("sub2api_model") or options.get("sub2api_default_model") or config.get("model"))
    group_ids = _positive_int_list(options.get("sub2api_group_ids") or config.get("group_id"))
    group_name = str(options.get("sub2api_group_name") or config.get("group_name") or "free").strip()
    certificate, current_state = _sync_context(extra)
    if int(current_state.get("remote_account_id") or 0) > 0 and not force_update:
        return True
    local_account_id = _local_account_id(
        str(getattr(account, "platform", "") or ""),
        str(getattr(account, "email", "") or ""),
    )
    # Default: OAuth codex session after phone bind. Agent Identity is opt-in only.
    preferred_mode = str(
        options.get("sub2api_auth_mode")
        or extra.get("sub2api_auth_mode")
        or "oauth"
    ).strip().lower()
    allow_agent_identity = _as_bool(
        options.get("sub2api_allow_agent_identity")
        if "sub2api_allow_agent_identity" in options
        else extra.get("sub2api_allow_agent_identity"),
        preferred_mode in {"agent", "agent_identity", "auto"},
    )
    try:
        access_token = str(extra.get("access_token") or getattr(account, "token", "") or "").strip()
        id_token = str(extra.get("id_token") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()
        oauth_json = _build_oauth_codex_session_json(account, extra)
        sync_auth_mode = "oauth"
        registry_status = "skipped"
        auth_json: dict[str, Any] | None = None

        use_oauth = preferred_mode in {"oauth", "codex", "codex_oauth", "bearer", "auto", ""}
        use_agent = preferred_mode in {"agent", "agent_identity"} or (
            preferred_mode == "auto" and allow_agent_identity and not oauth_json
        )

        if use_oauth and not oauth_json and not allow_agent_identity:
            raise Sub2ApiSyncError(
                "missing codex credential: need access_token + refresh_token from "
                "Codex PKCE after phone bind (Agent Identity disabled)"
            )

        if use_oauth and oauth_json:
            auth_json = oauth_json
            sync_auth_mode = "oauth"
            log(
                "  [Sub2API] Using Codex OAuth session "
                f"(refresh_token=yes len={len(refresh_token)} access_len={len(access_token)})"
            )
        elif use_agent or (allow_agent_identity and not oauth_json):
            from platforms.chatgpt.agent_identity import AgentIdentityError
            from platforms.chatgpt.agent_identity import certificate_to_sub2api_export
            from platforms.chatgpt.agent_identity import has_identity_claims
            from platforms.chatgpt.agent_identity import register_identity

            identity_token = id_token if id_token and has_identity_claims(id_token) else access_token
            if not access_token or not identity_token or not has_identity_claims(identity_token):
                raise Sub2ApiSyncError(
                    "missing codex credential: need OAuth refresh_token "
                    "(after phone bind) or Agent Identity claims"
                )

            sync_auth_mode = "agent_identity"
            registry_status = "enabled"
            if not certificate:
                proxy_url = str(extra.get("auth_proxy_url") or "").strip()
                try:
                    certificate = register_identity(
                        {"access_token": access_token, "id_token": identity_token},
                        proxy_url=proxy_url or None,
                        proxy_region=proxy_region,
                    )
                except AgentIdentityError as exc:
                    if not _is_agent_registry_disabled(exc):
                        raise
                    raise Sub2ApiSyncError(
                        "agent_registry_not_enabled: Agent Registry is not enabled for this account"
                    ) from exc

            if certificate:
                exported = certificate_to_sub2api_export(certificate)
                auth_json = {
                    "auth_mode": "agentIdentity",
                    "agent_identity": dict(exported["agent_identity"]),
                }
        else:
            raise Sub2ApiSyncError(
                "missing codex credential: account has no refresh_token for OAuth upload "
                "(complete phone verification / Codex PKCE first)"
            )

        if not auth_json:
            raise Sub2ApiSyncError("failed to build Sub2API import payload")

        client = Sub2ApiClient(config["url"], config["email"], config["password"])
        try:
            if not group_ids:
                resolved_group_id = client.resolve_group_id(group_name)
                if not resolved_group_id:
                    raise Sub2ApiSyncError(f"Sub2API group not found: {group_name}")
                group_ids = [resolved_group_id]
            if not models:
                for group_id in group_ids:
                    models.extend(client.resolve_group_models(group_id))
                models = _model_list(models)
                if not models:
                    raise Sub2ApiSyncError("Sub2API free group does not provide a model list")
            # Free accounts cannot use Sol. The model list itself comes from
            # the configured Sub2API group, so new models are supported without
            # another code release while Sol aliases are removed consistently.
            models, dropped = _filter_models_for_group(models, group_name)
            if dropped:
                log(
                    "  [Sub2API] Free group dropped Sol models from import: "
                    + ",".join(dropped[:12])
                )
            # If an older saved setting contains only Sol (for example the
            # former gpt-5.6 alias), transparently switch to the current group
            # model list instead of failing the whole upload. This is the
            # "自动支持最新模型" path and also repairs existing settings.
            if not models and dropped and _is_free_group(group_name):
                refreshed_models: list[str] = []
                for group_id in group_ids:
                    refreshed_models.extend(client.resolve_group_models(group_id))
                models, refreshed_dropped = _filter_models_for_group(
                    refreshed_models,
                    group_name,
                )
                if refreshed_dropped:
                    log(
                        "  [Sub2API] Automatic Free model refresh dropped Sol aliases: "
                        + ",".join(refreshed_dropped[:12])
                    )
                if models:
                    log(
                        "  [Sub2API] Previous Sol-only selection was replaced with the current Free group model list"
                    )
            if not models:
                raise Sub2ApiSyncError(
                    f"Sub2API group {group_name or 'free'} has no usable models after Free/Sol filtering"
                )
            test_models = list(models)
            remote_account_id = client.import_codex_session(
                auth_json,
                name=str(getattr(account, "email", "") or "ChatGPT Free"),
                proxy_id=proxy_id,
                group_ids=group_ids,
                models=models,
                concurrency=10,
            )
            try:
                test_result = client.test_account(
                    remote_account_id,
                    model=test_models[0] if test_models else "",
                )
            except Exception as exc:
                test_result = {"success": False, "error": f"Sub2API account test request failed: {exc}"}
            try:
                remote = client.get_account(remote_account_id)
            except Exception as exc:
                remote = {"status": "unknown", "schedulable": False, "error_message": str(exc)}
            # Hard-fail revoked OAuth tokens so we never leave a zombie account as "active".
            test_error = str((test_result or {}).get("error") or "").lower()
            if any(
                marker in test_error
                for marker in (
                    "token_revoked",
                    "invalidated oauth token",
                    "authentication token has been invalidated",
                    "token_invalidated",
                )
            ):
                sync_status, verification_reason = "invalid", str((test_result or {}).get("error") or "token_revoked")
            else:
                sync_status, verification_reason = _classify_remote_state(remote, test_result)
            deleted_remote = False
            if sync_status == "invalid":
                try:
                    client.delete_account(remote_account_id)
                    deleted_remote = True
                    log(
                        f"  [Sub2API] Removed invalid remote account {remote_account_id} "
                        f"({verification_reason[:120]})"
                    )
                except Exception as exc:
                    verification_reason = (
                        f"{verification_reason}; remote cleanup failed: {exc}"
                    ).strip("; ")
        finally:
            client.close()

        state = {
            "remote_account_id": 0 if sync_status == "invalid" and deleted_remote else remote_account_id,
            "status": sync_status,
            "synced_at": _utcnow_iso(),
            "base_url": str(config["url"]).rstrip("/"),
            "proxy_id": proxy_id,
            "proxy_region": proxy_region,
            "group_ids": group_ids,
            "models": models,
            "concurrency": 10,
            "auth_mode": sync_auth_mode,
            "agent_registry_status": registry_status,
            "verification_status": "passed" if test_result.get("success") else "failed",
            "verified_at": _utcnow_iso(),
            **_remote_state_fields(remote),
        }
        if verification_reason:
            state["last_error"] = verification_reason[:500]
        if sync_status == "invalid" and deleted_remote:
            state["deleted_remote_account_id"] = remote_account_id
            state["deleted_at"] = _utcnow_iso()
            state["delete_reason"] = verification_reason or "terminal remote verification failure"
        _persist_state(local_account_id, certificate=certificate or None, sync_state=state)
        if certificate:
            extra["agent_identity_certificate"] = certificate
        extra["sub2api_sync"] = state
        account.extra = extra
        model_label = ",".join(models) if models else "自动"
        upload_label = (
            "Agent Identity"
            if sync_auth_mode == "agent_identity"
            else "Codex OAuth session"
        )
        log(
            f"  [Sub2API] {upload_label} import result: status={sync_status} "
            f"remote_account_id={remote_account_id} proxy_id={proxy_id or '-'} "
            f"region={proxy_region or '-'} groups={','.join(map(str, group_ids))} "
            f"concurrency=10 model={model_label}"
        )
        return sync_status in {"imported_active", "imported_cooling"}
    except Exception as exc:
        registry_disabled = _is_agent_registry_disabled(exc) or "sub2_ineligible" in str(exc).lower()
        if registry_disabled:
            failure_state = _retry_state(
                current_state,
                pending_status="registry_pending",
                counter_key="registry_retry_count",
                delays=_REGISTRY_RETRY_DELAYS_SECONDS,
                final_status="registry_ineligible",
            )
            failure_state["agent_registry_status"] = (
                "not_enabled" if failure_state["status"] == "registry_ineligible" else "pending"
            )
        elif _is_credentials_pending_error(exc):
            failure_state = _retry_state(
                current_state,
                pending_status="credentials_pending",
                counter_key="credentials_retry_count",
                delays=_CREDENTIAL_RETRY_DELAYS_SECONDS,
            )
            failure_state["credential_status"] = "pending"
        elif _is_terminal_identity_error(exc):
            failure_state = {**current_state, "status": "invalid", "last_attempt_at": _utcnow_iso()}
        else:
            failure_state = _retry_state(
                current_state,
                pending_status="sync_pending",
                counter_key="sync_retry_count",
                delays=_SYNC_RETRY_DELAYS_SECONDS,
            )
        failure_state["last_error"] = str(exc)[:500]
        failure_state["base_url"] = str(config["url"]).rstrip("/")
        if registry_disabled:
            failure_state["agent_registry_error"] = "not_enabled"
        _persist_state(
            local_account_id,
            certificate=certificate or None,
            sync_state=failure_state,
        )
        extra["sub2api_sync"] = failure_state
        account.extra = extra
        log(f"  [Sub2API] Auto sync failed; local account retained: {exc}")
        return False


def delete_synced_account(local_account_id: int, *, reason: str = "invalid", log_fn=None) -> bool:
    log = log_fn or logger.info
    config = _get_config()
    if not config["auto_delete"] or not config["url"]:
        return False

    from sqlmodel import Session

    from core.account_graph import load_account_graphs
    from core.db import AccountModel, engine

    with Session(engine) as session:
        model = session.get(AccountModel, int(local_account_id))
        if not model:
            return False
        graph = load_account_graphs(session, [int(local_account_id)]).get(int(local_account_id), {})
        overview = dict(graph.get("overview") or {})
        legacy = dict(overview.get("legacy_extra") or {})
        state = dict(legacy.get("sub2api_sync") or {})
        email = model.email

    remote_account_id = int(state.get("remote_account_id") or 0)
    if remote_account_id <= 0:
        return False
    try:
        client = Sub2ApiClient(config["url"], config["email"], config["password"])
        try:
            client.delete_account(remote_account_id)
        finally:
            client.close()
        deleted_state = {
            **state,
            "remote_account_id": 0,
            "deleted_remote_account_id": remote_account_id,
            "status": "deleted",
            "deleted_at": _utcnow_iso(),
            "delete_reason": str(reason or "invalid"),
        }
        _persist_state(int(local_account_id), sync_state=deleted_state)
        log(f"  [Sub2API] Deleted expired account {email}: remote_account_id={remote_account_id}")
        return True
    except Exception as exc:
        log(f"  [Sub2API] Failed to delete remote account {remote_account_id}: {exc}")
        return False


def cleanup_invalid_synced_accounts(*, limit: int = 500, log_fn=None) -> dict[str, int]:
    config = _get_config()
    results = {"deleted": 0, "failed": 0, "skipped": 0}
    if not config["auto_delete"] or not config["url"]:
        return results

    from sqlmodel import Session, select

    from core.account_graph import load_account_graphs
    from core.db import AccountModel, engine

    with Session(engine) as session:
        accounts = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            .limit(max(int(limit), 1))
        ).all()
        graphs = load_account_graphs(session, [int(item.id or 0) for item in accounts if item.id])

    targets: list[tuple[int, str]] = []
    for model in accounts:
        account_id = int(model.id or 0)
        graph = graphs.get(account_id, {})
        overview = dict(graph.get("overview") or {})
        lifecycle = str(graph.get("lifecycle_status") or overview.get("lifecycle_status") or "").lower()
        validity = str(graph.get("validity_status") or overview.get("validity_status") or "").lower()
        legacy = dict(overview.get("legacy_extra") or {})
        state = dict(legacy.get("sub2api_sync") or {})
        if int(state.get("remote_account_id") or 0) <= 0:
            results["skipped"] += 1
            continue
        if validity == "invalid" or lifecycle in {"invalid", "expired"}:
            targets.append((account_id, validity or lifecycle))
        else:
            results["skipped"] += 1

    for account_id, reason in targets:
        if delete_synced_account(account_id, reason=reason, log_fn=log_fn):
            results["deleted"] += 1
        else:
            results["failed"] += 1
    return results


def reconcile_sub2_remote_statuses(
    *,
    limit: int = 500,
    account_ids: list[int] | None = None,
    log_fn=None,
) -> dict[str, int]:
    """Reflect terminal Sub2 authentication failures back into local validity."""
    log = log_fn or logger.info
    results = {
        "checked": 0,
        "invalid": 0,
        "healthy": 0,
        "active": 0,
        "cooling": 0,
        "unschedulable": 0,
        "failed": 0,
    }
    config = _get_config()
    if not config["auto_sync"] or not config["url"]:
        return results
    if not config["email"] or not config["password"]:
        return results

    from sqlmodel import Session, select

    from core.account_graph import load_account_graphs
    from core.db import AccountModel, engine

    with Session(engine) as session:
        query = select(AccountModel).where(AccountModel.platform == "chatgpt")
        normalized_ids = [int(item) for item in (account_ids or []) if int(item or 0) > 0]
        if account_ids is not None:
            if not normalized_ids:
                return results
            query = query.where(AccountModel.id.in_(normalized_ids))
        accounts = session.exec(
            query
            .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            .limit(max(int(limit), 1))
        ).all()
        graphs = load_account_graphs(session, [int(item.id or 0) for item in accounts if item.id])

    targets: list[tuple[int, str, dict[str, Any]]] = []
    for model in accounts:
        account_id = int(model.id or 0)
        graph = graphs.get(account_id, {})
        overview = dict(graph.get("overview") or {})
        legacy = dict(overview.get("legacy_extra") or {})
        state = dict(legacy.get("sub2api_sync") or {})
        remote_account_id = _as_positive_int(state.get("remote_account_id"))
        if remote_account_id:
            targets.append((account_id, model.email, state))

    client = Sub2ApiClient(config["url"], config["email"], config["password"])
    try:
        for local_account_id, email, state in targets:
            remote_account_id = _as_positive_int(state.get("remote_account_id"))
            try:
                remote = client.get_account(remote_account_id)
                results["checked"] += 1
                updated_state = {
                    **state,
                    **_remote_state_fields(remote),
                }
                terminal_reason = (
                    "Legacy bearer fallback is not valid for Codex API authentication"
                    if str(state.get("auth_mode") or "").strip().lower() == "bearer_fallback"
                    else ""
                )
                remote_state, remote_reason = _classify_remote_state(remote)
                if terminal_reason or remote_state == "invalid":
                    terminal_reason = terminal_reason or remote_reason
                    updated_state["status"] = "invalid"
                    updated_state["last_error"] = terminal_reason[:500]
                    _persist_state(local_account_id, sync_state=updated_state)
                    results["invalid"] += 1
                    log(
                        f"  [Sub2API] Remote credential marked invalid; ChatGPT status unchanged: "
                        f"{email} remote_account_id={remote_account_id}"
                    )
                else:
                    updated_state["status"] = remote_state
                    if remote_reason:
                        updated_state["last_error"] = remote_reason[:500]
                    else:
                        updated_state.pop("last_error", None)
                    _persist_state(local_account_id, sync_state=updated_state)
                    if remote_state == "imported_active":
                        results["active"] += 1
                        results["healthy"] += 1
                    elif remote_state == "imported_cooling":
                        results["cooling"] += 1
                        results["healthy"] += 1
                    else:
                        results["unschedulable"] += 1
            except Exception as exc:
                results["failed"] += 1
                log(
                    f"  [Sub2API] Remote status check failed: "
                    f"{email} remote_account_id={remote_account_id} error={exc}"
                )
    finally:
        client.close()
    return results


def repair_misclassified_registry_ineligible_accounts(*, limit: int = 1000) -> int:
    """Repair records that older builds globally invalidated for Sub2-only eligibility."""
    from sqlmodel import Session, select

    from core.account_graph import load_account_graphs, patch_account_graph
    from core.db import AccountModel, engine

    repaired = 0
    with Session(engine) as session:
        models = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            .limit(max(int(limit), 1))
        ).all()
        graphs = load_account_graphs(
            session,
            [int(model.id or 0) for model in models if model.id],
        )
        for model in models:
            account_id = int(model.id or 0)
            graph = graphs.get(account_id, {})
            overview = dict(graph.get("overview") or {})
            legacy = dict(overview.get("legacy_extra") or {})
            state = dict(legacy.get("sub2api_sync") or {})
            error = str(state.get("last_error") or "").lower()
            reason = str(overview.get("validity_reason") or "").lower()
            if "sub2_ineligible" not in error and "agent registry is not enabled" not in reason:
                continue
            if int(state.get("remote_account_id") or 0) > 0:
                continue
            state["status"] = "registry_pending"
            state["agent_registry_status"] = "pending"
            state["registry_retry_count"] = 0
            state["retry_count"] = 0
            state["next_retry_at"] = _utcnow_iso()
            state["reclassified_at"] = _utcnow_iso()
            legacy["sub2api_sync"] = state
            patch_account_graph(
                session,
                model,
                summary_updates={
                    "valid": None,
                    "validity_status": "unknown",
                    "validity_reason": "Sub2 Agent Registry is not enabled; ChatGPT validity is unverified",
                    "invalid_detected_at": "",
                    "legacy_extra": legacy,
                },
            )
            session.add(model)
            repaired += 1
        if repaired:
            session.commit()
    return repaired


def backfill_unsynced_accounts(*, limit: int = 500, log_fn=None) -> dict[str, int]:
    """Upload active ChatGPT accounts that do not have a remote Sub2API ID."""
    log = log_fn or logger.info
    results = {"synced": 0, "failed": 0, "skipped": 0}
    config = _get_config()
    if not config["auto_sync"] or not config["url"]:
        return results

    from sqlmodel import Session, select

    from core.account_graph import load_account_graphs
    from core.db import AccountModel, engine
    from core.platform_accounts import build_platform_account

    with Session(engine) as session:
        models = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            .limit(max(int(limit), 1))
        ).all()
        graphs = load_account_graphs(session, [int(item.id or 0) for item in models if item.id])
        targets = []
        now = datetime.now(timezone.utc)
        for model in models:
            account_id = int(model.id or 0)
            graph = graphs.get(account_id, {})
            overview = dict(graph.get("overview") or {})
            lifecycle = str(graph.get("lifecycle_status") or overview.get("lifecycle_status") or "").lower()
            validity = str(graph.get("validity_status") or overview.get("validity_status") or "").lower()
            legacy = dict(overview.get("legacy_extra") or {})
            state = dict(legacy.get("sub2api_sync") or {})
            if int(state.get("remote_account_id") or 0) > 0:
                results["skipped"] += 1
                continue
            if lifecycle in {"invalid", "expired"} or validity == "invalid":
                results["skipped"] += 1
                continue
            state_status = str(state.get("status") or "").strip().lower()
            if state_status in {"invalid", "deleted", "registry_ineligible"}:
                results["skipped"] += 1
                continue
            next_retry_at = _parse_iso(state.get("next_retry_at"))
            if next_retry_at and next_retry_at > now:
                results["skipped"] += 1
                continue
            targets.append(build_platform_account(session, model))

    for account in targets:
        if push_account_to_sub2api(account, log_fn=log):
            results["synced"] += 1
        else:
            results["failed"] += 1
    return results


def repair_missing_auth_proxy_from_active_pool(*, limit: int = 20) -> int:
    """Attach the current active proxy to recent accounts missing auth_proxy_url."""
    from sqlmodel import Session, select

    from core.account_graph import patch_account_graph
    from core.db import AccountModel, engine
    from core.platform_accounts import build_platform_account
    from infrastructure.proxies_repository import ProxiesRepository

    proxy = next(
        (
            item.url
            for item in ProxiesRepository().list()
            if item.is_active and "711proxy.com" in str(item.url or "").lower()
        ),
        "",
    )
    if not proxy:
        return 0

    repaired = 0
    with Session(engine) as session:
        models = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            .limit(max(int(limit), 1))
        ).all()
        for model in models:
            account = build_platform_account(session, model)
            if str((account.extra or {}).get("auth_proxy_url") or "").strip():
                continue
            patch_account_graph(
                session,
                model,
                credential_updates={"auth_proxy_url": proxy},
            )
            session.add(model)
            repaired += 1
        session.commit()
    return repaired
