from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.provider_settings import ProviderSettingsService
from core.vault import MASKED_SECRET
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
from infrastructure.provider_settings_repository import ProviderSettingsRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/provider-settings", tags=["provider-settings"])
service = ProviderSettingsService()


class ProviderSettingUpsertRequest(BaseModel):
    id: int | None = None
    provider_type: str
    provider_key: str
    display_name: str = ""
    auth_mode: str = ""
    enabled: bool = True
    is_default: bool = False
    config: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


@router.get("")
def list_provider_settings(provider_type: str):
    return service.list_settings(provider_type)


@router.put("")
def save_provider_setting(body: ProviderSettingUpsertRequest):
    try:
        return service.save_setting(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("")
def create_provider_setting(body: ProviderSettingUpsertRequest):
    try:
        return service.save_setting(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/{setting_id}")
def delete_provider_setting(setting_id: int):
    result = service.delete_setting(setting_id)
    if not result["ok"]:
        raise HTTPException(404, "provider setting 不存在")
    return result


class ProviderTestRequest(BaseModel):
    provider_type: str
    provider_key: str
    config: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, str] = Field(default_factory=dict)


@router.post("/test")
def test_provider(body: ProviderTestRequest):
    """测试 provider 配置；掩码/空认证字段回退到已保存密钥。"""
    definitions = ProviderDefinitionsRepository()
    definition = definitions.get_by_key(body.provider_type, body.provider_key)
    if not definition:
        return {"ok": False, "error": f"未找到 provider 定义: {body.provider_key}"}

    repository = ProviderSettingsRepository(definitions)
    extra = repository.resolve_runtime_settings(body.provider_type, body.provider_key)
    extra.update({str(key): value for key, value in body.config.items()})
    fields = definition.get_fields()
    known_keys = {
        str(field.get("key") or "")
        for field in fields
        if str(field.get("key") or "").strip()
    }
    secret_keys = {
        str(field.get("key") or "")
        for field in fields
        if str(field.get("key") or "").strip() and bool(field.get("secret"))
    }
    for key, value in body.auth.items():
        normalized_key = str(key)
        text = str(value or "")
        is_secret = normalized_key in secret_keys or normalized_key not in known_keys
        if is_secret:
            if text.strip() and text != MASKED_SECRET:
                extra[normalized_key] = value
        else:
            extra[normalized_key] = value

    if body.provider_type == "mailbox":
        return _test_mailbox(definition.driver_type or body.provider_key, extra, definition)
    if body.provider_type == "captcha":
        return {"ok": True, "message": "验证码服务暂不支持在线测试，请在注册任务中验证"}
    if body.provider_type == "sms":
        return {"ok": True, "message": "接码服务暂不支持在线测试，请在注册任务中验证"}
    return {"ok": False, "error": f"不支持测试的 provider 类型: {body.provider_type}"}


def _test_mailbox(driver_type: str, extra: dict, definition) -> dict:
    """尝试用给定配置创建一个邮箱，验证配置是否正确。"""
    from core.base_mailbox import MAILBOX_FACTORY_REGISTRY

    factory = MAILBOX_FACTORY_REGISTRY.get(driver_type)
    if not factory:
        return {"ok": False, "error": f"未找到邮箱驱动: {driver_type}"}

    try:
        if driver_type in ("generic_http_mailbox", "generic_http"):
            pipeline_config = definition.get_metadata() if definition else {}
            mailbox = factory(extra, None, pipeline_config=pipeline_config)
        else:
            mailbox = factory(extra, None)

        if hasattr(mailbox, "peek_email"):
            email = mailbox.peek_email()
            return {"ok": True, "message": f"测试成功！可用邮箱: {email}", "email": email}

        account = mailbox.get_email()
        return {"ok": True, "message": f"测试成功！生成邮箱: {account.email}", "email": account.email}
    except Exception:
        # Exception text and traceback may contain credentials or signed URLs.
        logger.exception("provider test failed: type=%s driver=%s", definition.provider_type, driver_type)
        return {"ok": False, "error": "测试失败，请检查配置或服务日志"}
