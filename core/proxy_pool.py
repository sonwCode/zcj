"""Proxy pool backed by the local database."""
from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from typing import Optional
import threading

from sqlmodel import Session, select

from .db import ProxyModel, engine
from .proxy_utils import normalize_proxy_url, redact_proxy_credentials

_PROXY_CHECK_URLS = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/ip",
    "https://httpbin.org/ip",
)


def _proxy_vendor(proxy_url: str) -> str:
    try:
        from urllib.parse import urlsplit

        host = str(urlsplit(normalize_proxy_url(proxy_url)).hostname or "").lower()
    except ValueError:
        return ""
    return "711Proxy" if host == "711proxy.com" or host.endswith(".711proxy.com") else ""


def _proxy_error_code(errors: list[str]) -> str:
    detail = " ".join(errors).lower()
    if "407" in detail or "proxy authentication required" in detail:
        return "authentication_rejected"
    if "remotedisconnected" in detail or "closed connection without response" in detail:
        return "authentication_session_closed"
    if "timed out" in detail or "timeout" in detail:
        return "connection_timeout"
    if "name or service not known" in detail or "getaddrinfo failed" in detail:
        return "dns_failed"
    return "connectivity_failed"


def _summarize_proxy_errors(errors: list[str], proxy_url: str = "") -> str:
    code = _proxy_error_code(errors)
    vendor = _proxy_vendor(proxy_url)
    if code == "authentication_rejected":
        return "代理服务器返回 407 并拒绝认证，请核对用户名、密码和认证方式"
    if code == "authentication_session_closed" and vendor == "711Proxy":
        return "711Proxy 网关端口可连接，但拒绝了当前账号认证会话；导入格式已通过，请核对账号密码、套餐端口和供应商网关状态"
    if code == "authentication_session_closed":
        return "代理端口可连接，但服务端主动关闭了认证会话；请检查账号密码、IP 白名单、套餐余额/到期状态和代理协议"
    if code == "connection_timeout":
        return "代理连接超时，请检查节点在线状态、网络出口和代理协议"
    if code == "dns_failed":
        return "代理域名解析失败，请检查代理主机地址"
    return "代理未通过连通性检测，请检查节点配置和供应商状态"


def _proxy_check_suggestions(proxy_url: str, error_code: str) -> list[str]:
    if _proxy_vendor(proxy_url) != "711Proxy":
        return []
    if error_code not in {"authentication_rejected", "authentication_session_closed"}:
        return []
    try:
        from urllib.parse import urlsplit

        port = urlsplit(normalize_proxy_url(proxy_url)).port
    except ValueError:
        port = None
    product = {
        10000: "Residential Proxies - GB",
        20000: "Residential Proxies - IP",
        12000: "Unlimited Proxies",
        22000: "SOCKS5 Proxies",
        30000: "Static Residential Proxies",
    }.get(port, "当前套餐")
    return [
        f"当前端口 {port or '-'} 对应 {product}，确认它与已购买套餐一致",
        "在 711Proxy 控制台重新复制当前主账号或 Sub-user 的用户名和密码",
        "检查套餐余额、到期状态；若刚重置密码，请编辑后重新检测",
        "优先复制 711Proxy Proxy Setup 页面生成的 Testing Command 或 Proxy List",
    ]


