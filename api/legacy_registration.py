from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.legacy_registration import create_legacy_register_task
from application.provider_definitions import ProviderDefinitionsService
from core.api_mailbox import parse_api_mailbox_rows
from core.db import ProxyModel, engine
from core.mihomo_client import MihomoError, mihomo_client
from core.mihomo_config import mihomo_config_manager
from infrastructure.microsoft_mailbox_repository import MicrosoftMailboxRepository
from infrastructure.provider_settings_repository import ProviderSettingsRepository
from core.registry import list_legacy_registration_platforms
from services.task_runtime import task_runtime
from sqlmodel import Session, func, select


router = APIRouter(prefix="/legacy-registration", tags=["legacy-registration"])


class LegacyRegisterRequest(BaseModel):
    platform: str
    count: int = Field(default=1, ge=1, le=100)
    concurrency: int = Field(default=1, ge=1, le=20)
    executor_type: str = "protocol"
    identity_provider: str = "mailbox"
    oauth_provider: str = ""
    oauth_email_hint: str = ""
    mail_provider: str = ""
    email: str = ""
    password: str = ""
    proxy: str = ""
    proxy_strategy: str = "auto"
    captcha_solver: str = "auto"
    failure_policy: str = "retry_then_continue"
    max_attempts_per_account: int = Field(default=2, ge=1, le=5)
    verify_after_registration: bool = True
    chrome_user_data_dir: str = ""
    chrome_cdp_url: str = ""
    otp_timeout: int = Field(default=300, ge=10, le=900)
    sms_provider: str = ""
    sms_country: str = ""
    sms_countries: str = ""
    sms_service: str = ""
    sms_max_price: str = ""
    sms_bulk_price_cny: str = ""
    smsbower_provider_ids_by_country: dict[str, list[str]] = Field(default_factory=dict)
    smsbower_auto_country_min_stock: int = Field(default=1, ge=0, le=1000000)
    sms_usd_cny_rate: float = Field(default=7.2, gt=0, le=20)
    smsbower_provider_reject_threshold: int = Field(default=2, ge=1, le=10)
    sms_code_timeout_seconds: int = Field(default=180, ge=60, le=600)
    sms_phone_max_attempts: int = Field(default=8, ge=1, le=20)
    sms_no_numbers_wait_seconds: int = Field(default=120, ge=0, le=600)
    sms_tier_cooldown_minutes: int = Field(default=45, ge=0, le=60)
    extra: dict = Field(default_factory=dict)


_REQUIRED_MAILBOX_FIELDS = {
    "api_mailbox": ("api_mailbox_pool_text",),
    "domain_imap_catchall": (
        "domain_imap_domain",
        "domain_imap_host",
        "domain_imap_username",
        "domain_imap_password",
    ),
    "domain_inbucket": ("inbucket_domain", "inbucket_api_url"),
}


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_mailbox_options() -> list[dict]:
    definitions = ProviderDefinitionsService().list_definitions("mailbox", enabled_only=True)
    settings_repo = ProviderSettingsRepository()
    ms_stats = MicrosoftMailboxRepository().stats()
    result: list[dict] = []
    for definition in definitions:
        provider_key = str(definition.get("provider_key") or "")
        setting = settings_repo.get_by_key("mailbox", provider_key)
        runtime = settings_repo.resolve_runtime_settings("mailbox", provider_key)
        missing = [key for key in _REQUIRED_MAILBOX_FIELDS.get(provider_key, ()) if not str(runtime.get(key) or "").strip()]
        total_count = None
        available_count = None
        if provider_key == "local_ms_pool":
            total_count = int(ms_stats.get("total") or 0)
            available_count = int(ms_stats.get("remaining") or 0)
            configured = available_count > 0
            status_message = f"剩余 {available_count} / 容量 {int(ms_stats.get('capacity') or 0)}"
        elif provider_key == "api_mailbox":
            try:
                total_count = len(parse_api_mailbox_rows(str(runtime.get("api_mailbox_pool_text") or "")))
            except ValueError:
                total_count = 0
                missing.append("api_mailbox_pool_text")
            allow_reuse = _truthy(runtime.get("api_mailbox_allow_reuse"))
            available_count = total_count if allow_reuse else total_count
            configured = total_count > 0 and not missing
            status_message = f"已配置 {total_count} 个邮箱" if configured else "邮箱 API 池未配置"
        else:
            configured = bool(setting and setting.enabled and not missing)
            status_message = "配置完整" if configured else "缺少必要配置"
        result.append({
            "provider_key": provider_key,
            "display_name": (setting.display_name if setting else "") or definition.get("label") or provider_key,
            "catalog_label": definition.get("label") or provider_key,
            "description": definition.get("description") or "",
            "enabled": bool(setting.enabled) if setting else provider_key == "local_ms_pool",
            "is_default": bool(setting.is_default) if setting else False,
            "configured": bool(configured),
            "missing_fields": sorted(set(missing)),
            "total_count": total_count,
            "available_count": available_count,
            "status_message": status_message,
        })
    return result


