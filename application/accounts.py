from __future__ import annotations

import ast
import csv
import json
import re
from typing import Any

from core.datetime_utils import serialize_datetime
from domain.accounts import (
    AccountCreateCommand,
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountStats,
    AccountUpdateCommand,
)
from infrastructure.accounts_repository import AccountsRepository


IMPORT_LINE_RE = re.compile(
    r'^\s*(?P<email>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'\s+(?P<password>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'(?:\s+(?P<extra>.*))?\s*$'
)


def _decode_import_token(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(text)
            return decoded if isinstance(decoded, str) else str(decoded)
        except Exception:
            return text[1:-1]
    return text


def _parse_csv_row(raw: str) -> list[str]:
    return next(csv.reader([raw]))


_LIST_SECRET_FIELDS = {
    "access_token",
    "admin_token",
    "api_key",
    "apikey",
    "auth_cookies",
    "authorization",
    "callback_url",
    "client_secret",
    "cookie",
    "cookies",
    "id_token",
    "legacy_token",
    "mailbox_url",
    "password",
    "primary_token",
    "proxy",
    "proxy_url",
    "auth_proxy_url",
    "recovery_password",
    "refresh_token",
    "session_token",
    "totp_secret",
}


def _redact_list_secret_tree(value: Any, *, field: str = "") -> Any:
    """Remove runtime secrets from the frequently-polled account list payload."""
    normalized_field = str(field or "").strip().lower()
    if normalized_field in _LIST_SECRET_FIELDS:
        return ""
    if isinstance(value, dict):
        return {
            str(key): _redact_list_secret_tree(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_list_secret_tree(item) for item in value]
    return value


def _redact_list_credential(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item or {})
    has_value = payload.get("value") not in (None, "")
    payload["value"] = ""
    payload["preview"] = "••••••••" if has_value else ""
    payload["has_value"] = bool(has_value)
    payload["metadata"] = _redact_list_secret_tree(payload.get("metadata") or {})
    return payload


def _redact_list_provider_account(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item or {})
    credentials = dict(payload.get("credentials") or {})
    payload["credentials"] = {}
    payload["credential_keys"] = sorted(str(key) for key in credentials)
    payload["credential_previews"] = {
        str(key): "••••••••" for key, secret in credentials.items() if secret not in (None, "")
    }
    payload["metadata"] = _redact_list_secret_tree(payload.get("metadata") or {})
    return payload


class AccountsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def list_accounts(self, query: AccountQuery) -> dict:
        total, items = self.repository.list(query)
        return {
            "total": total,
            "page": query.page,
            "items": [self._serialize(item, include_secrets=False) for item in items],
        }

    def get_account(self, account_id: int) -> dict | None:
        item = self.repository.get(account_id)
        return self._serialize(item) if item else None

    def create_account(self, command: AccountCreateCommand) -> dict:
        return self._serialize(self.repository.create(command))

    def update_account(self, account_id: int, command: AccountUpdateCommand) -> dict | None:
        requested_lifecycle = str(command.lifecycle_status or "").strip().lower()
        if requested_lifecycle in {"registered", "trial", "subscribed"}:
            current = self.repository.get(account_id)
            pipeline = (
                dict((current.overview or {}).get("registration_pipeline") or {})
                if current
                else {}
            )
            registration_status = str(pipeline.get("registration_status") or "").strip().lower()
            if pipeline and registration_status != "registered":
                current_stage = str(pipeline.get("current_stage") or "registration")
                raise ValueError(
                    f"注册流水线尚未完成（当前阶段: {current_stage}），"
                    "不能手动改成可交付状态"
                )
        item = self.repository.update(account_id, command)
        return self._serialize(item) if item else None

    def delete_account(self, account_id: int) -> dict:
        return {"ok": self.repository.delete(account_id)}

    def import_accounts(self, platform: str, lines: list[str]) -> dict:
        parsed: list[AccountImportLine] = []
        csv_header: list[str] | None = None
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            if csv_header is None and "," in raw:
                try:
                    header_candidate = [item.strip().lower() for item in _parse_csv_row(raw)]
                except Exception:
                    header_candidate = []
                if "email" in header_candidate and "password" in header_candidate:
                    csv_header = header_candidate
                    continue
            if csv_header is not None:
                try:
                    values = _parse_csv_row(raw)
                except Exception:
                    values = []
                if values:
                    row = {
                        csv_header[index]: values[index]
                        for index in range(min(len(csv_header), len(values)))
                    }
                    email = str(row.get("email", "") or "").strip()
                    password = str(row.get("password", "") or "")
                    if email and password and "@" in email and " " not in email:
                        extra = {}
                        cashier_url = str(row.get("cashier_url", "") or "").strip()
                        if cashier_url:
                            extra["cashier_url"] = cashier_url
                        parsed.append(AccountImportLine(email=email, password=password, extra=extra))
                        continue
            match = IMPORT_LINE_RE.match(raw)
            if not match:
                continue
            email = _decode_import_token(match.group("email"))
            password = _decode_import_token(match.group("password"))
            extra = {}
            payload = (match.group("extra") or "").strip()
            if payload:
                try:
                    decoded = json.loads(payload)
                    if isinstance(decoded, dict):
                        extra = decoded
                    elif decoded not in (None, ""):
                        extra = {"cashier_url": str(decoded)}
                except Exception:
                    extra = {"cashier_url": _decode_import_token(payload)}
            parsed.append(AccountImportLine(email=email, password=password, extra=extra))
        return {"created": self.repository.import_lines(platform, parsed)}

    def export_csv(self, query: AccountQuery) -> str:
        return self.repository.export_csv(query)

    def get_stats(self) -> dict:
        stats: AccountStats = self.repository.stats()
        return {
            "total": stats.total,
            "by_platform": stats.by_platform,
            "by_status": stats.by_status,
            "by_lifecycle_status": stats.by_lifecycle_status,
            "by_plan_state": stats.by_plan_state,
            "by_validity_status": stats.by_validity_status,
            "by_display_status": stats.by_display_status,
        }

    @staticmethod
    def _serialize(item: AccountRecord, *, include_secrets: bool = True) -> dict:
        credentials = list(item.credentials or [])
        provider_accounts = list(item.provider_accounts or [])
        overview = dict(item.overview or {})
        password = item.password
        primary_token = item.primary_token
        if not include_secrets:
            credentials = [_redact_list_credential(entry) for entry in credentials]
            provider_accounts = [
                _redact_list_provider_account(entry) for entry in provider_accounts
            ]
            overview = _redact_list_secret_tree(overview)
            password = ""
            primary_token = ""
        return {
            "id": item.id,
            "platform": item.platform,
            "email": item.email,
            "password": password,
            "password_present": bool(item.password),
            "user_id": item.user_id,
            "primary_token": primary_token,
            "primary_token_present": bool(item.primary_token),
            "trial_end_time": item.trial_end_time,
            "cashier_url": item.cashier_url,
            "lifecycle_status": item.lifecycle_status,
            "validity_status": item.validity_status,
            "plan_state": item.plan_state,
            "plan_name": item.plan_name,
            "display_status": item.display_status,
            "overview": overview,
            "display_summary": item.display_summary,
            "credentials": credentials,
            "provider_accounts": provider_accounts,
            "provider_resources": item.provider_resources,
            "created_at": serialize_datetime(item.created_at),
            "updated_at": serialize_datetime(item.updated_at),
        }
