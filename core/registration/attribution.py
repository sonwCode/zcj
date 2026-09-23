"""Failure attribution for registration errors.

The dashboard historically grouped failures by their raw error string, which
produces hundreds of near-duplicate buckets and cannot answer the operational
question: are runs being lost to the proxy, the mailbox, SMS risk, a captcha or
our own credential boundary?  This module maps an error to one stable category
so success rate can be broken down by root cause.
"""
from __future__ import annotations

from dataclasses import dataclass

CATEGORY_PROXY_BLOCKED = "proxy_blocked"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_CAPTCHA_FAIL = "captcha_fail"
CATEGORY_PHONE_RISK = "phone_risk"
CATEGORY_OTP_TIMEOUT = "otp_timeout"
CATEGORY_MAILBOX_ERROR = "mailbox_error"
CATEGORY_CREDENTIAL_INCOMPLETE = "credential_incomplete"
CATEGORY_NETWORK_ERROR = "network_error"
CATEGORY_UPSTREAM_ERROR = "upstream_error"
CATEGORY_UNKNOWN = "unknown"

_LABELS = {
    CATEGORY_PROXY_BLOCKED: "代理被风控/不可用",
    CATEGORY_RATE_LIMITED: "触发限流",
    CATEGORY_CAPTCHA_FAIL: "验证码/人机校验失败",
    CATEGORY_PHONE_RISK: "手机号风控",
    CATEGORY_OTP_TIMEOUT: "验证码超时",
    CATEGORY_MAILBOX_ERROR: "邮箱异常",
    CATEGORY_CREDENTIAL_INCOMPLETE: "未取得稳定凭据",
    CATEGORY_NETWORK_ERROR: "网络错误",
    CATEGORY_UPSTREAM_ERROR: "上游服务错误",
    CATEGORY_UNKNOWN: "未知原因",
}

_RETRYABLE = {
    CATEGORY_PROXY_BLOCKED,
    CATEGORY_RATE_LIMITED,
    CATEGORY_CAPTCHA_FAIL,
    CATEGORY_OTP_TIMEOUT,
    CATEGORY_MAILBOX_ERROR,
    CATEGORY_NETWORK_ERROR,
    CATEGORY_UPSTREAM_ERROR,
}

_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CATEGORY_PROXY_BLOCKED, (
        "proxy", "代理", "407", "proxyerror", "proxy_network", "cf_chl",
        "cloudflare", "unusual traffic", "ip banned", "ip 被封", "访问被拒绝",
        "proxy_preflight_failed", "authentication_rejected", "connectivity_failed",
    )),
    (CATEGORY_RATE_LIMITED, (
        "429", "too many requests", "rate limit", "请求过于频繁", "限流",
        "请求频率", "稍后再试",
    )),
    (CATEGORY_CAPTCHA_FAIL, (
        "captcha", "turnstile", "人机", "challenge", "solver", "验证码校验失败",
        "captcha_fail",
    )),
    (CATEGORY_PHONE_RISK, (
        "风控", "oas_error", "oas error", "接码", "号码不可用", "号码已停用",
        "phone_risk", "sms", "虚拟号", "手机号验证失败",
    )),
    (CATEGORY_OTP_TIMEOUT, (
        "验证码超时", "未收到验证码", "等待验证码", "otp timeout", "otp 超时",
        "验证码未返回", "短信验证码超时", "邮箱验证码超时",
    )),
    (CATEGORY_MAILBOX_ERROR, (
        "mailbox", "邮箱", "收件", "邮件", "inbox", "mail provider", "email_error",
        "邮箱异常", "无法获取邮件",
    )),
    (CATEGORY_CREDENTIAL_INCOMPLETE, (
        "missing_access_token", "stable-http-200-at", "未取得稳定", "persistence",
        "missing_identity", "missing_secret", "status_not_success",
    )),
    (CATEGORY_NETWORK_ERROR, (
        "timeout", "timed out", "connection", "ssl", "dns", "超时", "连接失败",
        "network", "remotedisconnected",
    )),
    (CATEGORY_UPSTREAM_ERROR, (
        "500", "502", "503", "504", "服务不可用", "internal server error",
        "bad gateway",
    )),
)


@dataclass(frozen=True)
class Attribution:
    code: str
    label: str
    retryable: bool
    stage: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "retryable": self.retryable,
            "stage": self.stage,
        }


def _from_status(status_code: int | None) -> str:
    if status_code is None:
        return ""
    code = int(status_code)
    if code == 407:
        return CATEGORY_PROXY_BLOCKED
    if code == 429:
        return CATEGORY_RATE_LIMITED
    if code == 403:
        return CATEGORY_PROXY_BLOCKED
    if 500 <= code < 600:
        return CATEGORY_UPSTREAM_ERROR
    return ""


def classify_failure(
    message: str,
    *,
    stage: str = "",
    status_code: int | None = None,
    error_code: str = "",
) -> Attribution:
    """Map one failure to a stable category."""
    empty = ""
    by_status = _from_status(status_code)
    if by_status:
        return Attribution(by_status, _LABELS[by_status], by_status in _RETRYABLE, stage)

        haystack = f"{message or empty} {error_code or empty}".lower()
    if haystack.strip():
        for code, keywords in _RULES:
            if any(keyword.lower() in haystack for keyword in keywords):
                return Attribution(code, _LABELS[code], code in _RETRYABLE, stage)
    return Attribution(CATEGORY_UNKNOWN, _LABELS[CATEGORY_UNKNOWN], False, stage)


def attribution_table() -> list[dict]:
    return [
        {"code": code, "label": _LABELS[code], "retryable": code in _RETRYABLE}
        for code in _LABELS
    ]


def summarize_attributions(rows: list[dict]) -> list[dict]:
    """Aggregate raw error rows into attributed buckets.

    Rows must carry an error string and a count integer.
    """
    buckets: dict[str, dict] = {}
    for row in rows or []:
        error = str((row or {}).get("error") or "")
        count = int((row or {}).get("count") or 0)
        attribution = classify_failure(error)
        bucket = buckets.setdefault(
            attribution.code,
            {
                "code": attribution.code,
                "label": attribution.label,
                "retryable": attribution.retryable,
                "count": 0,
                "samples": [],
            },
        )
        bucket["count"] += count
        if error and len(bucket["samples"]) < 3:
            bucket["samples"].append(error[:200])
    return sorted(buckets.values(), key=lambda item: item["count"], reverse=True)