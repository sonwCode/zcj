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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

MIN_CURL_CFFI = (0, 6, 0)
MIN_PYTHON = (3, 10)
HTTP_LIBRARIES = ("curl_cffi", "httpx", "requests")
BROWSER_LIBRARIES = ("playwright", "patchright", "camoufox")

_MAILBOX_PROVIDER_ALIASES = {
    "local_ms": "local_ms_pool",
    "outlook_email": "outlook_email_api",
    "generic_http": "generic_http_mailbox",
    "tempmail_lol": "tempmail_lol_api",
    "tempmail_web": "tempmail_web_api",
    "duckmail": "duckmail_api",
    "freemail": "freemail_api",
    "moemail": "moemail_api",
    "cfworker": "cfworker_admin_api",
    "testmail": "testmail_api",
    "laoudo": "laoudo_api",
}
_TEMP_MAILBOX_PROVIDERS = {
    "generic_http_mailbox",
    "tempmail_lol_api",
    "tempmail_web_api",
    "duckmail_api",
    "ddg_email",
    "ddg_email_api",
    "freemail_api",
    "moemail_api",
    "cfworker_admin_api",
    "testmail_api",
    "laoudo_api",
}
_SMS_PROVIDER_ALIASES = {
    "herosms": "herosms_api",
    "smsbower": "smsbower_api",
    "sms_activate": "sms_activate_api",
}
_SMS_KEY_FIELDS = {
    "herosms_api": "herosms_api_key",
    "smsbower_api": "smsbower_api_key",
    "sms_activate_api": "sms_activate_api_key",
}


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


def check_sentinel() -> PreflightCheck:
    """Verify the Python Sentinel solver still yields a coherent payload.

    OpenAI ships new ``sdk.js`` builds regularly and ZCJ reimplements the SDK VM
    in Python, so a shape change breaks signup silently — and only after the proxy
    and mailbox have already been paid for. This check is offline and cheap.
    """
    try:
        from core.registration.sentinel_check import run_sentinel_self_test
    except Exception as exc:
        return PreflightCheck("sentinel", True, f"跳过: {exc}", required=False)
    report = run_sentinel_self_test()
    return PreflightCheck("sentinel", report.ok, report.summary())


def _clean_mapping(config: Mapping[str, object] | None) -> dict[str, object]:
    return dict(config) if isinstance(config, Mapping) else {}


def _mailbox_provider_key(provider: str) -> str:
    key = str(provider or "").strip().lower()
    return _MAILBOX_PROVIDER_ALIASES.get(key, key)


def check_mailbox_source(
    *,
    provider: str = "",
    email: str = "",
    config: Mapping[str, object] | None = None,
    required: bool = False,
) -> PreflightCheck:
    """Describe the configured mailbox consumer without leasing a mailbox."""
    values = _clean_mapping(config)
    provider_key = _mailbox_provider_key(
        provider or values.get("mail_provider") or values.get("mailbox_provider")
    )
    fixed_email = str(email or values.get("email") or "").strip()
    if fixed_email:
        ok = "@" in fixed_email and " " not in fixed_email
        detail = (
            "mode=consume_existing; source=fixed_email; "
            "mainstream_signup=absent; provisioning=unsupported"
        )
        if not ok:
            detail += "; error=invalid_email"
        return PreflightCheck("mailbox_resource", ok, detail, required=required)

    inline = str(
        values.get("chatgpt_api_mailbox_lines")
        or values.get("api_mailbox_lines")
        or ""
    ).strip()
    if inline:
        rows = [line.strip() for line in inline.splitlines() if line.strip()]
        usable = [
            row for row in rows
            if "@" in row.split("----", 1)[0] and "----" in row
        ]
        ok = bool(usable)
        detail = (
            "mode=consume_existing; source=inline_mailbox_pool; "
            f"rows={len(usable)}/{len(rows)}; mainstream_signup=absent; "
            "provisioning=unsupported"
        )
        return PreflightCheck("mailbox_resource", ok, detail, required=required)

    pool_text = str(values.get("local_ms_pool_text") or "").strip()
    pool_file = str(values.get("local_ms_pool_file") or "").strip()
    if pool_text or pool_file:
        file_ok = bool(pool_file and Path(pool_file).expanduser().is_file())
        ok = bool(pool_text) or file_ok
        source = "local_ms_pool_text" if pool_text else "local_ms_pool_file"
        detail = (
            "mode=consume_existing; source=" + source + "; "
            f"available={ok}; mainstream_signup=absent; provisioning=unsupported"
        )
        return PreflightCheck("mailbox_resource", ok, detail, required=required)

    if provider_key:
        if provider_key in _TEMP_MAILBOX_PROVIDERS:
            mode = "temporary_mailbox_service"
            provisioning = "temporary_only"
        else:
            mode = "consume_existing"
            provisioning = "unsupported"
        detail = (
            f"mode={mode}; provider={provider_key}; source=provider_settings; "
            f"mainstream_signup=absent; provisioning={provisioning}; "
            "credentials=deferred_to_provider_settings"
        )
        return PreflightCheck("mailbox_resource", True, detail, required=required)

    return PreflightCheck(
        "mailbox_resource",
        False,
        "mode=consume_existing; source=missing; mainstream_signup=absent; "
        "provisioning=unsupported; error=no_mailbox_source",
        required=required,
    )


