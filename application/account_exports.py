from __future__ import annotations

import base64
import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from core.datetime_utils import serialize_datetime
from domain.accounts import AccountExportSelection, AccountRecord
from infrastructure.accounts_repository import AccountsRepository


CHATGPT_PLATFORM = "chatgpt"
DEFAULT_CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


@dataclass(slots=True)
class ExportArtifact:
    filename: str
    media_type: str
    content: str | bytes | io.BytesIO
    account_ids: list[int] = field(default_factory=list)
    exported_units: list[dict] = field(default_factory=list)


def _decode_jwt_part(token: str, index: int) -> dict:
    try:
        parts = token.split(".")
        if len(parts) <= index:
            return {}
        payload = parts[index]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _decode_jwt_payload(token: str) -> dict:
    return _decode_jwt_part(token, 1)


def _decode_jwt_header(token: str) -> dict:
    return _decode_jwt_part(token, 0)


def _is_synthetic_chatgpt_id_token(token: str, explicit_marker: object = None) -> bool:
    if explicit_marker is True:
        return True
    header = _decode_jwt_header(str(token or ""))
    return bool(header.get("cpa_synthetic"))


def _export_id_token(access_token: str, id_token: str, explicit_marker: object = None) -> str:
    """Return only a real id_token, or access_token as the CPA compatibility alias."""

    if id_token and not _is_synthetic_chatgpt_id_token(id_token, explicit_marker):
        return id_token
    return access_token


def _isoformat(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _timestamp_name(prefix: str, suffix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}.{suffix}"


def _safe_export_name(value: str) -> str:
    text = str(value or "").strip() or "account"
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in text).strip("-") or "account"


def _account_ids(items: list[AccountRecord]) -> list[int]:
    return [int(item.id) for item in items if int(item.id or 0) > 0]


def _limit_export_units(items: list, limit: int) -> list:
    return items[:limit] if int(limit or 0) > 0 else items


def _payload_export_units(payloads: list[dict]) -> list[dict]:
    units: list[dict] = []
    for payload in payloads:
        account_id = int(payload.get("id") or 0)
        if account_id <= 0:
            continue
        units.append(
            {
                "account_id": account_id,
                "workspace_id": str(payload.get("workspace_id") or ""),
                "workspace_unit": bool(payload.get("_workspace_unit")),
            }
        )
    return units