def _safe_captcha_options() -> list[dict]:
    definitions = ProviderDefinitionsService().list_definitions("captcha", enabled_only=True)
    settings_repo = ProviderSettingsRepository()
    result: list[dict] = []
    for definition in definitions:
        provider_key = str(definition.get("provider_key") or "")
        setting = settings_repo.get_by_key("captcha", provider_key)
        runtime = settings_repo.resolve_runtime_settings("captcha", provider_key)
        fields = list(definition.get("fields") or [])
        required = [str(item.get("key") or "") for item in fields if item.get("secret")]
        missing = [key for key in required if key and not str(runtime.get(key) or "").strip()]
        configured = provider_key == "manual" or bool(setting and setting.enabled and not missing)
        result.append({
            "provider_key": provider_key,
            "display_name": (setting.display_name if setting else "") or definition.get("label") or provider_key,
            "catalog_label": definition.get("label") or provider_key,
            "description": definition.get("description") or "",
            "enabled": bool(setting.enabled) if setting else provider_key == "manual",
            "is_default": bool(setting.is_default) if setting else False,
            "configured": configured,
            "missing_fields": missing,
            "status_message": "配置完整" if configured else "缺少 API Key 或未启用",
        })
    return result


def _safe_sms_options() -> list[dict]:
    definitions = ProviderDefinitionsService().list_definitions("sms", enabled_only=True)
    settings_repo = ProviderSettingsRepository()
    result: list[dict] = []
    for definition in definitions:
        provider_key = str(definition.get("provider_key") or "")
        setting = settings_repo.get_by_key("sms", provider_key)
        runtime = settings_repo.resolve_runtime_settings("sms", provider_key)
        fields = list(definition.get("fields") or [])
        required = [str(item.get("key") or "") for item in fields if item.get("secret")]
        missing = [key for key in required if key and not str(runtime.get(key) or "").strip()]
        configured = bool(setting and setting.enabled and not missing)
        result.append({
            "provider_key": provider_key,
            "display_name": (setting.display_name if setting else "") or definition.get("label") or provider_key,
            "catalog_label": definition.get("label") or provider_key,
            "description": definition.get("description") or "",
            "enabled": bool(setting.enabled) if setting else False,
            "is_default": bool(setting.is_default) if setting else False,
            "configured": configured,
            "missing_fields": missing,
            "status_message": "配置完整" if configured else "缺少 API Key 或未启用",
        })
    return result


def _proxy_status() -> dict:
    with Session(engine) as session:
        manual_total = int(session.exec(select(func.count()).select_from(ProxyModel)).one() or 0)
        manual_active = int(session.exec(
            select(func.count()).select_from(ProxyModel).where(ProxyModel.is_active == True)  # noqa: E712
        ).one() or 0)
    configured = mihomo_config_manager.config_path.is_file()
    mihomo = {
        "configured": configured,
        "available": False,
        "nodes": 0,
        "error": "" if configured else "尚未保存 Mihomo 订阅配置",
    }
    if not configured:
        return {
            "manual_pool": {"total": manual_total, "active": manual_active, "available": manual_active > 0},
            "mihomo": mihomo,
        }
    try:
        info = mihomo_client.list_nodes(refresh=False)
        nodes = [item for item in info.get("nodes", []) if item.get("alive") is not False and item.get("enabled", True)]
        mihomo.update({"configured": True, "available": bool(nodes), "nodes": len(nodes)})
    except MihomoError as exc:
        mihomo["error"] = str(exc)
    except Exception as exc:
        mihomo["error"] = str(exc)
    return {
        "manual_pool": {"total": manual_total, "active": manual_active, "available": manual_active > 0},
        "mihomo": mihomo,
    }


@router.get("/options")
def legacy_registration_options():
    return {
        "registration_engine": "legacy_isolated",
        "platforms": list_legacy_registration_platforms(),
        "mailbox_settings": _safe_mailbox_options(),
        "captcha_settings": _safe_captcha_options(),
        "sms_settings": _safe_sms_options(),
        "proxy_status": _proxy_status(),
    }