class ProxyPool:
    def __init__(self):
        self._sequence = 0
        self._lock = threading.RLock()
        self._lease_counts: dict[str, int] = {}
        self._last_assigned: dict[str, int] = {}
        self._failure_streaks: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

    @staticmethod
    def _proxy_key(url: str | None) -> str:
        return normalize_proxy_url(url) or str(url or "").strip()

    @staticmethod
    def _load_active(region: str = "") -> list[ProxyModel]:
        with Session(engine) as session:
            all_active = session.exec(
                select(ProxyModel).where(ProxyModel.is_active == True)  # noqa: E712
            ).all()
        if not all_active:
            return []
        normalized_region = str(region or "").strip().upper()
        if normalized_region:
            preferred = [
                item
                for item in all_active
                if str(item.region or "").strip().upper() == normalized_region
            ]
            if preferred:
                return preferred
        return list(all_active)

    def _remember_assignment(self, url: str, *, reserve: bool) -> None:
        key = self._proxy_key(url)
        if not key:
            return
        self._sequence += 1
        self._last_assigned[key] = self._sequence
        if reserve:
            self._lease_counts[key] = self._lease_counts.get(key, 0) + 1

    def _select_static(self, region: str = "", *, reserve: bool) -> Optional[str]:
        pool = self._load_active(region)
        if not pool:
            return None

        with self._lock:
            now = monotonic()
            ready = [
                item
                for item in pool
                if self._cooldown_until.get(self._proxy_key(item.url), 0.0) <= now
            ]
            # If every active entry is cooling down, keep the task executable by
            # selecting the least-busy/oldest entry instead of silently falling
            # back to a direct connection.
            candidates = ready or pool
            min_leases = min(
                self._lease_counts.get(self._proxy_key(item.url), 0)
                for item in candidates
            )
            least_busy = [
                item
                for item in candidates
                if self._lease_counts.get(self._proxy_key(item.url), 0) == min_leases
            ]
            chosen = min(
                least_busy,
                key=lambda item: (
                    self._last_assigned.get(self._proxy_key(item.url), -1),
                    int(item.id or 0),
                ),
            )
            selected = self._proxy_key(chosen.url)
            self._remember_assignment(selected, reserve=reserve)
            return selected or chosen.url

    @staticmethod
    def _dynamic_proxy() -> str:
        try:
            from core.proxy_providers import get_dynamic_proxy

            return normalize_proxy_url(get_dynamic_proxy())
        except Exception:
            return ""

    def get_next(self, region: str = "") -> Optional[str]:
        """Return a fair next proxy without reserving it for long-running work."""
        dynamic = self._dynamic_proxy()
        if dynamic:
            with self._lock:
                if self._cooldown_until.get(self._proxy_key(dynamic), 0.0) <= monotonic():
                    self._remember_assignment(dynamic, reserve=False)
                    return dynamic
        return self._select_static(region, reserve=False)

    def acquire_next(self, region: str = "") -> Optional[str]:
        """Reserve one proxy, preferring an unused route for concurrent workers.

        A caller must pair a successful acquisition with :meth:`release`.  The
        least-busy, least-recently-assigned rule spreads a concurrent window
        across distinct imported rows before any row is reused.
        """
        dynamic = self._dynamic_proxy()
        if dynamic:
            key = self._proxy_key(dynamic)
            with self._lock:
                if (
                    self._cooldown_until.get(key, 0.0) <= monotonic()
                    and self._lease_counts.get(key, 0) == 0
                ):
                    self._remember_assignment(dynamic, reserve=True)
                    return dynamic

        selected = self._select_static(region, reserve=True)
        if selected:
            return selected

        # A rotating provider may legitimately return one shared gateway URL.
        # Reuse it only when no static entry exists.
        if dynamic:
            with self._lock:
                self._remember_assignment(dynamic, reserve=True)
            return dynamic
        return None

    def release(self, url: str | None) -> None:
        key = self._proxy_key(url)
        if not key:
            return
        with self._lock:
            current = self._lease_counts.get(key, 0)
            if current <= 1:
                self._lease_counts.pop(key, None)
            else:
                self._lease_counts[key] = current - 1

    def assignment_label(self, url: str | None) -> str:
        """Return a credential-free identifier suitable for task logs."""
        key = self._proxy_key(url)
        if not key:
            return ""
        with Session(engine) as session:
            proxy = session.exec(select(ProxyModel).where(ProxyModel.url == key)).first()
        return f"#{int(proxy.id)}" if proxy and proxy.id is not None else "dynamic"

    def report_success(self, url: str) -> None:
        key = self._proxy_key(url)
        with self._lock:
            self._failure_streaks.pop(key, None)
            self._cooldown_until.pop(key, None)
        self._report(url, success=True)

    def report_fail(self, url: str) -> None:
        key = self._proxy_key(url)
        with self._lock:
            streak = self._failure_streaks.get(key, 0) + 1
            self._failure_streaks[key] = streak
            cooldown_seconds = min(60 * (2 ** (streak - 1)), 15 * 60)
            self._cooldown_until[key] = monotonic() + cooldown_seconds
        self._report(url, success=False)

    def _report(self, url: str, *, success: bool) -> None:
        normalized = normalize_proxy_url(url)
        # Serialize counter updates so concurrent workers cannot overwrite one
        # another's read-modify-write result in SQLite.
        with self._lock:
            with Session(engine) as session:
                proxy = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
                if not proxy and normalized and normalized != url:
                    proxy = session.exec(select(ProxyModel).where(ProxyModel.url == normalized)).first()
                if not proxy:
                    return
                if success:
                    proxy.success_count = int(proxy.success_count or 0) + 1
                else:
                    proxy.fail_count = int(proxy.fail_count or 0) + 1
                    if proxy.success_count == 0 and proxy.fail_count >= 5:
                        proxy.is_active = False
                proxy.last_checked = datetime.now(timezone.utc)
                session.add(proxy)
                session.commit()

    def check_all(self) -> dict:
        """Check all saved proxies with multiple public IP endpoints to reduce false failures."""
        with Session(engine) as session:
            proxies = session.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0}
        for proxy in proxies:
            if self._check_one(proxy.url):
                self.report_success(proxy.url)
                results["ok"] += 1
            else:
                self.report_fail(proxy.url)
                results["fail"] += 1
        return results

    def check_one(self, url: str) -> dict:
        """Check one proxy and persist its success/failure counters."""
        result = self._check_one_detail(url)
        if result.get("ok"):
            self.report_success(url)
        else:
            self.report_fail(url)
        return result

    @staticmethod
    def _check_one(url: str) -> bool:
        return bool(ProxyPool._check_one_detail(url).get("ok"))

    @staticmethod
    def _check_one_detail(url: str) -> dict:
        import requests

        proxy_url = normalize_proxy_url(url)
        if not proxy_url:
            return {"ok": False, "proxy": "", "error": "代理地址为空或格式无法识别"}
        proxy_config = {"http": proxy_url, "https": proxy_url}
        headers = {"User-Agent": "Mozilla/5.0 proxy-check"}
        errors: list[str] = []
        for check_url in _PROXY_CHECK_URLS:
            try:
                response = requests.get(check_url, proxies=proxy_config, timeout=10, headers=headers)
                if 200 <= response.status_code < 300 and response.text.strip():
                    return {
                        "ok": True,
                        "proxy": proxy_url,
                        "check_url": check_url,
                        "status_code": response.status_code,
                        "body": response.text.strip()[:300],
                    }
                errors.append(f"{check_url}: HTTP {response.status_code}")
            except Exception as exc:
                detail = redact_proxy_credentials(str(exc), proxy_url)
                errors.append(f"{check_url}: {type(exc).__name__}: {detail[:240]}")
        return {
            "ok": False,
            "proxy": proxy_url,
            "provider": _proxy_vendor(proxy_url),
            "error_code": _proxy_error_code(errors) if errors else "connectivity_failed",
            "error": _summarize_proxy_errors(errors, proxy_url) if errors else "所有探测站均失败",
            "suggestions": _proxy_check_suggestions(
                proxy_url,
                _proxy_error_code(errors) if errors else "connectivity_failed",
            ),
            "detail": "; ".join(errors[-3:]) if errors else "",
        }


proxy_pool = ProxyPool()
