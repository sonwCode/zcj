from __future__ import annotations

from core.config_store import config_store
from core.vault import MASKED_SECRET
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class ConfigRepository:
    SECRET_KEYS = {
        "cpa_api_key", "team_manager_key", "any2api_password",
        "sub2api_admin_password",
    }

    BASE_KEYS = {
        "default_executor",
        "default_identity_provider", "default_oauth_provider", "oauth_email_hint",
        "chrome_user_data_dir", "chrome_cdp_url",
        "cpa_api_url", "cpa_api_key",
        "team_manager_url", "team_manager_key",
        "any2api_url", "any2api_password",
        "sub2api_url", "sub2api_admin_email", "sub2api_admin_password",
        "sub2api_auto_sync", "sub2api_auto_delete_invalid",
        "sub2api_check_interval_minutes", "sub2api_agent_identity_region",
        "sub2api_proxy_id", "sub2api_group_id", "sub2api_group_name",
        "sub2api_default_model",
    }

    def __init__(self, definitions: ProviderDefinitionsRepository | None = None):
        self.definitions = definitions or ProviderDefinitionsRepository()

    def get_allowed_keys(self) -> set[str]:
        keys = set(self.BASE_KEYS)
        for provider_type in ("mailbox", "captcha", "sms"):
            for definition in self.definitions.list_by_type(provider_type, enabled_only=False):
                for field in definition.get_fields():
                    field_key = str(field.get("key") or "").strip()
                    if field_key:
                        keys.add(field_key)
        return keys

    def get_flat(self, *, masked: bool = True) -> dict[str, str]:
        data = config_store.get_all()
        allowed = self.get_allowed_keys()
        result: dict[str, str] = {}
        for key, value in data.items():
            if key not in allowed:
                continue
            text = str(value or "")
            result[key] = MASKED_SECRET if masked and key in self.SECRET_KEYS and text else text
        return result

    def update_flat(self, data: dict[str, str]) -> list[str]:
        allowed = self.get_allowed_keys()
        safe: dict[str, str] = {}
        for key, value in data.items():
            if key not in allowed:
                continue
            text = str(value or "")
            # Empty/masked secret fields mean "leave the stored secret alone".
            if key in self.SECRET_KEYS and (not text.strip() or text == MASKED_SECRET):
                continue
            safe[key] = text
        config_store.set_many(safe)
        return list(safe.keys())
