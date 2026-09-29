from __future__ import annotations

from core.vault import MASKED_SECRET
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
from infrastructure.provider_settings_repository import ProviderSettingsRepository


class ProviderSettingsService:
    def __init__(self, repository: ProviderSettingsRepository | None = None):
        self.repository = repository or ProviderSettingsRepository()
        self.definitions = ProviderDefinitionsRepository()

    def list_settings(self, provider_type: str) -> list[dict]:
        items = self.repository.list_by_type(provider_type)
        return [self._serialize(item) for item in items]

    def save_setting(self, payload: dict) -> dict:
        item = self.repository.save(
            setting_id=payload.get("id"),
            provider_type=str(payload.get("provider_type") or ""),
            provider_key=str(payload.get("provider_key") or ""),
            display_name=str(payload.get("display_name") or ""),
            auth_mode=str(payload.get("auth_mode") or ""),
            enabled=bool(payload.get("enabled", True)),
            is_default=bool(payload.get("is_default", False)),
            config=dict(payload.get("config") or {}),
            auth=dict(payload.get("auth") or {}),
            metadata=dict(payload.get("metadata") or {}),
        )
        return {"ok": True, "item": self._serialize(item)}

    def delete_setting(self, setting_id: int) -> dict:
        return {"ok": self.repository.delete(setting_id)}

    def get_catalog_options(self) -> dict:
        return {
            "mailbox_settings": self.list_settings("mailbox"),
            "captcha_settings": self.list_settings("captcha"),
            "sms_settings": self.list_settings("sms"),
            "captcha_policy": self.get_captcha_policy(),
        }

    def get_captcha_policy(self) -> dict:
        browser_default = self.repository.get_default_provider_key("captcha")
        protocol_order = self.repository.get_enabled_captcha_order()
        return {
            "protocol_mode": "auto_first_enabled_remote",
            "protocol_order": protocol_order,
            "browser_mode": browser_default,
        }

    def _serialize(self, item) -> dict:
        definition = self.definitions.get_by_key(item.provider_type, item.provider_key)
        raw_auth = item.get_auth() or {}
        # Never send credential material to the browser. The mask also lets the
        # client preserve configured fields while the repository merges updates.
        auth = {
            str(key): MASKED_SECRET if str(value or "") else ""
            for key, value in raw_auth.items()
        }
        auth_modes = definition.get_auth_modes() if definition else []
        fields = definition.get_fields() if definition else []
        return {
            "id": int(item.id or 0),
            "provider_type": item.provider_type,
            "provider_key": item.provider_key,
            "display_name": item.display_name,
            "catalog_label": definition.label if definition else item.provider_key,
            "description": definition.description if definition else "",
            "driver_type": definition.driver_type if definition else "",
            "auth_mode": item.auth_mode,
            "auth_modes": auth_modes,
            "enabled": bool(item.enabled),
            "is_default": bool(item.is_default),
            "is_builtin": bool(getattr(definition, "is_builtin", False)) if definition else False,
            "category": str(getattr(definition, "category", "") or "") if definition else "",
            "fields": fields,
            "config": item.get_config(),
            "auth": auth,
            "auth_preview": dict(auth),
            "metadata": item.get_metadata(),
        }