@router.post("/tasks")
def create_legacy_registration(body: LegacyRegisterRequest):
    extra = dict(body.extra or {})
    extra.update({
        "identity_provider": body.identity_provider,
        "oauth_provider": body.oauth_provider,
        "oauth_email_hint": body.oauth_email_hint,
        "mail_provider": body.mail_provider,
        "proxy_strategy": body.proxy_strategy,
        "failure_policy": body.failure_policy,
        "max_attempts_per_account": body.max_attempts_per_account,
        "verify_after_registration": body.verify_after_registration,
        "chrome_user_data_dir": body.chrome_user_data_dir,
        "chrome_cdp_url": body.chrome_cdp_url,
        "otp_timeout": body.otp_timeout,
        "sms_provider": body.sms_provider,
        "sms_country": body.sms_country,
        "sms_countries": body.sms_countries,
        "sms_service": body.sms_service,
        "sms_max_price": body.sms_max_price,
        "sms_bulk_price_cny": body.sms_bulk_price_cny,
        "smsbower_provider_ids_by_country": body.smsbower_provider_ids_by_country,
        "smsbower_auto_country_min_stock": body.smsbower_auto_country_min_stock,
        "sms_usd_cny_rate": body.sms_usd_cny_rate,
        "smsbower_provider_reject_threshold": body.smsbower_provider_reject_threshold,
        "sms_code_timeout_seconds": body.sms_code_timeout_seconds,
        "sms_phone_max_attempts": body.sms_phone_max_attempts,
        "sms_no_numbers_wait_seconds": body.sms_no_numbers_wait_seconds,
        "sms_tier_cooldown_minutes": body.sms_tier_cooldown_minutes,
    })
    if body.platform.strip().lower() == "chatgpt_free":
        provider_key = str(body.sms_provider or "").strip().lower()
        provider_key = {
            "herosms": "herosms_api",
            "sms_activate": "sms_activate_api",
            "smsbower": "smsbower_api",
        }.get(provider_key, provider_key)
        if not provider_key:
            provider_key = ProviderSettingsRepository().get_default_provider_key("sms")
        setting = ProviderSettingsRepository().get_by_key("sms", provider_key) if provider_key else None
        runtime = ProviderSettingsRepository().resolve_runtime_settings("sms", provider_key) if provider_key else {}
        key_field = {
            "herosms_api": "herosms_api_key",
            "sms_activate_api": "sms_activate_api_key",
            "smsbower_api": "smsbower_api_key",
        }.get(provider_key)
        if not setting or not setting.enabled or (key_field and not str(runtime.get(key_field) or "").strip()):
            raise HTTPException(400, "ChatGPT Free 需要先在设置中配置并启用短信接码 Provider")
        extra["sms_provider"] = provider_key
        if provider_key == "smsbower_api":
            effective_max_price = str(body.sms_max_price or "0.13").strip() or "0.13"
            countries = list(dict.fromkeys(
                item.strip()
                for item in re.split(r"[\s,;]+", str(body.sms_countries or body.sms_country or ""))
                if item.strip()
            ))
            if not countries:
                countries = [str(runtime.get("sms_country") or runtime.get("smsbower_default_country") or "187").strip()]
            if "12" in countries:
                raise HTTPException(400, "SMSBower 虚拟/VOIP 国家不能用于 ChatGPT 手机号注册")
            try:
                max_price = float(effective_max_price)
            except ValueError as exc:
                raise HTTPException(400, "SMSBower 单号最高价格必须是数字") from exc
            if max_price < 0:
                raise HTTPException(400, "SMSBower 单号最高价格不能小于 0")
            provider_ids_by_country = {
                country: list(dict.fromkeys(
                    str(provider_id or "").strip()
                    for provider_id in body.smsbower_provider_ids_by_country.get(country, [])
                    if str(provider_id or "").strip()
                ))[:200]
                for country in countries
            }
            provider_ids_by_country = {
                country: provider_ids
                for country, provider_ids in provider_ids_by_country.items()
                if provider_ids
            }
            extra.update({
                "sms_country": countries[0],
                "sms_countries": ",".join(countries),
                "sms_service": str(body.sms_service or "dr").strip() or "dr",
                "sms_max_price": effective_max_price,
                "smsbower_max_price": effective_max_price,
                "smsbower_provider_ids_by_country": provider_ids_by_country,
                "smsbower_allow_virtual": False,
                "smsbower_auto_country": False,
                "smsbower_auto_country_max_price": max_price,
            })
            if len(countries) == 1 and provider_ids_by_country.get(countries[0]):
                extra["smsbower_provider_ids"] = ",".join(provider_ids_by_country[countries[0]])
            else:
                extra.pop("smsbower_provider_ids", None)
    try:
        task = create_legacy_register_task({
            "platform": body.platform,
            "count": body.count,
            "concurrency": body.concurrency,
            "executor_type": body.executor_type,
            "captcha_solver": body.captcha_solver,
            "email": body.email or None,
            "password": body.password or None,
            "proxy": body.proxy or None,
            "extra": extra,
        })
    except (KeyError, NotImplementedError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    task_runtime.wake_up()
    return task
