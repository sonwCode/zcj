"""接码服务基类 + SMS-Activate / HeroSMS 实现。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


# SMSBower price tiers can remain selected in the UI even while the upstream
# inventory or the target service is temporarily rejecting them. Keep a
# process-wide cooldown so concurrent registration tasks do not immediately
# re-rent the same exhausted tier. The registry is deliberately in-memory:
# it follows the lifetime of the service and does not add another persistent
# state file or database table to the registration application.
_SMS_TIER_COOLDOWNS: dict[str, float] = {}
_SMS_TIER_COOLDOWN_LOCK = threading.Lock()


def clear_sms_tier_cooldowns() -> None:
    """Clear process-wide tier cooldowns (used by tests and local recovery)."""
    with _SMS_TIER_COOLDOWN_LOCK:
        _SMS_TIER_COOLDOWNS.clear()


def _cooldown_remaining(key: str, now: float | None = None) -> float:
    """Return seconds remaining for a tier, removing expired entries."""
    if not key:
        return 0.0
    current = time.monotonic() if now is None else now
    with _SMS_TIER_COOLDOWN_LOCK:
        until = float(_SMS_TIER_COOLDOWNS.get(key, 0.0) or 0.0)
        if until <= current:
            _SMS_TIER_COOLDOWNS.pop(key, None)
            return 0.0
        return until - current


def _set_cooldown(key: str, seconds: float, now: float | None = None) -> float:
    """Set or extend a tier cooldown and return its resulting duration."""
    if not key or seconds <= 0:
        return 0.0
    current = time.monotonic() if now is None else now
    until = current + seconds
    with _SMS_TIER_COOLDOWN_LOCK:
        until = max(until, float(_SMS_TIER_COOLDOWNS.get(key, 0.0) or 0.0))
        _SMS_TIER_COOLDOWNS[key] = until
    return max(until - current, 0.0)


@dataclass
class SmsActivation:
    """Represents an active phone number rental."""
    activation_id: str
    phone_number: str
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """Base class for SMS verification code providers."""

    auto_report_success_on_code = True

    @abstractmethod
    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        """Rent a phone number for the given service."""
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        """Wait for and return the SMS verification code."""
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        """Cancel/release an activation. Returns True on success."""
        ...

    def report_success(self, activation_id: str) -> bool:
        """Report that the code was used successfully (optional)."""
        return True

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        """Optional hook used by providers that can request upstream resend."""
        return None

    def set_cancel_check(self, callback: Callable[[], bool] | None) -> None:
        """Optional hook used to interrupt provider polling when a task is cancelled."""
        self._cancel_check = callback if callable(callback) else (lambda: False)

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects a received code."""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects the rented phone."""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """Optional hook used when the target service accepts the rented phone."""
        return None

    def get_reuse_info(self) -> dict:
        """Return provider-specific reuse state for task scheduling."""
        return {}


# ---------------------------------------------------------------------------
# SMS-Activate implementation (https://sms-activate.guru)
# ---------------------------------------------------------------------------

SMS_ACTIVATE_SERVICES = {
    "cursor": "ot",
    "chatgpt": "dr",
    "openai": "dr",
    "google": "go",
    "microsoft": "mg",
    "default": "ot",
}

SMS_ACTIVATE_COUNTRIES = {
    "ru": "0",
    "us": "187",
    "uk": "16",
    "in": "22",
    "id": "6",
    "ph": "4",
    "th": "52",
    "br": "73",
    "default": "0",
}


def _resolve_sms_activate_country_id(country: str, default_country: str) -> str:
    raw = str(country or default_country or "").strip().lower()
    if not raw:
        raw = "default"
    if raw.isdigit():
        return raw
    return SMS_ACTIVATE_COUNTRIES.get(raw, SMS_ACTIVATE_COUNTRIES["default"])


class SmsActivateProvider(BaseSmsProvider):
    """SMS-Activate (sms-activate.guru) provider."""

    BASE_URL = "https://api.sms-activate.guru/stubs/handler_api.php"

    def __init__(self, api_key: str, *, default_country: str = "", proxy: str = None):
        self.api_key = api_key
        self.default_country = default_country or "ru"
        self._proxy = {"http": proxy, "https": proxy} if proxy else None

    def _request(self, action: str, **params) -> str:
        params["api_key"] = self.api_key
        params["action"] = action
        resp = requests.get(
            self.BASE_URL,
            params=params,
            timeout=20,
            proxies=self._proxy,
        )
        resp.raise_for_status()
        return resp.text.strip()

    def get_balance(self) -> float:
        result = self._request("getBalance")
        if result.startswith("ACCESS_BALANCE:"):
            return float(result.split(":")[1])
        raise RuntimeError(f"SMS-Activate getBalance failed: {result}")

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = SMS_ACTIVATE_SERVICES.get(service, SMS_ACTIVATE_SERVICES["default"])
        country_id = _resolve_sms_activate_country_id(country, self.default_country)

        result = self._request("getNumber", service=service_code, country=country_id)
        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")
            return SmsActivation(
                activation_id=parts[1],
                phone_number=parts[2],
                country=country or self.default_country,
            )

        if "NO_NUMBERS" in result:
            raise RuntimeError(f"SMS-Activate: 当前无可用号码 (service={service_code}, country={country_id})")
        if "NO_BALANCE" in result:
            raise RuntimeError("SMS-Activate: 余额不足")
        raise RuntimeError(f"SMS-Activate getNumber failed: {result}")

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        deadline = time.monotonic() + max(int(timeout or 0), 0)
        while time.monotonic() < deadline:
            if callable(getattr(self, "_cancel_check", None)) and self._cancel_check():
                return ""
            result = self._request("getStatus", id=activation_id)
            if result.startswith("STATUS_OK:"):
                return result.split(":")[1]
            if result == "STATUS_WAIT_CODE":
                time.sleep(3)
                continue
            if result == "STATUS_WAIT_RETRY":
                self._request("setStatus", id=activation_id, status="6")
                time.sleep(3)
                continue
            if result == "STATUS_CANCEL":
                return ""
            time.sleep(3)

        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="8")
        return "ACCESS" in result

    def report_success(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="6")
        return "ACCESS" in result


# ---------------------------------------------------------------------------
# HeroSMS implementation (https://hero-sms.com/stubs/handler_api.php)
# ---------------------------------------------------------------------------

HERO_SMS_DEFAULT_SERVICE = "dr"
HERO_SMS_DEFAULT_COUNTRY = "187"
HERO_SMS_PHONE_LIFETIME = 20 * 60
_HERO_SMS_CACHE_LOCK = threading.Lock()
_HERO_SMS_VERIFY_LOCK = threading.RLock()
_HERO_SMS_CACHE: dict | None = None


def _project_data_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def hero_sms_cache_file() -> Path:
    return _project_data_dir() / ".herosms_phone_cache.json"


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _first_config_value(config: dict, *keys: str, default=None):
    for key in keys:
        value = config.get(key)
        if value not in (None, ""):
            return value
    return default


def _mask_phone_number(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 6:
        return "***"
    return f"{raw[:3]}***{raw[-3:]}"


def _mask_activation_id(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 6:
        return "***"
    return f"{raw[:3]}***{raw[-2:]}"


def _normalize_hero_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy or proxy.startswith("singbox://"):
        return None
    return proxy


def _parse_hero_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _canonical_sms_event_fields(event_fields: dict | None) -> dict:
    event_fields = event_fields or {}
    canonical: dict[str, str] = {}
    channel = str(event_fields.get("channel") or "").strip()
    if channel:
        canonical["channel"] = channel
    sms_time = (
        event_fields.get("dateTime")
        or event_fields.get("date")
        or event_fields.get("smsDate")
        or event_fields.get("smsTime")
        or ""
    )
    if sms_time:
        canonical["time"] = str(sms_time)
    text = event_fields.get("text") or event_fields.get("smsText")
    if text:
        canonical["text"] = str(text)
    if channel == "call":
        for key in ("from", "url"):
            if event_fields.get(key):
                canonical[key] = str(event_fields[key])
    if not sms_time:
        for key in ("repeated", "activationStatus", "verificationType"):
            if event_fields.get(key) is not None:
                canonical[key] = str(event_fields[key])
    return canonical


def _has_real_sms_time(event_fields: dict | None) -> bool:
    raw_time = (
        (event_fields or {}).get("dateTime")
        or (event_fields or {}).get("date")
        or (event_fields or {}).get("smsDate")
        or (event_fields or {}).get("smsTime")
        or ""
    )
    raw_time = str(raw_time).strip()
    return bool(raw_time and raw_time not in {"0", "0000-00-00 00:00:00", "0000-00-00T00:00:00"})


def _sms_event_key(activation_id: str, code: str, event_fields: dict | None) -> str:
    identity = {"activation_id": str(activation_id), "code": str(code)}
    identity.update(_canonical_sms_event_fields(event_fields))
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_sms_candidate(activation_id: str, source: str, code, event_fields: dict | None = None) -> dict | None:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    canonical = _canonical_sms_event_fields(event_fields)
    sms_key = _sms_event_key(activation_id, code, event_fields) if event_fields else ""
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": sms_key,
        "sms_time": canonical.get("time", ""),
        "sms_text": canonical.get("text", ""),
        "allow_same_code": _has_real_sms_time(event_fields),
    }


def _candidate_is_attempted(candidate: dict, used_codes: set, attempted_sms_keys: set) -> bool:
    sms_key = str(candidate.get("sms_key") or "")
    code = str(candidate.get("code") or "")
    if sms_key and sms_key in attempted_sms_keys:
        return True
    return bool(code in used_codes and not candidate.get("allow_same_code"))


class HeroSmsProvider(BaseSmsProvider):
    """HeroSMS provider with resend, SMS event dedupe, and short-lived phone reuse."""

    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
    PROVIDER_LABEL = "HeroSMS"
    auto_report_success_on_code = False

    def __init__(
        self,
        api_key: str,
        *,
        default_service: str = HERO_SMS_DEFAULT_SERVICE,
        default_country: str = HERO_SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        proxy: str | None = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
    ):
        self.api_key = str(api_key or "").strip()
        self.default_service = str(default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.proxy = _normalize_hero_proxy(proxy)
        self.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self.openai_resend_callback: Callable[[], None] | None = None
        self.last_code_result: dict | None = None
        self.current_activation: SmsActivation | None = None
        self._cancel_check: Callable[[], bool] = lambda: False
        self._poll_request_timeout = 30
        self.supports_active_activations = True

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        resp.raise_for_status()
        return resp

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"{self.PROVIDER_LABEL} getBalance failed: {text}")

    def get_services(self, country: str | int | None = None, lang: str = "cn") -> list:
        params = {"action": "getServicesList", "lang": lang}
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params, needs_key=False).json()
        if isinstance(data, dict) and data.get("status") == "success":
            return list(data.get("services") or [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 可能是 {"dr": {"name": "OpenAI", ...}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "error"):
                    continue
                if isinstance(value, dict):
                    if "code" not in value:
                        value["code"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"code": key, "name": value})
            if result:
                return result
        raise RuntimeError(f"{self.PROVIDER_LABEL} getServicesList returned unexpected response")

    def get_countries(self) -> list:
        data = self._request({"action": "getCountries"}, needs_key=False).json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 检查是否是错误响应 {"status":0,"message":"No access","data":[]}
            if data.get("status") == 0 or data.get("message") == "No access":
                raise RuntimeError(f"SMS API access denied: {data.get('message', 'unknown')}")
            # HeroSMS 可能返回 {"0": {"id": 0, "eng": "Russia"}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "data", "error"):
                    continue
                if isinstance(value, dict):
                    if "id" not in value:
                        value["id"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"id": key, "eng": value, "name": value})
            if result:
                return result
        raise RuntimeError("SMS getCountries returned unexpected response")

    def get_prices(self, service: str | None = None, country: str | int | None = None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError(f"{self.PROVIDER_LABEL} getPrices returned unexpected response")

    def get_top_countries(self, service: str | None = None) -> list[dict]:
        """获取指定服务按价格排序的国家列表（含价格和库存）。

        优先使用 getTopCountriesByServiceRank API，降级到 getPrices 全量解析。
        返回格式: [{"country": "66", "name": "Thailand", "price": 0.12, "count": 150}, ...]
        """
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()

        # 策略1: 使用 getTopCountriesByServiceRank（HeroSMS 专用排名接口）
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries_response(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue

        # 策略2: 从 getPrices 全量数据中解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc_data = services.get(service_code)
                if not isinstance(svc_data, dict):
                    continue
                price = svc_data.get("cost") or svc_data.get("price")
                count = svc_data.get("count") or svc_data.get("qty") or svc_data.get("available")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
            rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
            return rows
        except Exception:
            return []

    def _parse_top_countries_response(self, data) -> list[dict]:
        """解析 getTopCountriesByServiceRank 响应。"""
        rows = []
        items = data
        # 可能嵌套在 data/result 键下
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            # {country_id: {price, count, ...}} 格式
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = value.get("price") or value.get("cost") or value.get("retail_price")
                count = value.get("count") or value.get("qty") or value.get("available") or value.get("stock")
                name = value.get("name") or value.get("countryName") or value.get("country_name") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": country_id, "name": str(name), "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost") or item.get("retail_price") or item.get("retailPrice")
                count = item.get("count") or item.get("qty") or item.get("available") or item.get("stock") or item.get("total")
                name = item.get("name") or item.get("countryName") or item.get("country_name") or item.get("title") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "name": str(name), "price": price, "count": count})
        return rows

    def get_best_country(self, service: str | None = None, *, min_stock: int = 20, max_price: float = 0) -> str | None:
        """自动选择最优国家：价格最低且库存充足。

        Args:
            service: 服务代码（默认使用 self.default_service）
            min_stock: 最低库存要求（默认 20）
            max_price: 最高价格限制（0 表示不限）

        Returns:
            最优国家 ID 字符串，或 None（无可用国家）
        """
        # HeroSMS/SMSBower 中已验证对 OpenAI 走 SMS（非 WhatsApp）的国家白名单
        # OpenAI 2025年起对绝大多数国家改用 WhatsApp 验证
        # 目前只有泰国确认走 SMS
        ALLOWED_COUNTRIES = {
            "52",   # Thailand (已验证走SMS)
        }

        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("get_best_country 查询失败: %s", exc)
            return None

        if not rows:
            return None

        for row in rows:
            country_id = str(row.get("country") or "")
            if country_id not in ALLOWED_COUNTRIES:
                continue
            price = row.get("price") or 0
            count = row.get("count") or 0
            if count < min_stock:
                continue
            if max_price > 0 and price > max_price:
                continue
            return country_id

        # 如果没有满足 min_stock 的，放宽到 count > 0
        for row in rows:
            country_id = str(row.get("country") or "")
            if country_id not in ALLOWED_COUNTRIES:
                continue
            price = row.get("price") or 0
            count = row.get("count") or 0
            if count <= 0:
                continue
            if max_price > 0 and price > max_price:
                continue
            return country_id

        return None

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "api_key_hash": _hash_secret(self.api_key),
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> dict | None:
        global _HERO_SMS_CACHE
        if _HERO_SMS_CACHE is not None:
            cache = _HERO_SMS_CACHE
        else:
            path = hero_sms_cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        identity = self._cache_identity(service, country)
        if any(str(cache.get(key) or "") != str(value) for key, value in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= HERO_SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
            self._save_cache(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        cache["attempted_sms_keys"] = set(cache.get("attempted_sms_keys") or [])
        _HERO_SMS_CACHE = cache
        return cache

    def _save_cache(self, cache: dict | None) -> None:
        global _HERO_SMS_CACHE
        _HERO_SMS_CACHE = cache
        path = hero_sms_cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        serializable["attempted_sms_keys"] = sorted(serializable.get("attempted_sms_keys") or [])
        serializable.pop("client", None)
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    def _stop_reuse(self, reason: str) -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if not cache:
                return
            cache["reuse_stopped"] = True
            cache["stop_reason"] = reason
            self._save_cache(cache)

    def _request_number_raw(self, service: str, country: str) -> dict:
        common = {"service": service, "country": country}
        provider_ids = str(getattr(self, "provider_ids", "") or "").strip()
        excluded_provider_ids = {
            str(item).strip()
            for item in getattr(self, "excluded_provider_ids", set())
            if str(item).strip()
        }
        min_price = float(getattr(self, "min_price", -1) or -1)
        if provider_ids:
            common["providerIds"] = provider_ids
        if excluded_provider_ids:
            common["exceptProviderIds"] = ",".join(sorted(excluded_provider_ids))
        if min_price >= 0:
            common["minPrice"] = min_price

        # 动态获取该国家该服务的实际价格，用实际价格作为 maxPrice
        # 这样能确保拿到物理号码（而不是被分配虚拟号码）
        effective_max_price = self.max_price if self.max_price > 0 else 1
        try:
            prices = self.get_prices(service=service, country=country)
            # getPrices 返回格式: {country_id: {service_code: {cost, count}}}
            country_prices = prices.get(str(country)) or prices.get(country) or {}
            service_prices = country_prices.get(service) or {}
            actual_cost = service_prices.get("cost") or service_prices.get("price")
            if actual_cost is not None:
                actual_cost = float(actual_cost)
                # 用实际价格的 3 倍作为 maxPrice（留足余量），但不超过用户配置的上限
                dynamic_max = round(actual_cost * 3, 4)
                if self.max_price > 0:
                    effective_max_price = min(self.max_price, max(dynamic_max, 0.2))
                else:
                    effective_max_price = max(dynamic_max, 0.2)
        except Exception:
            pass  # 查询失败就用默认值

        common["maxPrice"] = effective_max_price

        v2_error = ""
        try:
            resp = self._request({"action": "getNumberV2", **common})
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("activationId"):
                return data
            v2_error = resp.text.strip()[:200]
        except Exception as exc:
            v2_error = str(exc)

        # 如果 NO_NUMBERS 且 maxPrice 低于用户配置的上限，提高 maxPrice 重试
        if "NO_NUMBERS" in v2_error and self.max_price > 0 and effective_max_price < self.max_price:
            common["maxPrice"] = self.max_price
            try:
                resp = self._request({"action": "getNumberV2", **common})
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("activationId"):
                    return data
                v2_error = resp.text.strip()[:200]
            except Exception as exc:
                v2_error = str(exc)

        # A provider tier may be sold out between the price query and purchase.
        # Keep the selected country and price ceiling, but retry V2 once without
        # providerIds so another currently stocked partner in that country can serve it.
        if (
            "NO_NUMBERS" in v2_error
            and common.get("providerIds")
            and not getattr(self, "strict_provider_ids", False)
        ):
            common.pop("providerIds", None)
            try:
                resp = self._request({"action": "getNumberV2", **common})
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("activationId"):
                    return data
                v2_error = resp.text.strip()[:200]
            except Exception as exc:
                v2_error = str(exc)

        try:
            text = self._request({"action": "getNumber", **common}).text.strip()
            if text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                        "activationCost": None,
                    }
            raise RuntimeError(text[:200])
        except Exception as exc:
            raise RuntimeError(f"{self.PROVIDER_LABEL} 获取号码失败: V2={v2_error}; V1={exc}") from exc

    @staticmethod
    def _format_phone(number_info: dict) -> str:
        raw = str(number_info.get("phoneNumber") or "").strip()
        country_phone_code = str(number_info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if country_phone_code and raw.startswith(country_phone_code):
            return f"+{raw}"
        if country_phone_code:
            return f"+{country_phone_code}{raw}"
        return f"+{raw}"

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        service_code = str(self.default_service or service or HERO_SMS_DEFAULT_SERVICE).strip()
        country_id = str(country or self.default_country or HERO_SMS_DEFAULT_COUNTRY).strip()

        # One-shot activations are independent provider orders.  They neither
        # read nor write the shared reuse cache, so their network purchases can
        # proceed concurrently.  This is the path used automatically when a
        # registration task has concurrency > 1.
        if not self.reuse_phone_to_max:
            number_info = self._request_number_raw(service_code, country_id)
            activation_id = str(number_info.get("activationId") or "")
            phone = self._format_phone(number_info)
            if not activation_id or not phone.strip("+"):
                raise RuntimeError(f"{self.PROVIDER_LABEL} 返回的号码信息不完整")
            activation = SmsActivation(
                activation_id=activation_id,
                phone_number=phone,
                country=country_id,
                metadata={"reused": False, "number_info": number_info},
            )
            self.current_activation = activation
            return activation

        with _HERO_SMS_VERIFY_LOCK:
            with _HERO_SMS_CACHE_LOCK:
                cache = self._load_cache(service_code, country_id)
                if cache:
                    activation = SmsActivation(
                        activation_id=str(cache["activation_id"]),
                        phone_number=str(cache["phone_number"]),
                        country=country_id,
                        metadata={"reused": True, "use_count": int(cache.get("use_count") or 0)},
                    )
                    self.current_activation = activation
                    return activation

                number_info = self._request_number_raw(service_code, country_id)
                activation_id = str(number_info.get("activationId") or "")
                phone = self._format_phone(number_info)
                if not activation_id or not phone.strip("+"):
                    raise RuntimeError(f"{self.PROVIDER_LABEL} 返回的号码信息不完整")
                cache = {
                    **self._cache_identity(service_code, country_id),
                    "activation_id": activation_id,
                    "phone_number": phone,
                    "acquired_at": time.time(),
                    "use_count": 0,
                    "used_codes": set(),
                    "attempted_sms_keys": set(),
                    "reuse_stopped": False,
                    "stop_reason": "",
                }
                self._save_cache(cache)
                activation = SmsActivation(
                    activation_id=activation_id,
                    phone_number=phone,
                    country=country_id,
                    metadata={"reused": False, "number_info": number_info},
                )
                self.current_activation = activation
                return activation

    def get_status(self, activation_id: str) -> dict:
        return _parse_hero_status_text(self._request(
            {"action": "getStatus", "id": activation_id},
            timeout=self._poll_request_timeout,
        ).text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request(
            {"action": "getStatusV2", "id": activation_id},
            timeout=self._poll_request_timeout,
        )
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_hero_status_text(text)
        if isinstance(data, str):
            return _parse_hero_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown", "raw": data}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_hero_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(
                    activation_id,
                    f"getStatusV2.{channel}",
                    item.get("code"),
                    {
                        "channel": channel,
                        "dateTime": item.get("dateTime"),
                        "text": item.get("text"),
                        "from": item.get("from"),
                        "url": item.get("url"),
                        "verificationType": data.get("verificationType"),
                    },
                )
                if candidate:
                    return candidate
        return {"status": "wait_code", "raw": data}

    def get_active_activations(self, start: int = 0, limit: int = 20) -> list:
        data = self._request(
            {"action": "getActiveActivations", "start": start, "limit": limit},
            timeout=self._poll_request_timeout,
        ).json()
        if isinstance(data, dict) and "data" in data:
            return list(data.get("data") or [])
        return []

    def set_status(self, activation_id: str, status: int) -> str:
        return self._request(
            {"action": "setStatus", "id": activation_id, "status": status},
            timeout=self._poll_request_timeout,
        ).text.strip()

    def cancel_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request(
                {"action": "cancelActivation", "id": activation_id},
                timeout=min(self._poll_request_timeout, 10),
            )
            if resp.status_code == 204 or "ACCESS_CANCEL" in resp.text:
                return True
        except Exception:
            pass
        try:
            return "ACCESS_CANCEL" in self.set_status(activation_id, 8)
        except Exception:
            return False

    def finish_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "finishActivation", "id": activation_id})
            text = resp.text.strip()
            return resp.status_code in (200, 204) or "ACCESS" in text
        except Exception:
            try:
                return "ACCESS" in self.set_status(activation_id, 6)
            except Exception:
                return False

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self.set_status(activation_id, 3)
            return True
        except Exception:
            return False

    def wait_for_code(self, activation_id: str, *, timeout: int = 180, poll_interval: int = 3) -> dict | None:
        timeout = max(int(timeout or 0), 0)
        deadline = time.monotonic() + timeout
        start = time.monotonic()
        last_hero_resend = start
        openai_resent = False
        warned_v2 = False
        while time.monotonic() < deadline:
            if self._cancel_check():
                return None
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE or {}
                used_codes = set(cache.get("used_codes") or [])
                attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])

            sources = ("v2", "v1", "active") if self.supports_active_activations else ("v2", "v1")
            for source in sources:
                if self._cancel_check():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._poll_request_timeout = max(1, min(8, int(remaining + 0.999)))
                try:
                    candidate = None
                    if source == "v2":
                        result = self.get_status_v2(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = result
                    elif source == "v1":
                        result = self.get_status(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = _make_sms_candidate(activation_id, "getStatus", result.get("code"))
                    else:
                        for item in self.get_active_activations():
                            if str(item.get("activationId")) == str(activation_id):
                                candidate = _make_sms_candidate(
                                    activation_id,
                                    "getActiveActivations",
                                    item.get("smsCode"),
                                    {
                                        "channel": "sms",
                                        "smsText": item.get("smsText"),
                                        "activationStatus": item.get("activationStatus"),
                                        "repeated": item.get("repeated"),
                                        "dateTime": item.get("dateTime"),
                                        "date": item.get("date") or item.get("smsDate") or item.get("smsTime"),
                                    },
                                )
                                break
                    if candidate and not _candidate_is_attempted(candidate, used_codes, attempted_sms_keys):
                        return candidate
                except Exception as exc:
                    if source == "v2" and not warned_v2:
                        logger.warning("HeroSMS getStatusV2 failed: %s", exc)
                        warned_v2 = True
                    else:
                        logger.debug("HeroSMS status check failed via %s: %s", source, exc)

            elapsed = time.monotonic() - start
            if not openai_resent and elapsed >= 90 and self.openai_resend_callback:
                try:
                    self.openai_resend_callback()
                except Exception as exc:
                    logger.warning("OpenAI phone resend callback failed: %s", exc)
                self.request_resend_sms(activation_id)
                last_hero_resend = time.monotonic()
                openai_resent = True
            elif time.monotonic() - last_hero_resend >= 30:
                self.request_resend_sms(activation_id)
                last_hero_resend = time.monotonic()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(max(float(poll_interval or 0), 0), remaining))
        return None

    def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
        wait_timeout = max(int(timeout or 0), 0)
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE or {}
            if cache and str(cache.get("activation_id")) == str(activation_id):
                remaining = int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0)))
                wait_timeout = min(wait_timeout, max(remaining, 0))
        if wait_timeout <= 0 or self._cancel_check():
            self.last_code_result = None
            return ""
        candidate = self.wait_for_code(activation_id, timeout=wait_timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    def cancel(self, activation_id: str) -> bool:
        try:
            return self.cancel_activation(activation_id)
        finally:
            with _HERO_SMS_CACHE_LOCK:
                cache = _HERO_SMS_CACHE
                if cache and str(cache.get("activation_id")) == str(activation_id):
                    self._clear_cache()

    def report_success(self, activation_id: str) -> bool:
        should_finish = False
        should_clear_cache = False
        handled_cached_activation = False
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                handled_cached_activation = True
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                self._record_last_attempt(cache, failed=False)
                remaining = HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "reuse disabled"
                    should_finish = True
                    should_clear_cache = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
                    should_finish = True
                elif remaining <= 30:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "phone lifetime nearly expired"
                    should_finish = True
                    should_clear_cache = True
                self._save_cache(cache)
                if should_clear_cache:
                    self._clear_cache()
        if handled_cached_activation:
            if should_finish:
                self.finish_activation(activation_id)
            return True
        return self.finish_activation(activation_id)

    def _record_last_attempt(self, cache: dict, *, failed: bool) -> None:
        candidate = self.last_code_result or {}
        code = str(candidate.get("code") or "")
        sms_key = str(candidate.get("sms_key") or "")
        used_codes = set(cache.get("used_codes") or [])
        attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])
        if code:
            used_codes.add(code)
        if sms_key:
            attempted_sms_keys.add(sms_key)
        cache["used_codes"] = used_codes
        cache["attempted_sms_keys"] = attempted_sms_keys
        if failed:
            cache["last_failed_reason"] = "invalid otp"

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._record_last_attempt(cache, failed=True)
                self._save_cache(cache)
        if self.openai_resend_callback:
            try:
                self.openai_resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self.set_status(activation_id, 1)
        except Exception:
            pass

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        # Return the rented number immediately so unused activations are not billed/held.
        try:
            if activation_id:
                self.cancel(str(activation_id))
        except Exception:
            pass
        reason_text = str(reason or "").lower()
        if any(keyword in reason_text for keyword in ("limit", "already", "too many", "exceeded", "maximum", "上限", "已达")):
            self._stop_reuse("phone limit reached")
        else:
            self._stop_reuse(reason or "phone rejected")

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        self.openai_resend_callback = callback

    def get_reuse_info(self) -> dict:
        with _HERO_SMS_CACHE_LOCK:
            cache = _HERO_SMS_CACHE or self._load_cache(self.default_service, self.default_country) or {}
            if not cache:
                return {"alive": False}
            remaining = max(0, int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))))
            return {
                "alive": remaining > 0 and not bool(cache.get("reuse_stopped")),
                "phone_number": cache.get("phone_number", ""),
                "use_count": int(cache.get("use_count") or 0),
                "remaining_seconds": remaining,
                "reuse_stopped": bool(cache.get("reuse_stopped")),
                "stop_reason": cache.get("stop_reason", ""),
            }


class SmsBowerProvider(HeroSmsProvider):
    """SMSBower provider — API 兼容 HeroSMS，仅 base URL 不同。"""

    BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    PROVIDER_LABEL = "SMSBower"

    def __init__(
        self,
        api_key: str,
        *,
        provider_ids: str = "",
        except_provider_ids: str = "",
        min_price: float = -1,
        provider_reject_threshold: int = 2,
        **kwargs,
    ):
        super().__init__(api_key, **kwargs)
        self.provider_ids = str(provider_ids or "").strip()
        self.strict_provider_ids = False
        self.supports_active_activations = False
        self.excluded_provider_ids = {
            item.strip() for item in str(except_provider_ids or "").split(",") if item.strip()
        }
        self.min_price = float(min_price or -1)
        self.provider_reject_threshold = max(int(provider_reject_threshold or 2), 1)
        self._provider_reject_counts: dict[str, int] = {}
        self._rejected_numbers: set[str] = set()
        self._country_map_cache: tuple[dict[str, str], dict[str, str]] | None = None

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        # SMSBower 所有接口都需要 api_key（包括 getServicesList、getCountries）
        payload = dict(params)
        if needs_key or self.api_key:
            payload["api_key"] = self.api_key
        try:
            resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            message = str(exc).replace(self.api_key, "***") if self.api_key else str(exc)
            message = re.sub(r"(?i)(api_key=)[^&\s]+", r"\1***", message)
            raise RuntimeError(f"SMSBower request failed: {message}") from None

    @staticmethod
    def _country_key(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())

    def _country_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        if self._country_map_cache is not None:
            return self._country_map_cache
        name_to_id: dict[str, str] = {}
        id_to_name: dict[str, str] = {}
        for item in self.get_countries():
            if not isinstance(item, dict):
                continue
            country_id = str(item.get("id") or item.get("country") or "").strip()
            if not country_id:
                continue
            display_name = str(item.get("chn") or item.get("eng") or item.get("name") or country_id).strip()
            id_to_name[country_id] = display_name
            for key in ("eng", "chn", "rus", "name"):
                normalized = self._country_key(item.get(key))
                if normalized:
                    name_to_id[normalized] = country_id
        name_to_id.update({
            "usa": "187",
            "unitedstates": "187",
            "colombia": "33",
            "chile": "151",
            "brazil": "73",
            "vietnam": "10",
        })
        self._country_map_cache = (name_to_id, id_to_name)
        return self._country_map_cache

    def _parse_smsbower_top_countries(self, data, *, service: str = "") -> list[dict]:
        if isinstance(data, dict):
            data = data.get("data") or data.get("result") or data.get("response") or data
        if not isinstance(data, dict):
            return []
        name_to_id, id_to_name = self._country_maps()
        rows: list[dict] = []
        for raw_country, payload in data.items():
            if not isinstance(payload, dict):
                continue
            raw_country_text = str(raw_country or "").strip()
            country_id = raw_country_text if raw_country_text.isdigit() else name_to_id.get(self._country_key(raw_country_text), "")
            if not country_id:
                continue
            service_payload = payload.get(service) if service and isinstance(payload.get(service), dict) else payload
            partners: list[dict] = []
            if any(key in service_payload for key in ("price", "cost")):
                partners.append({"provider_id": "", **service_payload})
            else:
                for provider_id, item in service_payload.items():
                    if isinstance(item, dict):
                        partners.append({"provider_id": str(provider_id), **item})
                    else:
                        # getPricesV2 uses {price: count} instead of provider objects.
                        try:
                            partners.append({
                                "provider_id": "",
                                "price": float(provider_id),
                                "count": int(item),
                            })
                        except (TypeError, ValueError):
                            continue
            normalized_partners: list[dict] = []
            for item in partners:
                try:
                    raw_price = item.get("price")
                    if raw_price is None:
                        raw_price = item.get("cost")
                    price = float(raw_price)
                    count = int(item.get("count") or item.get("qty") or item.get("stock") or 0)
                except (TypeError, ValueError):
                    continue
                if price < 0 or count <= 0:
                    continue
                normalized_partners.append({
                    "provider_id": str(item.get("provider_id") or item.get("providerId") or ""),
                    "price": price,
                    "count": count,
                    "rank": str(item.get("rank") or item.get("tier") or item.get("grade") or "").strip().lower(),
                })
            if not normalized_partners:
                continue
            normalized_partners.sort(key=lambda item: (item["price"], -item["count"]))
            rows.append({
                "country": country_id,
                "name": id_to_name.get(country_id) or raw_country_text,
                "price": normalized_partners[0]["price"],
                "count": sum(item["count"] for item in normalized_partners),
                "provider_count": len(normalized_partners),
                "providers": normalized_partners,
            })
        rows.sort(key=lambda item: (item["price"], -item["count"]))
        return rows

    def get_top_countries(self, service: str | None = None) -> list[dict]:
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        # Prices V3/V2 contain the full country/provider table. The documented
        # top-country endpoint identifies Gold-ranked partners, so merge that
        # evidence into the full table instead of guessing ranks from prices.
        last_error: Exception | None = None
        full_rows: list[dict] = []
        for action in ("getPricesV3", "getPricesV2", "getPrices"):
            try:
                rows = self._parse_smsbower_top_countries(
                    self._request({"action": action, "service": service_code}).json(),
                    service=service_code,
                )
                if rows:
                    full_rows = rows
                    break
            except Exception as exc:
                last_error = exc
                continue
        gold_rows: list[dict] = []
        for action in ("getTopCountriesByService", "getTopCountriesByServiceRank"):
            try:
                rows = self._parse_smsbower_top_countries(
                    self._request({"action": action, "service": service_code}).json(),
                    service=service_code,
                )
                if rows:
                    gold_rows = rows
                    break
            except Exception as exc:
                last_error = exc
                continue
        if full_rows:
            gold_provider_ids = {
                str(row.get("country") or ""): {
                    str(provider.get("provider_id") or "")
                    for provider in row.get("providers") or []
                    if str(provider.get("provider_id") or "")
                }
                for row in gold_rows
            }
            for row in full_rows:
                country_gold_ids = gold_provider_ids.get(str(row.get("country") or ""), set())
                for provider in row.get("providers") or []:
                    if str(provider.get("provider_id") or "") in country_gold_ids:
                        provider["rank"] = "gold"
            return full_rows
        if gold_rows:
            for row in gold_rows:
                for provider in row.get("providers") or []:
                    provider["rank"] = "gold"
            return gold_rows
        rows = super().get_top_countries(service=service_code)
        try:
            _name_to_id, id_to_name = self._country_maps()
            for row in rows:
                row["name"] = row.get("name") or id_to_name.get(str(row.get("country") or ""), "")
        except Exception:
            pass
        if rows:
            return rows
        if last_error is not None:
            raise RuntimeError(f"SMSBower price query failed: {last_error}")
        return []

    def get_best_country(self, service: str | None = None, *, min_stock: int = 20, max_price: float = 0) -> str | None:
        rows = self.get_top_countries(service=service)
        eligible = [
            row for row in rows
            if int(row.get("count") or 0) > 0
            and (max_price <= 0 or float(row.get("price") or 0) <= max_price)
        ]
        for row in eligible:
            if int(row.get("count") or 0) >= max(int(min_stock or 0), 0):
                return str(row.get("country") or "") or None
        return str(eligible[0].get("country") or "") if eligible else None

    def get_number(self, *, service: str, country: str = "") -> SmsActivation:
        last_activation = None
        for _attempt in range(3):
            activation = super().get_number(service=service, country=country)
            last_activation = activation
            if activation.phone_number not in self._rejected_numbers:
                return activation
            self.cancel(activation.activation_id)
        raise RuntimeError(
            "SMSBower 连续返回本轮已拒绝号码: "
            f"{_mask_phone_number(getattr(last_activation, 'phone_number', ''))}"
        )

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        # Always return unused/rejected numbers to SMSBower immediately.
        try:
            if activation_id:
                self.cancel(str(activation_id))
        except Exception:
            pass
        activation = self.current_activation
        if activation:
            self._rejected_numbers.add(str(activation.phone_number or ""))
            info = dict((activation.metadata or {}).get("number_info") or {})
            provider_id = str(
                info.get("providerId")
                or info.get("provider_id")
                or info.get("activationOperator")
                or ""
            ).strip()
            if not provider_id and self.provider_ids and "," not in self.provider_ids:
                provider_id = self.provider_ids
            if provider_id.isdigit():
                count = self._provider_reject_counts.get(provider_id, 0) + 1
                self._provider_reject_counts[provider_id] = count
                selected_provider_ids = {
                    item.strip()
                    for item in self.provider_ids.split(",")
                    if item.strip()
                }
                # A single explicitly selected tier is a strict user choice.
                # Keep replacing numbers inside that tier instead of silently
                # excluding it after repeated phone-level rejections.
                can_exclude_provider = len(selected_provider_ids) != 1
                if can_exclude_provider and count >= self.provider_reject_threshold:
                    self.excluded_provider_ids.add(provider_id)
                    logger.warning(
                        "SMSBower provider %s reached reject threshold %s; excluded for this flow",
                        provider_id,
                        self.provider_reject_threshold,
                    )
        super().mark_send_failed(activation_id, reason=reason)


def is_herosms_phone_cache_alive(config: dict | None = None) -> tuple[bool, dict]:
    """Return whether the current HeroSMS cache is reusable for scheduling."""
    config = dict(config or {})
    api_key = str(config.get("herosms_api_key") or "").strip()
    if not api_key:
        return False, {"alive": False}
    provider = HeroSmsProvider(
        api_key,
        default_service=str(config.get("sms_service") or HERO_SMS_DEFAULT_SERVICE),
        default_country=str(config.get("sms_country") or config.get("herosms_country") or HERO_SMS_DEFAULT_COUNTRY),
        phone_success_max=max(0, _safe_int(config.get("register_phone_success_max"), 3)),
    )
    info = provider.get_reuse_info()
    return bool(info.get("alive")), info


# ---------------------------------------------------------------------------
# Factory and browser callback adapter
# ---------------------------------------------------------------------------

def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """Create an SMS provider instance from config."""
    if provider_key in ("sms_activate", "sms_activate_api"):
        api_key = config.get("sms_activate_api_key", "")
        if not api_key:
            raise RuntimeError("SMS-Activate 未配置 API Key")
        return SmsActivateProvider(
            api_key=api_key,
            default_country=config.get("sms_activate_country", config.get("sms_activate_default_country", "")),
            proxy=config.get("sms_proxy") or config.get("proxy") or None,
        )
    if provider_key in ("herosms", "herosms_api"):
        api_key = str(config.get("herosms_api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("HeroSMS 未配置 API Key")
        return HeroSmsProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("herosms_service") or config.get("herosms_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("herosms_country") or config.get("herosms_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(
                _first_config_value(config, "herosms_max_price", "sms_max_price", "max_price", default=-1),
                -1,
            ),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
        )
    if provider_key in ("smsbower", "smsbower_api"):
        api_key = str(config.get("smsbower_api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("SMSBower 未配置 API Key")
        return SmsBowerProvider(
            api_key=api_key,
            default_service=str(config.get("sms_service") or config.get("smsbower_service") or config.get("smsbower_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(config.get("sms_country") or config.get("smsbower_country") or config.get("smsbower_default_country") or HERO_SMS_DEFAULT_COUNTRY),
            max_price=_safe_float(
                _first_config_value(config, "smsbower_max_price", "sms_max_price", "max_price", default=-1),
                -1,
            ),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), True),
            phone_success_max=max(0, _safe_int(config.get("register_phone_extra_max") or config.get("register_phone_success_max"), 3)),
            provider_ids=str(config.get("smsbower_provider_ids") or ""),
            except_provider_ids=str(config.get("smsbower_except_provider_ids") or ""),
            min_price=_safe_float(config.get("smsbower_min_price"), -1),
            provider_reject_threshold=max(1, _safe_int(config.get("smsbower_provider_reject_threshold"), 2)),
        )
    raise RuntimeError(f"未知的接码服务: {provider_key}")


class PhoneCallbackController:
    """Callable phone callback with optional lifecycle hooks for advanced providers."""

    def __init__(self, provider_key: str, config: dict, *, service: str, country: str = "", log_fn=None):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.phase = "need_number"
        self.completed = False
        self._verify_lock_acquired = False
        self.awaiting_external_success = False
        self._cancel_check: Callable[[], bool] = lambda: False
        raw_countries = self.config.get("sms_countries") or self.config.get("smsbower_countries") or []
        if isinstance(raw_countries, str):
            raw_countries = re.split(r"[\s,;]+", raw_countries)
        self.country_candidates = list(dict.fromkeys(
            str(item).strip() for item in raw_countries if str(item).strip()
        ))
        raw_provider_map = self.config.get("smsbower_provider_ids_by_country") or {}
        if isinstance(raw_provider_map, str):
            try:
                raw_provider_map = json.loads(raw_provider_map)
            except Exception:
                raw_provider_map = {}
        self.provider_ids_by_country: dict[str, list[str]] = {}
        if isinstance(raw_provider_map, dict):
            for country_key, provider_values in raw_provider_map.items():
                if isinstance(provider_values, str):
                    provider_values = re.split(r"[\s,;]+", provider_values)
                if not isinstance(provider_values, (list, tuple, set)):
                    continue
                provider_ids = list(dict.fromkeys(
                    str(item).strip()
                    for item in provider_values
                    if str(item).strip()
                ))
                if provider_ids:
                    self.provider_ids_by_country[str(country_key).strip()] = provider_ids
        self._provider_cursor_by_country: dict[str, int] = {}
        # Same country+provider tier keeps renting new numbers for delivery
        # failures until this many retries. Keep the default low so a tier
        # with poor SMS delivery does not consume many activations before the
        # country/provider ladder advances. Number-in-use failures skip the
        # tier immediately in ``mark_send_failed``.
        self.same_tier_max_retries = max(
            _safe_int(
                self.config.get("sms_same_tier_retries")
                or self.config.get("smsbower_same_provider_retries"),
                2,
            ),
            1,
        )
        # Keep the default in the requested 0.5-1 hour window. Explicit zero
        # disables the shared cooldown for callers that need legacy behavior;
        # the UI exposes only the bounded 30/45/60 minute choices.
        raw_cooldown_minutes = self.config.get("sms_tier_cooldown_minutes")
        if raw_cooldown_minutes in (None, ""):
            self.tier_cooldown_seconds = 45 * 60
        else:
            configured_minutes = _safe_float(raw_cooldown_minutes, 45.0)
            if configured_minutes != configured_minutes:  # NaN
                configured_minutes = 45.0
            # Keep non-zero values inside the UI's 0.5-1 hour contract.  Zero
            # remains an explicit escape hatch for legacy callers/tests.
            if configured_minutes != 0:
                configured_minutes = max(configured_minutes, 30.0)
            self.tier_cooldown_seconds = max(
                0.0,
                min(configured_minutes, 60.0) * 60.0,
            )
        self._same_tier_fail_count: dict[str, int] = {}
        self._exhausted_tiers: set[str] = set()
        self._exhausted_countries: set[str] = set()
        self.virtual_country_ids = {
            item.strip()
            for item in str(self.config.get("smsbower_virtual_country_ids") or "12").split(",")
            if item.strip()
        }
        self.allow_virtual_countries = _safe_bool(self.config.get("smsbower_allow_virtual"), False)
        self.only_virtual_country_pool = bool(self.country_candidates) and all(
            country in self.virtual_country_ids for country in self.country_candidates
        )
        if self.provider_key in {"smsbower", "smsbower_api"} and not self.allow_virtual_countries:
            removed_virtual = [
                country for country in self.country_candidates
                if country in self.virtual_country_ids
            ]
            if removed_virtual:
                self.country_candidates = [
                    country for country in self.country_candidates
                    if country not in self.virtual_country_ids
                ]
                self.log(
                    "已跳过虚拟/VOIP号码国家: "
                    + ",".join(removed_virtual)
                )
        self._country_cursor = max(_safe_int(self.config.get("sms_country_offset"), 0), 0)

    @staticmethod
    def _is_number_in_use_failure(reason: str) -> bool:
        """Return whether the target rejected the number as already bound.

        This is materially different from an SMS delivery timeout: another
        number from the same provider tier is unlikely to improve the result,
        so the controller should move on to the next tier/country immediately.
        """
        text = str(reason or "").strip().lower()
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                "phone_number_in_use",
                "phone number already in use",
                "phone number is already in use",
                "phone number already used",
                "number already in use",
                "手机号已绑定",
                "手机号已被使用",
            )
        )

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
            hook = getattr(self.provider, "set_cancel_check", None)
            if callable(hook):
                hook(self._cancel_check)
        return self.provider

    def set_cancel_check(self, callback: Callable[[], bool] | None) -> None:
        self._cancel_check = callback if callable(callback) else (lambda: False)
        if self.provider is not None:
            hook = getattr(self.provider, "set_cancel_check", None)
            if callable(hook):
                hook(self._cancel_check)

    def _configure_provider_for_country(self, provider: BaseSmsProvider, country: str) -> None:
        if not isinstance(provider, SmsBowerProvider):
            return
        country_key = str(country or "").strip()
        if self.provider_ids_by_country:
            provider_ids = self.provider_ids_by_country.get(country_key, [])
            if provider_ids:
                cursor = self._provider_cursor_by_country.get(country_key, 0) % len(provider_ids)
                provider.provider_ids = provider_ids[cursor]
                provider.strict_provider_ids = True
            else:
                provider.provider_ids = ""
                provider.strict_provider_ids = False

    def _advance_provider_for_country(self, country: str) -> None:
        country_key = str(country or "").strip()
        provider_ids = self.provider_ids_by_country.get(country_key, [])
        if len(provider_ids) <= 1:
            return
        next_cursor = (self._provider_cursor_by_country.get(country_key, 0) + 1) % len(provider_ids)
        self._provider_cursor_by_country[country_key] = next_cursor
        self.log(
            f"国家 {country_key} 切换到下一个 Provider 档位: "
            f"{provider_ids[next_cursor]} ({next_cursor + 1}/{len(provider_ids)})"
        )

    def _tier_key(self, country: str) -> str:
        """Stable key for current country + selected SMSBower provider tier."""
        country_key = str(country or "").strip()
        provider_ids = self.provider_ids_by_country.get(country_key, [])
        if provider_ids:
            cursor = self._provider_cursor_by_country.get(country_key, 0) % len(provider_ids)
            return f"{country_key}:{provider_ids[cursor]}"
        meta = (self.activation.metadata or {}) if self.activation else {}
        number_info = meta.get("number_info") if isinstance(meta.get("number_info"), dict) else {}
        provider_id = str(
            number_info.get("providerId")
            or number_info.get("provider_id")
            or meta.get("provider_id")
            or "default"
        ).strip()
        return f"{country_key}:{provider_id or 'default'}"

    def _current_tier_key(self, country: str) -> str:
        """Return the selected country/provider key without an activation."""
        country_key = str(country or "").strip()
        provider_ids = self.provider_ids_by_country.get(country_key, [])
        if not provider_ids:
            return f"{country_key}:default"
        cursor = self._provider_cursor_by_country.get(country_key, 0) % len(provider_ids)
        return f"{country_key}:{provider_ids[cursor]}"

    def _cooldown_key(self, tier_key: str) -> str:
        """Scope cooldowns to provider, service, country, and selected tier."""
        return f"{self.provider_key}:{self.service}:{tier_key}"

    def _tier_cooldown_remaining(self, tier_key: str) -> float:
        return _cooldown_remaining(self._cooldown_key(tier_key))

    def _cooldown_tier(self, country: str, tier_key: str, reason: str = "") -> None:
        """Exhaust a tier for this account and cool it down for other tasks."""
        self._exhaust_tier(country, tier_key)
        if self.tier_cooldown_seconds <= 0:
            return
        remaining = _set_cooldown(
            self._cooldown_key(tier_key),
            self.tier_cooldown_seconds,
        )
        if remaining <= 0:
            return
        minutes = max(1, int(round(remaining / 60.0)))
        suffix = f"，原因={reason}" if reason else ""
        self.log(f"档位 {tier_key} 进入冷却 {minutes} 分钟{suffix}")

    @staticmethod
    def _is_no_numbers_error(reason: str) -> bool:
        text = str(reason or "").strip().lower()
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                "no_numbers",
                "no numbers",
                "no available numbers",
                "out of stock",
                "sold out",
                "暂无可用号码",
                "当前无可用号码",
                "没有可用号码",
                "库存为空",
            )
        )

    def _exhaust_tier(self, country: str, tier_key: str) -> None:
        country_key = str(country or "").strip()
        self._exhausted_tiers.add(str(tier_key or self._current_tier_key(country_key)))
        if not self.provider_ids_by_country.get(country_key):
            self._exhausted_countries.add(country_key)

    def _advance_country(self, reason: str = "") -> None:
        if not self.country_candidates:
            return
        prev = self.country_candidates[self._country_cursor % len(self.country_candidates)]
        self._country_cursor = (self._country_cursor + 1) % len(self.country_candidates)
        nxt = self.country_candidates[self._country_cursor]
        suffix = f" ({reason})" if reason else ""
        self.log(f"候选国家切换: {prev} -> {nxt}{suffix}")

    def _get_number_for_country(self, provider: BaseSmsProvider, country: str) -> SmsActivation:
        country_key = str(country or "").strip()
        if country_key in self._exhausted_countries:
            raise RuntimeError(f"国家 {country_key} 已在本账号流程中用尽")
        provider_ids = self.provider_ids_by_country.get(country_key, [])
        attempts = len(provider_ids) if provider_ids else 1
        wait_seconds = max(_safe_int(self.config.get("sms_no_numbers_wait_seconds"), 0), 0)
        wait_seconds = min(wait_seconds, 10 * 60)
        retry_interval = min(max(_safe_int(self.config.get("sms_no_numbers_retry_interval_seconds"), 20), 5), 60)
        should_wait = wait_seconds > 0 and len(self.country_candidates or [country_key]) <= 1
        deadline = time.monotonic() + wait_seconds
        no_number_tiers: set[str] = set()

        while True:
            last_error: Exception | None = None
            for provider_attempt in range(attempts):
                tier_key = self._current_tier_key(country_key)
                remaining_cooldown = self._tier_cooldown_remaining(tier_key)
                if remaining_cooldown > 0:
                    self._exhausted_tiers.add(tier_key)
                    self.log(
                        f"档位 {tier_key} 处于冷却中，跳过（剩余约 "
                        f"{max(1, int((remaining_cooldown + 59) // 60))} 分钟）"
                    )
                    if provider_ids:
                        self._advance_provider_for_country(country_key)
                    continue
                if provider_ids and tier_key in self._exhausted_tiers:
                    self._advance_provider_for_country(country_key)
                    continue
                self._configure_provider_for_country(provider, country_key)
                if provider_ids:
                    cursor = self._provider_cursor_by_country.get(country_key, 0) % len(provider_ids)
                    self.log(
                        f"租号档位: country={country_key} provider={provider_ids[cursor]} "
                        f"({cursor + 1}/{len(provider_ids)})"
                    )
                try:
                    return provider.get_number(service=self.service, country=country)
                except Exception as exc:
                    last_error = exc
                    if self._is_no_numbers_error(str(exc)):
                        # Preserve the configured short inventory retry window.
                        # The long shared cooldown is applied only after that
                        # window is exhausted, or immediately when no wait was
                        # requested / other countries are available.
                        no_number_tiers.add(tier_key)
                    if provider_ids and provider_attempt + 1 < attempts:
                        if self._is_no_numbers_error(str(exc)):
                            # A different selected Provider is available right
                            # now, so this tier is explicitly sold out. Cool it
                            # immediately; the short inventory wait is reserved
                            # for the final remaining tier.
                            self._cooldown_tier(country_key, tier_key, "暂无库存")
                            no_number_tiers.discard(tier_key)
                        self._advance_provider_for_country(country_key)
                        continue
                    break

            if provider_ids and last_error is None:
                # Every configured tier for this country is either exhausted
                # in this account flow or still inside the shared cooldown.
                # Mark the country itself exhausted so the outer candidate
                # loop will move on instead of re-entering the same country
                # on the next phone attempt.
                self._exhausted_countries.add(country_key)
                self.log(
                    f"国家 {country_key} 的已选 Provider 档位已全部用尽，"
                    "当前账号流程跳过该国家"
                )
                raise RuntimeError(f"国家 {country_key} 的已选 Provider 档位已在本账号流程中用尽")
            error_text = str(last_error or "")
            remaining = max(deadline - time.monotonic(), 0)
            if not should_wait or "NO_NUMBERS" not in error_text or remaining <= 0:
                for tier_key in no_number_tiers:
                    self._cooldown_tier(country_key, tier_key, "暂无库存")
                raise last_error or RuntimeError("所选 Provider 档位均无可用号码")

            delay = min(float(retry_interval), remaining)
            self.log(
                f"国家 {country_key} 的已选 Provider 暂无号码，"
                f"{int(delay)} 秒后重试（最多再等 {int(remaining)} 秒）"
            )
            wait_deadline = time.monotonic() + delay
            while time.monotonic() < wait_deadline:
                if self._cancel_check():
                    raise RuntimeError("任务已取消")
                time.sleep(min(1.0, max(wait_deadline - time.monotonic(), 0)))

    def _smsbower_fallback_countries(
        self,
        provider: BaseSmsProvider,
        attempted_countries: list[str],
    ) -> list[str]:
        if not isinstance(provider, SmsBowerProvider):
            return []
        if self.country_candidates:
            return []
        auto_select = _safe_bool(self.config.get("smsbower_auto_country"), False)
        if not auto_select:
            return []
        min_stock = max(_safe_int(self.config.get("smsbower_auto_country_min_stock"), 1), 0)
        max_price = _safe_float(
            self.config.get("smsbower_auto_country_max_price")
            or self.config.get("smsbower_max_price")
            or self.config.get("sms_max_price"),
            0,
        )
        attempted = {str(item).strip() for item in attempted_countries if str(item).strip()}
        fallback: list[str] = []
        for row in provider.get_top_countries(service=self.service):
            country = str(row.get("country") or "").strip()
            price = _safe_float(row.get("price"), -1)
            stock = max(_safe_int(row.get("count"), 0), 0)
            if not country or country in attempted or stock < min_stock:
                continue
            if not self.allow_virtual_countries and country in self.virtual_country_ids:
                continue
            if max_price > 0 and (price < 0 or price > max_price):
                continue
            fallback.append(country)
            if len(fallback) >= 8:
                break
        if fallback:
            # Provider IDs are scoped to the country selected in the UI.  A
            # fallback country must let SMSBower choose from its own partners.
            provider.provider_ids = ""
            self.log(
                "所选国家即时库存已售罄，按价格上限补充备用国家: "
                + ",".join(fallback)
            )
        return fallback

    def __call__(self) -> str:
        provider = self._provider()
        if self.phase == "need_number":
            if (
                isinstance(provider, SmsBowerProvider)
                and not self.allow_virtual_countries
                and (
                    self.only_virtual_country_pool
                    or (
                        not self.country_candidates
                        and str(self.country or "").strip() in self.virtual_country_ids
                    )
                )
            ):
                raise RuntimeError("所选国家属于虚拟/VOIP号码国家，已停止租号")
            # A reused activation must be consumed serially so SMS events do not
            # cross accounts.  One-shot activations are independent and may wait
            # for their codes concurrently without entering the reuse lock.
            if (
                isinstance(provider, HeroSmsProvider)
                and bool(getattr(provider, "reuse_phone_to_max", True))
                and not self._verify_lock_acquired
            ):
                lock_wait_started = time.monotonic()
                _HERO_SMS_VERIFY_LOCK.acquire()
                self._verify_lock_acquired = True
                lock_wait_seconds = time.monotonic() - lock_wait_started
                if lock_wait_seconds >= 0.05:
                    self.log(
                        "接码复用锁等待: "
                        f"{lock_wait_seconds:.2f}s；当前 provider 开启了同号复用串行保护"
                    )

            # Explicit country pools rotate after a rejected/risked number. They
            # take precedence over automatic single-country selection.
            effective_country = self.country
            if self.country_candidates:
                effective_country = self.country_candidates[self._country_cursor % len(self.country_candidates)]
            auto_select = _safe_bool(self.config.get("herosms_auto_country") or self.config.get("smsbower_auto_country"), False)
            if auto_select and not self.country_candidates and isinstance(provider, HeroSmsProvider):
                self.log("正在查询最优国家（价格最低 + 库存充足）...")
                try:
                    min_stock = _safe_int(self.config.get("herosms_auto_country_min_stock") or self.config.get("smsbower_auto_country_min_stock"), 20)
                    max_price_limit = _safe_float(self.config.get("herosms_auto_country_max_price") or self.config.get("smsbower_auto_country_max_price"), 0)
                    best = provider.get_best_country(
                        service=self.service,
                        min_stock=min_stock,
                        max_price=max_price_limit,
                    )
                    if (
                        best
                        and isinstance(provider, SmsBowerProvider)
                        and not self.allow_virtual_countries
                        and str(best).strip() in self.virtual_country_ids
                    ):
                        self.log(f"自动选国结果 {best} 属于虚拟/VOIP号码国家，继续使用实体号码国家")
                        best = ""
                    if best:
                        self.log(f"自动选择最优国家: {best}")
                        effective_country = best
                    else:
                        self.log("未找到满足条件的国家，使用默认配置")
                except Exception as exc:
                    self.log(f"智能国家选择失败({exc})，使用默认配置")

            country_label = effective_country or self.config.get("sms_country") or self.config.get("sms_activate_country") or "default"
            self.log(f"已进入 add_phone，准备租用手机号: provider={self.provider_key} service={self.service} country={country_label}")
            self.log(f"正在从 {self.provider_key} 获取手机号...")
            try:
                acquisition_countries = self.country_candidates or [effective_country]
                acquisition_error = None
                attempted_countries: list[str] = []
                for country_attempt in range(len(acquisition_countries)):
                    candidate = acquisition_countries[
                        (self._country_cursor + country_attempt) % len(acquisition_countries)
                    ] if self.country_candidates else effective_country
                    attempted_countries.append(str(candidate or ""))
                    try:
                        self.activation = self._get_number_for_country(provider, str(candidate or ""))
                        effective_country = candidate
                        if self.country_candidates:
                            self._country_cursor = (
                                self._country_cursor + country_attempt
                            ) % len(self.country_candidates)
                        break
                    except Exception as exc:
                        acquisition_error = exc
                        if self.country_candidates:
                            self.log(f"国家 {candidate} 暂无可用号码，尝试下一个候选国家")
                if self.activation is None:
                    fallback_countries = self._smsbower_fallback_countries(
                        provider,
                        attempted_countries,
                    )
                    if fallback_countries:
                        self.country_candidates = list(dict.fromkeys([
                            *self.country_candidates,
                            *fallback_countries,
                        ]))
                        for candidate in fallback_countries:
                            try:
                                self.activation = self._get_number_for_country(provider, candidate)
                                effective_country = candidate
                                self._country_cursor = self.country_candidates.index(candidate)
                                break
                            except Exception as exc:
                                acquisition_error = exc
                                self.log(f"备用国家 {candidate} 暂无可用号码，继续切换")
                if self.activation is None:
                    if self.country_candidates and len(set(attempted_countries)) >= len(set(self.country_candidates)):
                        attempted_label = ",".join(
                            dict.fromkeys(str(item).strip() for item in attempted_countries if str(item).strip())
                        )
                        last_reason = str(acquisition_error or "未知原因")[:240]
                        raise RuntimeError(
                            "候选国家池本次已全部尝试但没有可用号码: "
                            f"{attempted_label or '-'}; 最后原因: {last_reason}"
                        )
                    raise acquisition_error or RuntimeError("候选国家均无可用号码")
            except Exception as first_exc:
                # 如果是自动选择的国家失败了，回退到默认国家重试
                fallback_country = self.country or self.config.get("sms_country") or self.config.get("herosms_country") or ""
                if auto_select and effective_country != fallback_country and fallback_country:
                    self.log(f"自动选择的国家({effective_country})获取号码失败，回退到默认国家({fallback_country})...")
                    try:
                        self.activation = self._get_number_for_country(provider, fallback_country)
                    except Exception:
                        if self._verify_lock_acquired:
                            _HERO_SMS_VERIFY_LOCK.release()
                            self._verify_lock_acquired = False
                        raise
                else:
                    if self._verify_lock_acquired:
                        _HERO_SMS_VERIFY_LOCK.release()
                        self._verify_lock_acquired = False
                    raise
            self.phase = "need_code"
            self.activation.country = effective_country
            reused = bool((self.activation.metadata or {}).get("reused"))
            reuse_label = "复用号码" if reused else "新号码"
            self.log(
                f"已成功租到号码({reuse_label}): {_mask_phone_number(self.activation.phone_number)} "
                f"(activation_id={_mask_activation_id(self.activation.activation_id)})"
            )
            return self.activation.phone_number

        if self.phase == "need_code" and self.activation:
            self.log(f"等待短信验证码... (activation_id={_mask_activation_id(self.activation.activation_id)})")
            code_timeout = min(max(
                _safe_int(self.config.get("sms_code_timeout_seconds"), 180),
                60,
            ), 300)
            self.log(f"本号码最多等待 {code_timeout} 秒，超时后自动取消并换号")
            code = provider.get_code(self.activation.activation_id, timeout=code_timeout)
            if code:
                self.log("已收到短信验证码")
                if getattr(provider, "auto_report_success_on_code", True):
                    self.report_success()
                else:
                    self.awaiting_external_success = True
            else:
                self.log(f"⚠️ 未收到验证码: activation_id={_mask_activation_id(self.activation.activation_id)}")
            return code
        return ""

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        if self.provider is not None:
            self.provider.set_resend_callback(callback)
        else:
            original_provider = self._provider()
            original_provider.set_resend_callback(callback)

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_code_failed", None)
            if callable(hook):
                hook(self.activation.activation_id, reason=reason)
            self.phase = "need_code"
            self.awaiting_external_success = False

    def mark_send_failed(self, reason: str = "") -> None:
        """Release current number and decide whether to stay on same price tier.

        Delivery failures keep the same country+provider until
        ``sms_same_tier_retries`` is reached. A number reported as already in
        use is handled separately: the current tier is skipped immediately,
        since replacing the number in that tier is unlikely to help.
        """
        if not (self.activation and self.provider):
            return
        country = str(self.activation.country or "").strip()
        hook = getattr(self.provider, "mark_send_failed", None)
        if callable(hook):
            hook(self.activation.activation_id, reason=reason)
        self.awaiting_external_success = False

        reason_l = str(reason or "").lower()
        hard_skip_country = any(
            token in reason_l
            for token in (
                "voip_phone_disallowed",
                "phone_country_pool_rejected",
                "phone numbers similar",
                "suspicious behavior from phone",
            )
        )
        tier_key = self._tier_key(country)

        if self._is_number_in_use_failure(reason):
            provider_ids = self.provider_ids_by_country.get(country, [])
            old_cursor = self._provider_cursor_by_country.get(country, 0)
            self._same_tier_fail_count[tier_key] = self.same_tier_max_retries
            self._cooldown_tier(country, tier_key, "号码已被使用")
            if len(provider_ids) > 1:
                self._advance_provider_for_country(country)
                new_cursor = self._provider_cursor_by_country.get(country, 0)
                self.log(
                    f"档位 {tier_key} 返回已使用号码，跳过当前 Provider"
                )
                if (
                    self.country_candidates
                    and old_cursor >= len(provider_ids) - 1
                    and new_cursor == 0
                ):
                    self._advance_country("当前国家全部 Provider 均返回已使用号码")
                return
            if self.country_candidates:
                self.log(f"档位 {tier_key} 返回已使用号码，跳过当前档位")
                self._advance_country("号码已被使用")
            else:
                self.log(
                    f"档位 {tier_key} 返回已使用号码，但没有其他候选档位；"
                    "继续时将受任务最大尝试次数限制"
                )
            return

        self._same_tier_fail_count[tier_key] = min(
            self._same_tier_fail_count.get(tier_key, 0) + 1,
            self.same_tier_max_retries,
        )
        fails = self._same_tier_fail_count[tier_key]
        max_same = self.same_tier_max_retries

        if hard_skip_country:
            self._same_tier_fail_count[tier_key] = max_same
            self._cooldown_tier(country, tier_key, "目标服务硬失败")
            provider_ids = self.provider_ids_by_country.get(country, [])
            old_cursor = self._provider_cursor_by_country.get(country, 0)
            if len(provider_ids) > 1:
                self.log(
                    f"档位 {tier_key} 硬失败，跳过当前 Provider: {str(reason or '')[:140]}"
                )
                self._advance_provider_for_country(country)
                new_cursor = self._provider_cursor_by_country.get(country, 0)
                if (
                    self.country_candidates
                    and old_cursor >= len(provider_ids) - 1
                    and new_cursor == 0
                ):
                    self._advance_country("当前国家全部 Provider 均硬失败")
            else:
                self.log(
                    f"档位 {tier_key} 硬失败，跳过该国家: {str(reason or '')[:140]}"
                )
                if self.country_candidates:
                    self._advance_country("硬失败")
            return

        if fails < max_same:
            self.log(
                f"同档位继续换号: {tier_key} 第 {fails}/{max_same} 次失败，"
                f"保持当前 Provider 再租新号"
            )
            return

        # Exhausted this provider tier — climb within country, then country pool.
        provider_ids = self.provider_ids_by_country.get(country, [])
        old_cursor = self._provider_cursor_by_country.get(country, 0)
        self.log(
            f"同档位 {tier_key} 已连续失败 {fails} 次（上限 {max_same}），切换下一 Provider"
        )
        self._cooldown_tier(country, tier_key, "同档位连续失败")
        self._advance_provider_for_country(country)
        new_cursor = self._provider_cursor_by_country.get(country, 0)

        if not self.country_candidates:
            return
        if len(provider_ids) <= 1:
            self._advance_country("单档位用尽")
            return
        # After last provider in the ladder, cursor wraps to 0 → next country.
        if old_cursor >= len(provider_ids) - 1 and new_cursor == 0:
            self._advance_country("全部 Provider 已轮询")

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            hook = getattr(self.provider, "mark_send_succeeded", None)
            if callable(hook):
                hook(self.activation.activation_id)

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            self.provider.report_success(self.activation.activation_id)
            self.completed = True
            self.phase = "done"
            self.awaiting_external_success = False
            self.log(
                "短信验证成功，已标记号码完成使用: "
                f"activation_id={_mask_activation_id(self.activation.activation_id)}"
            )
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False

    def cleanup(self) -> None:
        activation = self.activation
        self.activation = None
        if activation and not self.completed:
            try:
                provider = self._provider()
                provider.cancel(activation.activation_id)
                self.log(
                    "已释放未完成验证的号码: "
                    f"activation_id={_mask_activation_id(activation.activation_id)}"
                )
            except Exception:
                pass
        if self._verify_lock_acquired:
            _HERO_SMS_VERIFY_LOCK.release()
            self._verify_lock_acquired = False


def create_phone_callbacks(
    provider_key: str,
    config: dict,
    *,
    service: str,
    country: str = "",
    log_fn=None,
) -> tuple:
    """Create (phone_callback, cleanup) tuple for browser registration."""
    controller = PhoneCallbackController(
        provider_key,
        config,
        service=service,
        country=country,
        log_fn=log_fn,
    )
    return controller, controller.cleanup
