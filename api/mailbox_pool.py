from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from core.local_ms_mailbox import LocalMicrosoftMailboxPool, split_unused_local_ms_pool_rows


router = APIRouter(prefix="/mailbox-pool", tags=["mailbox-pool"])


class MailboxPoolSplitRequest(BaseModel):
    text: str = ""
    state_file: str = ""


class MailboxPoolInventoryRequest(BaseModel):
    provider_key: str = ""
    text: str = ""
    pool_file: str = ""
    state_file: str = ""
    alias_enabled: bool = False
    alias_count: int = 1
    limit: int = 1000


@router.post("/split-unused")
def split_unused_mailboxes(body: MailboxPoolSplitRequest):
    result = split_unused_local_ms_pool_rows(body.text, state_file=body.state_file)
    return {
        "unused_text": result.pool_text,
        "total_count": result.total_count,
        "unused_count": result.unused_count,
        "used_count": result.used_count,
        "blocked_count": result.blocked_count,
        "duplicate_count": result.duplicate_count,
        "invalid_count": result.invalid_count,
    }


@router.post("/inventory")
def inspect_mailbox_pool(body: MailboxPoolInventoryRequest):
    config: dict = {}
    if body.provider_key:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository

        config.update(
            ProviderSettingsRepository().resolve_runtime_settings(
                "mailbox",
                body.provider_key,
                {},
            )
        )
    if body.text:
        config["local_ms_pool_text"] = body.text
    if body.pool_file:
        config["local_ms_pool_file"] = body.pool_file
    if body.state_file:
        config["local_ms_pool_state_file"] = body.state_file
    config["mailbox_alias_enabled"] = body.alias_enabled
    config["mailbox_alias_count"] = max(min(int(body.alias_count or 1), 1000), 1)
    config["local_ms_pool_allow_reuse"] = False
    config["_provider_key"] = body.provider_key or "inline_api_pool"
    pool = LocalMicrosoftMailboxPool.from_config(config)
    return pool.inventory(limit=max(min(int(body.limit or 1000), 5000), 1))
