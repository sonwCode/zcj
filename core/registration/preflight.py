"""Cheap preflight checks run before registration enters procurement.

A registration spends money (proxy, mailbox, SMS) before it discovers a broken
runtime.  These checks fail fast and locally instead: they verify the
interpreter, the HTTP impersonation library version, the browser automation
stack and the requested browser profile before any resource is leased.

Nothing in this module touches the network.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence

MIN_CURL_CFFI = (0, 6, 0)
MIN_PYTHON = (3, 10)
HTTP_LIBRARIES = ("curl_cffi", "httpx", "requests")
BROWSER_LIBRARIES = ("playwright", "patchright", "camoufox")


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "required": self.required,
        }


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    @property
    def failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.required and not check.ok)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "failures": [check.name for check in self.failures],
        }


class PreflightError(RuntimeError):
    code = "preflight_failed"

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        names = ", ".join(check.name for check in report.failures) or "unknown"
        super().__init__(f"注册前置检查未通过: {names}")


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text or "").replace("-", ".").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_python() -> PreflightCheck:
    current = sys.version_info[:3]
    ok = tuple(current[:2]) >= MIN_PYTHON
    return PreflightCheck(
        "python",
        ok,
        f"当前 {current[0]}.{current[1]}.{current[2]}，要求 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def check_curl_cffi() -> PreflightCheck:
    try:
        from importlib.metadata import version

        raw = version("curl_cffi")
    except Exception:
        return PreflightCheck("curl_cffi", False, "未安装 curl_cffi", required=False)
    ok = _version_tuple(raw)[:2] >= MIN_CURL_CFFI[:2]
    return PreflightCheck(
        "curl_cffi",
        ok,
        f"当前 {raw}，要求 >= {MIN_CURL_CFFI[0]}.{MIN_CURL_CFFI[1]}",
    )


def check_http_stack() -> PreflightCheck:
    available = [name for name in HTTP_LIBRARIES if importlib.util.find_spec(name) is not None]
    return PreflightCheck(
        "http_stack",
        bool(available),
        "可用: " + (", ".join(available) if available else "无"),
    )


def check_browser_stack() -> PreflightCheck:
    available = [name for name in BROWSER_LIBRARIES if importlib.util.find_spec(name) is not None]
    return PreflightCheck(
        "browser_stack",
        bool(available),
        "可用: " + (", ".join(available) if available else "无"),
        required=False,
    )


def check_browser_profile(profile: str) -> PreflightCheck:
    name = str(profile or "").strip()
    if not name:
        return PreflightCheck("browser_profile", True, "未指定浏览器指纹", required=False)
    backend_available = any(
        importlib.util.find_spec(module) is not None for module in BROWSER_LIBRARIES
    )
    return PreflightCheck(
        "browser_profile",
        backend_available,
        f"指纹 {name}；浏览器后端可用: {backend_available}",
        required=backend_available,
    )


def run_preflight(
    *,
    require_browser: bool = False,
    browser_profile: str = "",
    extra_checks: Sequence[PreflightCheck] = (),
) -> PreflightReport:
    checks: list[PreflightCheck] = [
        check_python(),
        check_curl_cffi(),
        check_http_stack(),
    ]
    if require_browser or browser_profile:
        browser = check_browser_stack()
        checks.append(browser if require_browser else PreflightCheck(
            browser.name, browser.ok, browser.detail, required=False
        ))
        checks.append(check_browser_profile(browser_profile))
    checks.extend(extra_checks)
    return PreflightReport(tuple(checks))


def assert_preflight(
    *,
    require_browser: bool = False,
    browser_profile: str = "",
    extra_checks: Iterable[PreflightCheck] = (),
) -> PreflightReport:
    report = run_preflight(
        require_browser=require_browser,
        browser_profile=browser_profile,
        extra_checks=tuple(extra_checks),
    )
    if not report.ok:
        raise PreflightError(report)
    return report