from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.base_sms import HERO_SMS_DEFAULT_COUNTRY, HERO_SMS_DEFAULT_SERVICE, HeroSmsProvider, SmsBowerProvider
from infrastructure.provider_settings_repository import ProviderSettingsRepository

router = APIRouter(prefix="/sms", tags=["sms"])


class HeroSmsQueryRequest(BaseModel):
    api_key: str = ""
    service: str = ""
    country: str = ""
    proxy: str = ""


def _saved_herosms_config() -> dict:
    repo = ProviderSettingsRepository()
    # 兼容旧版 provider_key "herosms" 和新版 "herosms_api"
    config = repo.resolve_runtime_settings("sms", "herosms_api", {})
    if not config.get("herosms_api_key"):
        config = repo.resolve_runtime_settings("sms", "herosms", {})
    return config


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _public_sms_error(exc: Exception, secret: str = "") -> str:
    message = str(exc or "SMS provider request failed")
    if secret:
        message = message.replace(secret, "***")
    message = re.sub(r"(?i)(api_key=)[^&\s]+", r"\1***", message)
    if "WinError 10013" in message:
        return "SMSBower 网络连接被 Windows 拒绝，请配置接码请求代理或启用可用代理池"
    return message


def _provider_from_payload(payload: HeroSmsQueryRequest | None = None) -> HeroSmsProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_herosms_config()
    api_key = str(payload.api_key or saved.get("herosms_api_key") or "").strip()
    return HeroSmsProvider(
        api_key=api_key,
        default_service=str(payload.service or saved.get("sms_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(payload.country or saved.get("sms_country") or HERO_SMS_DEFAULT_COUNTRY),
        max_price=_safe_float(saved.get("herosms_max_price"), -1),
        proxy=str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "") or None,
    )


@router.get("/herosms/countries")
def herosms_countries():
    try:
        return {"countries": _provider_from_payload().get_countries()}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.get("/herosms/services")
def herosms_services(country: str = ""):
    try:
        return {"services": _provider_from_payload(HeroSmsQueryRequest(country=country)).get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.post("/herosms/balance")
def herosms_balance(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _provider_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        return {"balance": provider.get_balance()}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.post("/herosms/prices")
def herosms_prices(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _provider_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        country = str(body.country or provider.default_country or HERO_SMS_DEFAULT_COUNTRY)
        return {"prices": provider.get_prices(service=service, country=country)}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


class HeroSmsBestCountryRequest(BaseModel):
    api_key: str = ""
    service: str = ""
    proxy: str = ""
    min_stock: int = 20
    max_price: float = 0
    top_n: int = 10


class SmsBowerTopCountriesRequest(BaseModel):
    api_key: str = ""
    service: str = "dr"
    proxy: str = ""
    min_stock: int = 1
    max_price: float = 0
    top_n: int = 30
    usd_cny_rate: float = 7.2


@router.post("/herosms/top-countries")
def herosms_top_countries(body: HeroSmsBestCountryRequest | None = None):
    """获取按价格排序的国家列表（含价格和库存）。"""
    body = body or HeroSmsBestCountryRequest()
    provider = _provider_from_payload(HeroSmsQueryRequest(
        api_key=body.api_key, service=body.service, proxy=body.proxy,
    ))
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        rows = provider.get_top_countries(service=service)
        # 只返回有库存的
        rows = [r for r in rows if (r.get("count") or 0) > 0]
        if body.top_n > 0:
            rows = rows[:body.top_n]
        return {"countries": rows, "service": service}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.post("/herosms/best-country")
def herosms_best_country(body: HeroSmsBestCountryRequest | None = None):
    """自动选择最优国家（价格最低 + 库存充足）。"""
    body = body or HeroSmsBestCountryRequest()
    provider = _provider_from_payload(HeroSmsQueryRequest(
        api_key=body.api_key, service=body.service, proxy=body.proxy,
    ))
    if not provider.api_key:
        raise HTTPException(400, "HeroSMS API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        best = provider.get_best_country(
            service=service,
            min_stock=body.min_stock,
            max_price=body.max_price,
        )
        if best:
            # 获取详细信息
            rows = provider.get_top_countries(service=service)
            detail = next((r for r in rows if str(r.get("country")) == str(best)), None)
            return {
                "country": best,
                "detail": detail,
                "service": service,
            }
        return {"country": None, "detail": None, "service": service}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


# ── SMSBower endpoints ──────────────────────────────────────────────────────

def _saved_smsbower_config() -> dict:
    return ProviderSettingsRepository().resolve_runtime_settings("sms", "smsbower_api", {})


def _smsbower_request_proxy(payload: HeroSmsQueryRequest, saved: dict) -> str:
    explicit = str(payload.proxy or saved.get("sms_proxy") or saved.get("proxy") or "").strip()
    if explicit:
        return explicit
    try:
        from core.proxy_pool import proxy_pool

        return str(proxy_pool.get_next() or "").strip()
    except Exception:
        return ""


def _smsbower_from_payload(payload: HeroSmsQueryRequest | None = None) -> SmsBowerProvider:
    payload = payload or HeroSmsQueryRequest()
    saved = _saved_smsbower_config()
    api_key = str(payload.api_key or saved.get("smsbower_api_key") or "").strip()
    return SmsBowerProvider(
        api_key=api_key,
        default_service=str(
            payload.service
            or saved.get("sms_service")
            or saved.get("smsbower_service")
            or saved.get("smsbower_default_service")
            or HERO_SMS_DEFAULT_SERVICE
        ),
        default_country=str(
            payload.country
            or saved.get("sms_country")
            or saved.get("smsbower_country")
            or saved.get("smsbower_default_country")
            or HERO_SMS_DEFAULT_COUNTRY
        ),
        max_price=_safe_float(saved.get("smsbower_max_price"), -1),
        proxy=_smsbower_request_proxy(payload, saved) or None,
    )


@router.get("/smsbower/countries")
def smsbower_countries():
    try:
        provider = _smsbower_from_payload()
        if not provider.api_key:
            return {"countries": []}
        return {"countries": provider.get_countries()}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.get("/smsbower/services")
def smsbower_services(country: str = ""):
    try:
        provider = _smsbower_from_payload(HeroSmsQueryRequest(country=country))
        if not provider.api_key:
            return {"services": []}
        return {"services": provider.get_services(country=country or None)}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.post("/smsbower/balance")
def smsbower_balance(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _smsbower_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "SMSBower API Key 未配置")
    try:
        return {"balance": provider.get_balance()}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.post("/smsbower/prices")
def smsbower_prices(body: HeroSmsQueryRequest | None = None):
    body = body or HeroSmsQueryRequest()
    provider = _smsbower_from_payload(body)
    if not provider.api_key:
        raise HTTPException(400, "SMSBower API Key 未配置")
    try:
        service = str(body.service or provider.default_service or HERO_SMS_DEFAULT_SERVICE)
        country = str(body.country or provider.default_country or HERO_SMS_DEFAULT_COUNTRY)
        return {"prices": provider.get_prices(service=service, country=country)}
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc))


@router.post("/smsbower/top-countries")
def smsbower_top_countries(body: SmsBowerTopCountriesRequest | None = None):
    """Return SMSBower OpenAI country prices, stock, and partner details."""
    body = body or SmsBowerTopCountriesRequest()
    provider = _smsbower_from_payload(HeroSmsQueryRequest(
        api_key=body.api_key,
        service=body.service,
        proxy=body.proxy,
    ))
    if not provider.api_key:
        raise HTTPException(400, "SMSBower API Key 未配置")
    try:
        service = str(body.service or provider.default_service or "dr").strip()
        min_stock = max(int(body.min_stock or 0), 0)
        max_price = max(float(body.max_price or 0), 0)
        top_n = min(max(int(body.top_n or 0), 0), 200)
        rate = float(body.usd_cny_rate or 7.2)
        if rate <= 0 or rate > 20:
            raise HTTPException(400, "USD/CNY 汇率必须大于 0 且不超过 20")

        rows = []
        for item in provider.get_top_countries(service=service):
            count = max(int(item.get("count") or 0), 0)
            price = max(float(item.get("price") or 0), 0)
            if count <= 0:
                continue
            row = dict(item)
            row["price"] = round(price, 4)
            row["price_cny"] = round(price * rate, 2)
            providers = []
            for provider_item in item.get("providers") or []:
                provider_row = dict(provider_item)
                provider_price = max(float(provider_row.get("price") or 0), 0)
                provider_row["price"] = round(provider_price, 4)
                provider_row["price_cny"] = round(provider_price * rate, 2)
                providers.append(provider_row)
            providers.sort(key=lambda provider_item: (
                provider_item.get("price", 0),
                -int(provider_item.get("count") or 0),
            ))
            row["providers"] = providers
            row["price_tiers"] = [
                {
                    "price": tier_price,
                    "price_cny": round(tier_price * rate, 2),
                    "count": sum(
                        int(provider_item.get("count") or 0)
                        for provider_item in providers
                        if float(provider_item.get("price") or 0) == tier_price
                    ),
                    "provider_ids": [
                        str(provider_item.get("provider_id") or "")
                        for provider_item in providers
                        if float(provider_item.get("price") or 0) == tier_price
                        and str(provider_item.get("provider_id") or "")
                    ],
                }
                for tier_price in sorted({float(provider_item.get("price") or 0) for provider_item in providers})
            ]
            row["eligible"] = count >= min_stock and (max_price <= 0 or price <= max_price)
            rows.append(row)
        rows.sort(key=lambda item: (not item["eligible"], item["price"], -item["count"]))
        if top_n:
            rows = rows[:top_n]
        return {
            "service": service,
            "usd_cny_rate": rate,
            "countries": rows,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, _public_sms_error(exc, provider.api_key))