def _exported_account_ids_from_units(units: list[dict]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for unit in units:
        account_id = int(unit.get("account_id") or 0)
        if account_id > 0 and account_id not in seen:
            seen.add(account_id)
            result.append(account_id)
    return result


def _credential_value(item: AccountRecord, *keys: str) -> str:
    for key in keys:
        for credential in item.credentials or []:
            if credential.get("scope") == "platform" and credential.get("key") == key and credential.get("value"):
                return str(credential["value"])
    return ""


def _mailbox_provider_name(item: AccountRecord) -> str:
    for resource in item.provider_resources or []:
        if resource.get("resource_type") == "mailbox" and resource.get("provider_name"):
            return str(resource["provider_name"])
    for provider_account in item.provider_accounts or []:
        if provider_account.get("provider_type") == "mailbox" and provider_account.get("provider_name"):
            return str(provider_account["provider_name"])
    return ""


def _chatgpt_auth_info(*tokens: str) -> dict:
    merged: dict = {}
    for token in tokens:
        if not token:
            continue
        payload = _decode_jwt_payload(token)
        auth_info = payload.get("https://api.openai.com/auth", {})
        if isinstance(auth_info, dict):
            for key, value in auth_info.items():
                if value not in (None, "", [], {}):
                    merged[key] = value
    return merged


def _chatgpt_plan_type_from_auth(auth_info: dict) -> str:
    return str(auth_info.get("chatgpt_plan_type") or auth_info.get("plan_type") or "").strip()


def _chatgpt_is_workspace_plan(plan_type: str) -> bool:
    raw = str(plan_type or "").strip().lower()
    if not raw or any(token in raw for token in ("free", "personal")):
        return False
    return any(
        token in raw
        for token in (
            "workspace",
            "team",
            "business",
            "enterprise",
            "edu",
            "education",
            "teacher",
            "school",
            "k12",
        )
    )


def _ensure_workspace_exportable(payload: dict) -> None:
    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not workspace_id:
        return
    plan_type = str(payload.get("token_plan_type") or "").strip()
    if _chatgpt_is_workspace_plan(plan_type):
        return
    raise ValueError(
        f"{payload.get('email') or 'ChatGPT account'} 已记录 workspace_id={workspace_id[:8]}..., "
        f"但当前 access_token 仍是 {plan_type or 'unknown'} 个人上下文。"
        "请先重新完成 workspace session 导出/刷新后再导出 Sub2Api/CPA。"
    )


def _chatgpt_export_payload(item: AccountRecord) -> dict:
    access_token = _credential_value(item, "access_token", "accessToken", "legacy_token")
    refresh_token = _credential_value(item, "refresh_token", "refreshToken")
    raw_id_token = _credential_value(item, "id_token", "idToken")
    session_token = _credential_value(item, "session_token", "sessionToken")
    workspace_id = _credential_value(item, "workspace_id", "workspaceId")
    payload = _decode_jwt_payload(access_token) if access_token else {}
    id_token = _export_id_token(access_token, raw_id_token)
    auth_info = _chatgpt_auth_info(access_token, id_token)
    client_id = _credential_value(item, "client_id", "clientId") or str(payload.get("client_id", "") or DEFAULT_CHATGPT_CLIENT_ID)
    cookies = _credential_value(item, "cookies", "cookie")
    account_id = _credential_value(item, "chatgpt_account_id", "account_id") or item.user_id or ""
    email_service = _mailbox_provider_name(item)

    if not account_id:
        account_id = str(auth_info.get("chatgpt_account_id", "") or auth_info.get("account_id", "") or "")
    if not workspace_id:
        workspace_id = str(auth_info.get("organization_id", "") or "")
    token_plan_type = _chatgpt_plan_type_from_auth(auth_info)
    expires_at = None
    exp_timestamp = payload.get("exp")
    if isinstance(exp_timestamp, int) and exp_timestamp > 0:
        expires_at = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
    last_refresh_at = item.updated_at
    iat_timestamp = payload.get("iat")
    if isinstance(iat_timestamp, int) and iat_timestamp > 0:
        last_refresh_at = datetime.fromtimestamp(iat_timestamp, tz=timezone.utc)

    return {
        "id": item.id,
        "email": item.email,
        "password": item.password,
        "client_id": client_id,
        "account_id": account_id,
        "workspace_id": workspace_id,
        "token_plan_type": token_plan_type,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "cookies": cookies,
        "email_service": email_service,
        "registered_at": _isoformat(item.created_at),
        "last_refresh": _isoformat(last_refresh_at),
        "expires_at": _isoformat(expires_at),
        "status": item.display_status,
        "expires_at_unix": int(expires_at.timestamp()) if expires_at else 0,
        "_workspace_unit": False,
    }


def _make_agent_identity_sub2api_json(item: AccountRecord) -> dict:
    payload = _chatgpt_export_payload(item)
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise ValueError(f"账号 {item.email} 缺少 access_token，无法生成 Agent Identity")

    from platforms.chatgpt.agent_identity import AgentIdentityError
    from platforms.chatgpt.agent_identity import certificate_to_sub2api_export
    from platforms.chatgpt.agent_identity import has_identity_claims
    from platforms.chatgpt.agent_identity import register_identity

    identity_token = next(
        (
            token
            for token in (str(payload.get("id_token") or ""), access_token)
            if token and has_identity_claims(token)
        ),
        "",
    )
    if not identity_token:
        raise ValueError(f"账号 {item.email} 的 OAuth token 缺少 Agent Identity 账户 claims")

    legacy_extra = dict((item.overview or {}).get("legacy_extra") or {})
    cached_certificate = legacy_extra.get("agent_identity_certificate")
    if isinstance(cached_certificate, dict) and cached_certificate.get("agent_runtime_id"):
        try:
            return certificate_to_sub2api_export(dict(cached_certificate))
        except (AgentIdentityError, KeyError, TypeError, ValueError):
            pass
    proxy_url = str(
        _credential_value(item, "auth_proxy_url")
        or legacy_extra.get("auth_proxy_url")
        or ""
    ).strip()
    try:
        certificate = register_identity(
            {"access_token": access_token, "id_token": identity_token},
            proxy_url=proxy_url or None,
        )
        return certificate_to_sub2api_export(certificate)
    except AgentIdentityError as exc:
        raise ValueError(f"账号 {item.email} 生成 Agent Identity 失败: {exc}") from exc


def _workspace_statuses(item: AccountRecord) -> dict:
    overview = item.overview or {}
    statuses = overview.get("workspace_statuses")
    if isinstance(statuses, dict) and statuses:
        return statuses
    legacy_join = ((overview.get("legacy_extra") or {}).get("workspace_join") or {})
    statuses = legacy_join.get("workspace_statuses")
    return statuses if isinstance(statuses, dict) else {}


def _load_workspace_json(path: str) -> dict:
    text = str(path or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(Path(text).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _chatgpt_workspace_export_payloads(item: AccountRecord) -> list[dict]:
    base_payload = _chatgpt_export_payload(item)
    payloads: list[dict] = []
    for workspace_id, status in _workspace_statuses(item).items():
        if not isinstance(status, dict) or status.get("status") != "export_ok":
            continue
        workspace_json = _load_workspace_json(str(status.get("json_path") or ""))
        if not workspace_json:
            continue

        access_token = str(workspace_json.get("access_token") or base_payload["access_token"] or "")
        id_token = _export_id_token(
            access_token,
            str(workspace_json.get("id_token") or base_payload["id_token"] or ""),
            workspace_json.get("id_token_synthetic"),
        )
        auth_info = _chatgpt_auth_info(access_token, id_token)
        payload = dict(base_payload)
        payload.update(
            {
                "access_token": access_token,
                "refresh_token": str(workspace_json.get("refresh_token") or base_payload["refresh_token"] or ""),
                "id_token": id_token,
                "session_token": str(workspace_json.get("session_token") or base_payload["session_token"] or ""),
                "account_id": str(
                    workspace_json.get("chatgpt_account_id")
                    or workspace_json.get("account_id")
                    or base_payload["account_id"]
                    or workspace_id
                    or ""
                ),
                # workspace_statuses 的 key 是本次 join/export 针对的 workspace/account ID；
                # 每个导出条目必须用它区分，否则一个账号加入多个空间时会被压成首个空间。
                "workspace_id": str(workspace_id or ""),
                "token_plan_type": str(
                    workspace_json.get("chatgpt_plan_type")
                    or workspace_json.get("plan_type")
                    or _chatgpt_plan_type_from_auth(auth_info)
                    or base_payload["token_plan_type"]
                    or ""
                ),
                "last_refresh": str(workspace_json.get("last_refresh") or base_payload["last_refresh"] or ""),
                "expires_at": str(
                    workspace_json.get("expired")
                    or workspace_json.get("expires_at")
                    or base_payload["expires_at"]
                    or ""
                ),
                "_workspace_unit": True,
            }
        )
        payloads.append(payload)
    return payloads or [base_payload]


def _chatgpt_workspace_cpa_jsons(item: AccountRecord) -> list[tuple[str, dict, bool]]:
    tokens: list[tuple[str, dict, bool]] = []
    errors: list[str] = []
    for workspace_id, status in _workspace_statuses(item).items():
        if not isinstance(status, dict) or status.get("status") != "export_ok":
            continue
        workspace_json = _load_workspace_json(str(status.get("json_path") or ""))
        if workspace_json:
            workspace_json = dict(workspace_json)
            workspace_json.setdefault("email", item.email)
            workspace_json.setdefault("workspace_id", str(workspace_id or ""))
            workspace_json.setdefault("organization_id", str(workspace_id or ""))
            workspace_json["id_token"] = _export_id_token(
                str(workspace_json.get("access_token") or ""),
                str(workspace_json.get("id_token") or ""),
                workspace_json.get("id_token_synthetic"),
            )
            workspace_json.pop("id_token_synthetic", None)
            try:
                from platforms.chatgpt.cpa_session import assert_workspace_cpa_json

                assert_workspace_cpa_json(workspace_json, workspace_id=str(workspace_id or ""))
            except Exception as exc:
                errors.append(f"{item.email} workspace_id={str(workspace_id)[:8]}...: {exc}")
                continue
            tokens.append((str(workspace_id or ""), workspace_json, True))
    if errors and not tokens:
        raise ValueError("workspace CPA JSON 不可导出: " + " | ".join(errors[:3]))
    return tokens


def _compact_auto_token_json(token: dict, *, email: str, workspace_id: str = "") -> dict:
    """Normalize CPA workspace JSON for Compact Auto style imports.

    Keep all original token fields, but add common aliases many panels use to
    classify K12/private workspace tokens.  This does not fabricate token
    privileges; export-time validation still requires a real workspace token.
    """

    data = dict(token or {})
    data.setdefault("type", "codex")
    data.setdefault("platform", "openai")
    data.setdefault("auth_type", "oauth")
    data.setdefault("email", email)
    if workspace_id:
        data.setdefault("workspace_id", workspace_id)
        data.setdefault("organization_id", workspace_id)
    plan_type = str(data.get("chatgpt_plan_type") or data.get("plan_type") or "").strip()
    if plan_type:
        data.setdefault("plan_type", plan_type)
        data.setdefault("chatgpt_plan_type", plan_type)
    if data.get("expired") and not data.get("expires_at"):
        data["expires_at"] = data.get("expired")
    if data.get("account_id") and not data.get("chatgpt_account_id"):
        data["chatgpt_account_id"] = data.get("account_id")
    data["id_token"] = _export_id_token(
        str(data.get("access_token") or ""),
        str(data.get("id_token") or ""),
        data.get("id_token_synthetic"),
    )
    data.pop("id_token_synthetic", None)
    data.setdefault("private", True)
    data.setdefault("is_private", True)
    return data


def _sub2api_from_payloads(payloads: list[dict]) -> dict:
    accounts = []
    for payload in payloads:
        _ensure_workspace_exportable(payload)
        accounts.append(
            {
                "name": payload["email"],
                "platform": "openai",
                "type": "oauth",
                "credentials": {
                    "access_token": payload["access_token"],
                    "chatgpt_account_id": payload["account_id"],
                    "chatgpt_user_id": "",
                    "client_id": payload["client_id"],
                    "expires_at": payload["expires_at_unix"],
                    "expires_in": 863999,
                    "model_mapping": {
                        "gpt-5.1": "gpt-5.1",
                        "gpt-5.1-codex": "gpt-5.1-codex",
                        "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                        "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                        "gpt-5.2": "gpt-5.2",
                        "gpt-5.2-codex": "gpt-5.2-codex",
                    },
                    "organization_id": payload["workspace_id"],
                    "refresh_token": payload["refresh_token"],
                    "plan_type": payload["token_plan_type"],
                },
                "extra": {},
                "concurrency": 10,
                "priority": 1,
                "rate_multiplier": 1,
                "auto_pause_on_expired": True,
            }
        )
    return {"proxies": [], "accounts": accounts}


def _to_cpa_account(item: AccountRecord) -> SimpleNamespace:
    payload = _chatgpt_export_payload(item)
    _ensure_workspace_exportable(payload)
    return SimpleNamespace(
        email=payload["email"],
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        id_token=payload["id_token"],
        session_token=payload["session_token"],
        account_id=payload["account_id"],
        user_id=payload["account_id"],
        expired=payload["expires_at"],
        last_refresh=payload["last_refresh"],
        client_id=payload["client_id"],
        cookies=payload["cookies"],
        credentials={
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "id_token": payload["id_token"],
            "session_token": payload["session_token"],
            "account_id": payload["account_id"],
            "chatgpt_account_id": payload["account_id"],
            "client_id": payload["client_id"],
            "cookies": payload["cookies"],
        },
    )


def _generate_cpa_token_json(item: AccountRecord) -> dict:
    from platforms.chatgpt.cpa_upload import generate_token_json

    return generate_token_json(_to_cpa_account(item))


def _make_sub2api_json(item: AccountRecord) -> dict:
    return _sub2api_from_payloads(_chatgpt_workspace_export_payloads(item))


def _make_cockpit_token(item: AccountRecord) -> dict:
    payload = _chatgpt_export_payload(item)
    return {
        "type": "codex",
        "id_token": payload["id_token"],
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "account_id": payload["account_id"],
        "last_refresh": payload["last_refresh"] or "",
        "email": payload["email"],
        "expired": payload["expires_at"] or "",
        "account_note": "",
    }


def _make_kiro_go_account(item: AccountRecord) -> dict:
    """Convert a Kiro AccountRecord to Kiro-Go Account JSON format."""
    import uuid
    import time

    access_token = _credential_value(item, "accessToken", "access_token", "legacy_token")
    refresh_token = _credential_value(item, "refreshToken", "refresh_token")
    client_id = _credential_value(item, "clientId", "client_id")
    client_secret = _credential_value(item, "clientSecret", "client_secret")
    session_token = _credential_value(item, "sessionToken", "session_token")
    oauth_provider = _credential_value(item, "oauthProvider")

    # Determine auth method
    auth_method = "idc"
    provider = "BuilderId"
    if oauth_provider:
        lp = oauth_provider.lower()
        if lp in ("google", "github"):
            auth_method = "social"
            provider = "Google" if lp == "google" else "GitHub"

    return {
        "id": str(uuid.uuid4()),
        "email": item.email,
        "nickname": item.email.split("@")[0] if item.email else "",
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "clientId": client_id,
        "clientSecret": client_secret,
        "authMethod": auth_method,
        "provider": provider,
        "region": "us-east-1",
        "startUrl": "https://view.awsapps.com/start" if auth_method == "idc" else "",
        "expiresAt": int(time.time()) + 3600,
        "machineId": str(uuid.uuid4()),
        "weight": 0,
        "enabled": True,
    }


def _make_any2api_kiro_account(item: AccountRecord) -> dict:
    """Convert a Kiro AccountRecord to Any2API KiroAccount format."""
    import uuid

    access_token = _credential_value(item, "accessToken", "access_token", "legacy_token")
    return {
        "id": str(uuid.uuid4()),
        "name": item.email or f"Kiro Account",
        "accessToken": access_token,
        "machineId": str(uuid.uuid4()),
        "preferredEndpoint": "",
        "active": True,
        "updatedAt": _isoformat(item.updated_at) or _isoformat(item.created_at) or "",
    }


def _make_any2api_grok_token(item: AccountRecord) -> dict:
    """Convert a Grok AccountRecord to Any2API GrokToken format."""
    import uuid

    sso = _credential_value(item, "sso")
    sso_rw = _credential_value(item, "sso_rw")
    cookie_token = sso or sso_rw
    return {
        "id": str(uuid.uuid4()),
        "name": item.email or "Grok Token",
        "cookieToken": cookie_token,
        "active": True,
        "updatedAt": _isoformat(item.updated_at) or _isoformat(item.created_at) or "",
    }


def _build_any2api_admin_config(items: list[AccountRecord]) -> dict:
    """Build an Any2API admin.json from a list of accounts (multi-platform)."""
    kiro_accounts = []
    grok_tokens = []
    cursor_config = {}
    blink_config = {}
    chatgpt_config = {}

    for item in items:
        if item.platform == "kiro":
            kiro_accounts.append(_make_any2api_kiro_account(item))
        elif item.platform == "grok":
            grok_tokens.append(_make_any2api_grok_token(item))
        elif item.platform == "cursor":
            # Cursor uses a single cookie-based config, take the last one
            token = _credential_value(item, "session_token", "sessionToken", "wos_session", "legacy_token")
            if token:
                cursor_config = {"cookie": f"WorkosCursorSessionToken={token}"}
        elif item.platform == "blink":
            refresh = _credential_value(item, "firebase_refresh_token", "refresh_token", "refreshToken")
            id_token = _credential_value(item, "id_token", "idToken")
            session = _credential_value(item, "session_token", "sessionToken")
            slug = _credential_value(item, "workspace_slug", "workspaceSlug")
            if refresh or id_token:
                blink_config = {
                    "refreshToken": refresh,
                    "idToken": id_token,
                    "sessionToken": session,
                    "workspaceSlug": slug,
                }
        elif item.platform == "chatgpt":
            token = _credential_value(item, "access_token", "accessToken", "legacy_token")
            if token:
                chatgpt_config = {"token": token}

    providers = {}
    if kiro_accounts:
        providers["kiroAccounts"] = kiro_accounts
    if grok_tokens:
        providers["grokTokens"] = grok_tokens
    if cursor_config:
        providers["cursorConfig"] = cursor_config
    if blink_config:
        providers["blinkConfig"] = blink_config
    if chatgpt_config:
        providers["chatgptConfig"] = chatgpt_config

    return {
        "settings": {
            "adminPassword": "changeme",
            "apiKey": "0000",
            "defaultProvider": "kiro" if kiro_accounts else "cursor",
        },
        "providers": providers,
    }


class AccountExportsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def export_chatgpt_json(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        payloads = _limit_export_units(
            [payload for item in items for payload in _chatgpt_workspace_export_payloads(item)],
            selection.limit,
        )
        exported_units = _payload_export_units(payloads)
        content = json.dumps(
            [
                {
                    "email": payload["email"],
                    "password": payload["password"],
                    "client_id": payload["client_id"],
                    "account_id": payload["account_id"],
                    "workspace_id": payload["workspace_id"],
                    "token_plan_type": payload["token_plan_type"],
                    "access_token": payload["access_token"],
                    "refresh_token": payload["refresh_token"],
                    "id_token": payload["id_token"],
                    "session_token": payload["session_token"],
                    "email_service": payload["email_service"],
                    "registered_at": payload["registered_at"],
                    "last_refresh": payload["last_refresh"],
                    "expires_at": payload["expires_at"],
                    "status": payload["status"],
                }
                for payload in payloads
            ],
            ensure_ascii=False,
            indent=2,
        )
        return ExportArtifact(
            filename=_timestamp_name("accounts", "json"),
            media_type="application/json",
            content=content,
            account_ids=_exported_account_ids_from_units(exported_units),
            exported_units=exported_units,
        )

    def export_chatgpt_csv(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        payloads = _limit_export_units(
            [payload for item in items for payload in _chatgpt_workspace_export_payloads(item)],
            selection.limit,
        )
        exported_units = _payload_export_units(payloads)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "ID",
                "Email",
                "Password",
                "Client ID",
                "Account ID",
                "Workspace ID",
                "Token Plan Type",
                "Access Token",
                "Refresh Token",
                "ID Token",
                "Session Token",
                "Email Service",
                "Status",
                "Registered At",
                "Last Refresh",
                "Expires At",
            ]
        )
        for payload in payloads:
            writer.writerow(
                [
                    payload["id"],
                    payload["email"],
                    payload["password"],
                    payload["client_id"],
                    payload["account_id"],
                    payload["workspace_id"],
                    payload["token_plan_type"],
                    payload["access_token"],
                    payload["refresh_token"],
                    payload["id_token"],
                    payload["session_token"],
                    payload["email_service"],
                    payload["status"],
                    payload["registered_at"] or "",
                    payload["last_refresh"] or "",
                    payload["expires_at"] or "",
                ]
            )
        return ExportArtifact(
            filename=_timestamp_name("accounts", "csv"),
            media_type="text/csv",
            content=output.getvalue(),
            account_ids=_exported_account_ids_from_units(exported_units),
            exported_units=exported_units,
        )

    def export_chatgpt_sub2api(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        payloads = _limit_export_units(
            [payload for item in items for payload in _chatgpt_workspace_export_payloads(item)],
            selection.limit,
        )
        exported_units = _payload_export_units(payloads)
        content = json.dumps(_sub2api_from_payloads(payloads), ensure_ascii=False, indent=2)
        return ExportArtifact(
            filename=_timestamp_name("sub2api_tokens", "json"),
            media_type="application/json",
            content=content,
            account_ids=_exported_account_ids_from_units(exported_units),
            exported_units=exported_units,
        )

    def export_chatgpt_agent_identity_sub2api(
        self,
        selection: AccountExportSelection,
    ) -> ExportArtifact:
        items = _limit_export_units(self._load_chatgpt_items(selection), selection.limit)
        payloads = [
            (item, _make_agent_identity_sub2api_json(item))
            for item in items
        ]
        account_ids = _account_ids([item for item, _payload in payloads])
        exported_units = [
            {"account_id": int(item.id), "workspace_id": "", "workspace_unit": False}
            for item, _payload in payloads
            if int(item.id or 0) > 0
        ]
        if len(payloads) == 1:
            item, payload = payloads[0]
            return ExportArtifact(
                filename=f"{_safe_export_name(item.email)}_agent_identity_sub2api.json",
                media_type="application/json",
                content=json.dumps(payload, ensure_ascii=False, indent=2),
                account_ids=account_ids,
                exported_units=exported_units,
            )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item, payload in payloads:
                archive.writestr(
                    f"{_safe_export_name(item.email)}_agent_identity_sub2api.json",
                    json.dumps(payload, ensure_ascii=False, indent=2),
                )
        buffer.seek(0)
        return ExportArtifact(
            filename=_timestamp_name("agent_identity_sub2api", "zip"),
            media_type="application/zip",
            content=buffer,
            account_ids=account_ids,
            exported_units=exported_units,
        )

    def export_chatgpt_cpa(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        cpa_tokens: list[tuple[AccountRecord, str, dict, bool]] = []
        for item in items:
            workspace_tokens = _chatgpt_workspace_cpa_jsons(item)
            if workspace_tokens:
                cpa_tokens.extend((item, workspace_id, token, workspace_unit) for workspace_id, token, workspace_unit in workspace_tokens)
            else:
                cpa_tokens.append((item, "", _generate_cpa_token_json(item), False))
        cpa_tokens = _limit_export_units(cpa_tokens, selection.limit)
        exported_units = [
            {"account_id": int(item.id), "workspace_id": workspace_id, "workspace_unit": workspace_unit}
            for item, workspace_id, _token, workspace_unit in cpa_tokens
            if int(item.id or 0) > 0
        ]

        if len(cpa_tokens) == 1:
            item, _workspace_id, token, _workspace_unit = cpa_tokens[0]
            content = json.dumps(token, ensure_ascii=False, indent=2)
            return ExportArtifact(
                filename=f"{item.email}.json",
                media_type="application/json",
                content=content,
                account_ids=_exported_account_ids_from_units(exported_units),
                exported_units=exported_units,
            )

        # Keep multi-account CPA exports as one JSON document.  The previous
        # ZIP format was inconvenient for panels that accept a single import
        # file, and it also made it harder to inspect which token belonged to
        # which account/workspace.  A JSON array preserves every token object
        # without changing the single-account export shape.
        content = json.dumps(
            [token for _item, _workspace_id, token, _workspace_unit in cpa_tokens],
            ensure_ascii=False,
            indent=2,
        )
        return ExportArtifact(
            filename=_timestamp_name("cpa_tokens", "json"),
            media_type="application/json",
            content=content,
            account_ids=_exported_account_ids_from_units(exported_units),
            exported_units=exported_units,
        )

    def export_chatgpt_compact_auto(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        cpa_tokens: list[tuple[AccountRecord, str, dict, bool]] = []
        for item in items:
            workspace_tokens = _chatgpt_workspace_cpa_jsons(item)
            if workspace_tokens:
                cpa_tokens.extend(
                    (
                        item,
                        workspace_id,
                        _compact_auto_token_json(token, email=item.email, workspace_id=workspace_id),
                        workspace_unit,
                    )
                    for workspace_id, token, workspace_unit in workspace_tokens
                )
            else:
                token = _compact_auto_token_json(_generate_cpa_token_json(item), email=item.email)
                cpa_tokens.append((item, "", token, False))
        cpa_tokens = _limit_export_units(cpa_tokens, selection.limit)
        exported_units = [
            {"account_id": int(item.id), "workspace_id": workspace_id, "workspace_unit": workspace_unit}
            for item, workspace_id, _token, workspace_unit in cpa_tokens
            if int(item.id or 0) > 0
        ]

        if len(cpa_tokens) == 1:
            item, workspace_id, token, _workspace_unit = cpa_tokens[0]
            suffix = f"_{_safe_export_name(str(workspace_id))}" if workspace_id else ""
            return ExportArtifact(
                filename=f"{_safe_export_name(item.email)}{suffix}_compact_auto.json",
                media_type="application/json",
                content=json.dumps(token, ensure_ascii=False, indent=2),
                account_ids=_exported_account_ids_from_units(exported_units),
                exported_units=exported_units,
            )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            used_names: set[str] = set()
            for item, workspace_id, token, _workspace_unit in cpa_tokens:
                suffix = f"_{_safe_export_name(str(workspace_id))}" if workspace_id else ""
                filename = f"{_safe_export_name(item.email)}{suffix}_compact_auto.json"
                if filename in used_names:
                    filename = f"{_safe_export_name(item.email)}{suffix}_{len(used_names) + 1}_compact_auto.json"
                used_names.add(filename)
                archive.writestr(filename, json.dumps(token, ensure_ascii=False, indent=2))
        buffer.seek(0)
        return ExportArtifact(
            filename=_timestamp_name("compact_auto_tokens", "zip"),
            media_type="application/zip",
            content=buffer,
            account_ids=_exported_account_ids_from_units(exported_units),
            exported_units=exported_units,
        )

    def export_chatgpt_email_api_txt(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        # Provider-specific mailbox endpoints and access keys are runtime
        # secrets. The public build exports only the account identifier.
        lines = [item.email for item in items]
        return ExportArtifact(
            filename=_timestamp_name("chatgpt_email_api", "txt"),
            media_type="text/plain",
            content="\n".join(lines),
            account_ids=_account_ids(items),
        )

    def export_chatgpt_cockpit(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        payload: dict | list[dict]
        if len(items) == 1:
            payload = _make_cockpit_token(items[0])
        else:
            payload = [_make_cockpit_token(item) for item in items]
        return ExportArtifact(
            filename=_timestamp_name("cockpit_tokens", "json"),
            media_type="application/json",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            account_ids=_account_ids(items),
        )

    def _load_chatgpt_items(self, selection: AccountExportSelection) -> list[AccountRecord]:
        selection.platform = selection.platform or CHATGPT_PLATFORM
        if selection.platform != CHATGPT_PLATFORM:
            raise ValueError("仅支持 ChatGPT 账号导出")
        return self.repository.select_for_export(selection)

    # ------------------------------------------------------------------
    # Kiro → Kiro-Go CLI Proxy export
    # ------------------------------------------------------------------

    def export_kiro_go(self, selection: AccountExportSelection) -> ExportArtifact:
        """导出 Kiro 账号为 Kiro-Go CLI Proxy 兼容的 config.json 格式。"""
        selection.platform = "kiro"
        items = self.repository.select_for_export(selection)
        accounts = [_make_kiro_go_account(item) for item in items]
        config = {
            "password": "changeme",
            "port": 8080,
            "host": "0.0.0.0",
            "requireApiKey": False,
            "accounts": accounts,
        }
        content = json.dumps(config, ensure_ascii=False, indent=2)
        return ExportArtifact(
            filename=_timestamp_name("kiro_go_config", "json"),
            media_type="application/json",
            content=content,
            account_ids=_account_ids(items),
        )

    def export_any2api(self, selection: AccountExportSelection) -> ExportArtifact:
        """导出账号为 Any2API admin.json 兼容格式。

        支持多平台：Kiro → kiroAccounts, Grok → grokTokens, Cursor/Blink/ChatGPT → 对应 config。
        """
        items = self.repository.select_for_export(selection)
        admin_config = _build_any2api_admin_config(items)
        content = json.dumps(admin_config, ensure_ascii=False, indent=2)
        return ExportArtifact(
            filename=_timestamp_name("any2api_admin", "json"),
            media_type="application/json",
            content=content,
            account_ids=_account_ids(items),
        )
