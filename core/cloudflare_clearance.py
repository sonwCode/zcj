"""Cloudflare clearance seam (FlareSolverr / WARP).

Many registration endpoints sit behind Cloudflare bot management.  Plain proxy
rotation cannot clear a JavaScript challenge, which is one of the largest
single sources of registration failure.  This module adds an optional
clearance provider that solves the challenge once and hands the resulting
cookies to the existing HTTP clients.

It is disabled by default: with no endpoint configured nothing changes.  When
enabled the provider talks to a FlareSolverr-compatible service, typically run
behind WARP + Privoxy so the solving egress is Cloudflare-friendly, and caches
the clearance cookie for its lifetime.

Configuration (environment wins, then the config store):
  ZCJ_CF_CLEARANCE_URL   FlareSolverr base URL, e.g. http://127.0.0.1:8191
  ZCJ_CF_CLEARANCE_TTL   cache lifetime in seconds (default 600)
  config keys            cf_clearance_url / cf_clearance_ttl
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

ENV_ENDPOINT = "ZCJ_CF_CLEARANCE_URL"
ENV_TTL = "ZCJ_CF_CLEARANCE_TTL"
CONFIG_ENDPOINT = "cf_clearance_url"
CONFIG_TTL = "cf_clearance_ttl"
DEFAULT_TTL = 600.0
COOKIE_NAME = "cf_clearance"


def _config(key: str, default: str = "") -> str:
    value = str(os.getenv(key.upper(), "") or "").strip()
    if value:
        return value
    try:
        from ..config_store import config_store

        return str(config_store.get(key, default) or "").strip()
    except Exception:
        return default


def configured_endpoint() -> str:
    return _config(CONFIG_ENDPOINT)


def configured_ttl() -> float:
    raw = str(os.getenv(ENV_TTL, "") or "").strip() or _config(CONFIG_TTL)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL
    return value if value > 0 else DEFAULT_TTL


def _domain(url: str) -> str:
    try:
        return str(urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


@dataclass
class ClearanceResult:
    domain: str
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    provider: str = ""
    solved_at: float = 0.0

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if k and v)

    def has_clearance_cookie(self) -> bool:
        return bool(self.cookies.get(COOKIE_NAME))

    def is_expired(self, ttl: float) -> bool:
        return (time.time() - float(self.solved_at or 0.0)) > float(ttl)


class ClearanceProvider(Protocol):
    name: str

    def fetch(self, url: str) -> "ClearanceResult | None":
        ...


class NoopProvider:
    """Used when no clearance endpoint is configured."""

    name = "none"

    def fetch(self, url: str) -> "ClearanceResult | None":
        return None


class FlareSolverrProvider:
    """Talk to a FlareSolverr-compatible /v1 endpoint."""

    name = "flaresolverr"

    def __init__(self, endpoint: str, timeout: float = 60.0) -> None:
        self.endpoint = str(endpoint or "").rstrip("/")
        self.timeout = float(timeout)

    def fetch(self, url: str) -> "ClearanceResult | None":
        if not self.endpoint:
            return None
        try:
            import requests

            response = requests.post(
                self.endpoint + "/v1",
                json={
                    "cmd": "request.get",
                    "url": str(url),
                    "maxTimeout": int(self.timeout * 1000),
                },
                timeout=self.timeout + 10,
            )
            payload: Any = response.json()
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return None
        solution = payload.get("solution") or {}
        cookies = {
            str(item.get("name")): str(item.get("value"))
            for item in (solution.get("cookies") or [])
            if item.get("name")
        }
        if not cookies:
            return None
        return ClearanceResult(
            domain=_domain(url),
            cookies=cookies,
            user_agent=str(solution.get("userAgent") or ""),
            provider=self.name,
            solved_at=time.time(),
        )


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, ClearanceResult] = {}

    def get(self, domain: str, ttl: float) -> "ClearanceResult | None":
        with self._lock:
            item = self._items.get(domain)
        if item and not item.is_expired(ttl):
            return item
        return None

    def put(self, result: ClearanceResult) -> None:
        with self._lock:
            self._items[result.domain] = result

    def domains(self) -> list[str]:
        with self._lock:
            return sorted(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_CACHE = _Cache()


def get_provider() -> ClearanceProvider:
    endpoint = configured_endpoint()
    if not endpoint:
        return NoopProvider()
    return FlareSolverrProvider(endpoint)


def get_clearance(url: str, *, force: bool = False) -> "ClearanceResult | None":
    domain = _domain(url)
    if not domain:
        return None
    ttl = configured_ttl()
    if not force:
        cached = _CACHE.get(domain, ttl)
        if cached is not None:
            return cached
    result = get_provider().fetch(url)
    if result is not None:
        _CACHE.put(result)
    return result


def apply_clearance(headers: dict, url: str) -> dict:
    """Merge a cached clearance cookie into a request header mapping."""
    result = get_clearance(url)
    if result is None:
        return dict(headers or {})
    merged = dict(headers or {})
    cookie = result.cookie_header()
    if cookie:
        existing = str(merged.get("Cookie") or "").strip()
        merged["Cookie"] = f"{existing}; {cookie}" if existing else cookie
    if result.user_agent and not merged.get("User-Agent"):
        merged["User-Agent"] = result.user_agent
    return merged


def clearance_status() -> dict:
    provider = get_provider()
    return {
        "enabled": not isinstance(provider, NoopProvider),
        "provider": provider.name,
        "endpoint": configured_endpoint(),
        "ttl": configured_ttl(),
        "cached_domains": _CACHE.domains(),
    }


def reset_cache() -> None:
    _CACHE.clear()