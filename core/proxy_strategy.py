"""Unified proxy selection with explicit four-tier priority.

Historically proxy selection was spread across ``ProxyPool`` (dynamic provider
plus the static database pool) and a two-branch helper in the task executor.
That made it impossible to answer "which route did this registration actually
use, and why" from a failure record.  This module makes the priority order
explicit, auditable and configurable.

Tiers (highest priority first)
------------------------------
1. ``account``        - a proxy bound to the specific account/identity.
2. ``stable_runtime`` - the configured rotating/stable proxy provider runtime.
3. ``pool``           - a row from the static proxy pool in the database.
4. ``explicit``       - a proxy passed with the individual task request.
5. ``legacy_global``  - the deprecated single global proxy setting.

The default order keeps the historical effective precedence
(``account > explicit > stable_runtime > pool > legacy_global``) so existing
deployments do not change behaviour.  Set ``ZCJ_PROXY_TIER_ORDER`` (or pass
``tier_order``) to ``account,stable_runtime,pool,explicit,legacy_global`` to
adopt the stricter upstream ordering, where a configured stable runtime wins
over a per-task override.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Sequence

from .proxy_utils import normalize_proxy_url

TIER_ACCOUNT = "account"
TIER_STABLE_RUNTIME = "stable_runtime"
TIER_POOL = "pool"
TIER_EXPLICIT = "explicit"
TIER_LEGACY_GLOBAL = "legacy_global"
TIER_DIRECT = "direct"

KNOWN_TIERS = (
    TIER_ACCOUNT,
    TIER_STABLE_RUNTIME,
    TIER_POOL,
    TIER_EXPLICIT,
    TIER_LEGACY_GLOBAL,
)

# Backwards-compatible: explicit task proxy still beats the pool.
DEFAULT_TIER_ORDER: tuple[str, ...] = (
    TIER_ACCOUNT,
    TIER_EXPLICIT,
    TIER_STABLE_RUNTIME,
    TIER_POOL,
    TIER_LEGACY_GLOBAL,
)

# Upstream-style: a configured stable runtime wins over a per-task override.
STRICT_TIER_ORDER: tuple[str, ...] = (
    TIER_ACCOUNT,
    TIER_STABLE_RUNTIME,
    TIER_POOL,
    TIER_EXPLICIT,
    TIER_LEGACY_GLOBAL,
)

TierOrderEnv = "ZCJ_PROXY_TIER_ORDER"
GlobalProxyEnv = "ZCJ_GLOBAL_PROXY"
GLOBAL_PROXY_CONFIG_KEY = "proxy_global_url"


@dataclass(frozen=True)
class ProxyDecision:
    """Result of a proxy resolution pass, safe to write into task logs."""

    proxy: str | None
    tier: str
    source: str
    region: str = ""
    order: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()

    @property
    def used_proxy(self) -> bool:
        return bool(self.proxy)

    def log_fields(self) -> dict[str, str]:
        return {
            "proxy_tier": self.tier,
            "proxy_source": self.source,
            "proxy_region": self.region,
        }


def parse_tier_order(raw: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated tier order, rejecting unknown tier names."""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    parts = tuple(part.strip() for part in text.split(",") if part.strip())
    if not parts or any(part not in KNOWN_TIERS for part in parts):
        return None
    return parts


def configured_tier_order() -> tuple[str, ...]:
    return parse_tier_order(os.getenv(TierOrderEnv)) or DEFAULT_TIER_ORDER


def stable_runtime_proxy(extra: dict | None = None) -> str | None:
    """Return a route from the configured proxy provider runtime, if any."""
    try:
        from .proxy_providers import get_dynamic_proxy

        return normalize_proxy_url(get_dynamic_proxy(extra)) or None
    except Exception:
        return None


def legacy_global_proxy() -> str | None:
    """Return the deprecated single global proxy, if one is configured."""
    raw = str(os.getenv(GlobalProxyEnv, "") or "").strip()
    if not raw:
        try:
            from .config_store import config_store

            raw = str(config_store.get(GLOBAL_PROXY_CONFIG_KEY, "") or "").strip()
        except Exception:
            raw = ""
    return normalize_proxy_url(raw) or None


def _unpack_tier(value: object, default_tier: str) -> tuple[object, str]:
    """Accept either a plain route or ``(route, tier)`` from a tier getter."""
    if isinstance(value, tuple) and len(value) == 2:
        return value[0], str(value[1] or default_tier)
    return value, default_tier


def resolve_proxy(
    *,
    account_proxy: str | None = None,
    explicit_proxy: str | None = None,
    stable_runtime_getter: Callable[[], str | None] | None = None,
    pool_getter: Callable[[], str | None] | None = None,
    legacy_global_getter: Callable[[], str | None] | None = None,
    region: str = "",
    allow_pool: bool = True,
    tier_order: Sequence[str] | None = None,
    normalize: Callable[[str | None], str] | None = None,
) -> ProxyDecision:
    """Resolve the proxy for one registration using the tier order."""
    order = tuple(tier_order or configured_tier_order())
    normalize_fn = normalize or normalize_proxy_url
    getters: dict[str, Callable[[], str | None] | None] = {
        TIER_ACCOUNT: (lambda: account_proxy) if account_proxy else None,
        TIER_EXPLICIT: (lambda: explicit_proxy) if explicit_proxy else None,
        TIER_STABLE_RUNTIME: stable_runtime_getter,
        TIER_POOL: pool_getter if allow_pool else None,
        TIER_LEGACY_GLOBAL: legacy_global_getter or legacy_global_proxy,
    }
    considered: list[str] = []
    for tier in order:
        considered.append(tier)
        getter = getters.get(tier)
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:
            value = None
        value, resolved_tier = _unpack_tier(value, tier)
        if not value:
            continue
        normalized = normalize_fn(value) or ""
        if normalized:
            return ProxyDecision(
                proxy=normalized,
                tier=resolved_tier,
                source=_TIER_LABELS.get(resolved_tier, resolved_tier),
                region=str(region or "").strip().upper(),
                order=order,
                candidates=tuple(considered),
            )
    return ProxyDecision(
        proxy=None,
        tier=TIER_DIRECT,
        source="direct",
        region=str(region or "").strip().upper(),
        order=order,
        candidates=tuple(considered),
    )


_TIER_LABELS = {
    TIER_ACCOUNT: "账号绑定代理",
    TIER_STABLE_RUNTIME: "稳定代理运行时",
    TIER_POOL: "静态代理池",
    TIER_EXPLICIT: "任务显式代理",
    TIER_LEGACY_GLOBAL: "旧版全局代理",
    TIER_DIRECT: "直连",
}


def describe_decision(decision: ProxyDecision) -> str:
    """Return a credential-free, human-readable summary for task logs."""
    if not decision.used_proxy:
        return "未使用代理（直连）"
    return f"代理来源: {decision.source}（{decision.tier}）"