def _sms_provider_key(provider: str) -> str:
    key = str(provider or "").strip().lower()
    return _SMS_PROVIDER_ALIASES.get(key, key)


def check_sms_source(
    *,
    provider: str = "",
    config: Mapping[str, object] | None = None,
    required: bool = False,
) -> PreflightCheck:
    """Check local SMS-provider configuration without making an API request."""
    values = _clean_mapping(config)
    provider_key = _sms_provider_key(
        provider or values.get("sms_provider") or values.get("phone_provider")
    )
    if not provider_key:
        return PreflightCheck(
            "sms_resource",
            False,
            "provider=missing; activation=unavailable; error=no_sms_provider",
            required=required,
        )

    key_field = _SMS_KEY_FIELDS.get(provider_key)
    if not key_field:
        return PreflightCheck(
            "sms_resource",
            False,
            f"provider={provider_key}; activation=unsupported; "
            "error=unknown_sms_provider",
            required=required,
        )

    configured = bool(str(values.get(key_field) or "").strip())
    detail = (
        f"provider={provider_key}; activation={'configured' if configured else 'unavailable'}; "
        f"api_key={'present' if configured else 'missing'}; network_probe=skipped"
    )
    return PreflightCheck("sms_resource", configured, detail, required=required)


def run_preflight(
    *,
    require_browser: bool = False,
    browser_profile: str = "",
    extra_checks: Sequence[PreflightCheck] = (),
    sentinel: bool = False,
    mailbox_provider: str = "",
    mailbox_email: str = "",
    mailbox_config: Mapping[str, object] | None = None,
    require_mailbox: bool = False,
    sms_provider: str = "",
    sms_config: Mapping[str, object] | None = None,
    require_sms: bool = False,
) -> PreflightReport:
    checks: list[PreflightCheck] = [
        check_python(),
        check_curl_cffi(),
        check_http_stack(),
    ]
    if sentinel:
        checks.append(check_sentinel())
    if require_browser or browser_profile:
        browser = check_browser_stack()
        checks.append(browser if require_browser else PreflightCheck(
            browser.name, browser.ok, browser.detail, required=False
        ))
        checks.append(check_browser_profile(browser_profile))
    checks.append(check_mailbox_source(
        provider=mailbox_provider,
        email=mailbox_email,
        config=mailbox_config,
        required=require_mailbox,
    ))
    checks.append(check_sms_source(
        provider=sms_provider,
        config=sms_config,
        required=require_sms,
    ))
    checks.extend(extra_checks)
    return PreflightReport(tuple(checks))


def assert_preflight(
    *,
    require_browser: bool = False,
    browser_profile: str = "",
    extra_checks: Iterable[PreflightCheck] = (),
    sentinel: bool = False,
    mailbox_provider: str = "",
    mailbox_email: str = "",
    mailbox_config: Mapping[str, object] | None = None,
    require_mailbox: bool = False,
    sms_provider: str = "",
    sms_config: Mapping[str, object] | None = None,
    require_sms: bool = False,
) -> PreflightReport:
    report = run_preflight(
        require_browser=require_browser,
        browser_profile=browser_profile,
        extra_checks=tuple(extra_checks),
        sentinel=sentinel,
        mailbox_provider=mailbox_provider,
        mailbox_email=mailbox_email,
        mailbox_config=mailbox_config,
        require_mailbox=require_mailbox,
        sms_provider=sms_provider,
        sms_config=sms_config,
        require_sms=require_sms,
    )
    if not report.ok:
        raise PreflightError(report)
    return report