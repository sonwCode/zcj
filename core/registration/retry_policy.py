"""Turn a failure attribution into a concrete retry decision.

``Attribution.retryable`` was computed but never consumed. Retries were governed by
a coarse attempt budget, so "the proxy is blocked" and "the mailbox never delivered"
were retried identically: a fresh mailbox was burned when the proxy was the problem,
and the same blocked exit was re-dialled when rotating the mailbox was the fix.

This module maps each attribution code to an explicit action — whether to retry, how
long to wait, which resource to rotate (proxy, mailbox) and which pipeline stage to
resume from — so a retry spends money on the thing that actually failed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .attribution import (
    CATEGORY_CAPTCHA_FAIL,
    CATEGORY_CREDENTIAL_INCOMPLETE,
    CATEGORY_MAILBOX_ERROR,
    CATEGORY_NETWORK_ERROR,
    CATEGORY_OTP_TIMEOUT,
    CATEGORY_PHONE_RISK,
    CATEGORY_PROXY_BLOCKED,
    CATEGORY_RATE_LIMITED,
    CATEGORY_UNKNOWN,
    CATEGORY_UPSTREAM_ERROR,
    _LABELS,
    classify_failure,
)

MAX_BACKOFF_SECONDS = 300.0
DEFAULT_MAX_ATTEMPTS = 3

# code -> (retry, base_backoff_seconds, rotate_proxy, rotate_mailbox, reason)
_POLICIES: dict[str, tuple[bool, float, bool, bool, str]] = {
    CATEGORY_PROXY_BLOCKED: (True, 0.0, True, False, "换一个出口 IP 再试"),
    CATEGORY_RATE_LIMITED: (True, 60.0, True, False, "退避后换出口 IP"),
    CATEGORY_CAPTCHA_FAIL: (True, 5.0, True, False, "换出口 IP 重新过人机校验"),
    CATEGORY_PHONE_RISK: (True, 10.0, True, True, "换邮箱与号段后重试"),
    CATEGORY_OTP_TIMEOUT: (True, 5.0, False, True, "换邮箱重发验证码"),
    CATEGORY_MAILBOX_ERROR: (True, 10.0, False, True, "换邮箱服务重试"),
    CATEGORY_CREDENTIAL_INCOMPLETE: (True, 3.0, True, False, "换出口 IP 重走凭据阶段"),
    CATEGORY_NETWORK_ERROR: (True, 15.0, False, False, "网络抖动，原样重试"),
    CATEGORY_UPSTREAM_ERROR: (True, 30.0, False, False, "上游故障，退避重试"),
    CATEGORY_UNKNOWN: (False, 0.0, False, False, "原因不明，不自动重试"),
}

# Where a retry should re-enter the pipeline instead of starting over.
_RESUME_STAGE = {
    CATEGORY_OTP_TIMEOUT: "credentials_ready",
    CATEGORY_CREDENTIAL_INCOMPLETE: "credentials_ready",
    CATEGORY_MAILBOX_ERROR: "account_created",
    CATEGORY_PHONE_RISK: "phone_verified",
}


@dataclass(frozen=True)
class RetryAction:
    retry: bool
    code: str
    label: str
    backoff_seconds: float = 0.0
    rotate_proxy: bool = False
    rotate_mailbox: bool = False
    resume_stage: str = ""
    reason: str = ""
    stage: str = ""

    def to_dict(self) -> dict:
        return {
            "retry": self.retry,
            "code": self.code,
            "label": self.label,
            "backoff_seconds": round(self.backoff_seconds, 2),
            "rotate_proxy": self.rotate_proxy,
            "rotate_mailbox": self.rotate_mailbox,
            "resume_stage": self.resume_stage,
            "reason": self.reason,
            "stage": self.stage,
        }


def _label(code: str) -> str:
    return _LABELS.get(code, code)


def decide(
    code: str,
    *,
    attempt: int = 1,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    stage: str = "",
) -> RetryAction:
    """Resolve one attribution code into an explicit retry action."""
    normalized = str(code or "").strip() or CATEGORY_UNKNOWN
    retry, base, rotate_proxy, rotate_mailbox, reason = _POLICIES.get(
        normalized, _POLICIES[CATEGORY_UNKNOWN]
    )
    tries = max(int(attempt or 1), 1)
    limit = int(max_attempts or 0)

    if not retry:
        return RetryAction(False, normalized, _label(normalized), 0.0, False, False, "", reason, stage)
    if limit and tries >= limit:
        return RetryAction(
            False, normalized, _label(normalized), 0.0, False, False, "",
            f"已达重试上限 ({limit})", stage,
        )

    backoff = min(base * (2 ** (tries - 1)), MAX_BACKOFF_SECONDS) if base else 0.0
    return RetryAction(
        True,
        normalized,
        _label(normalized),
        backoff,
        rotate_proxy,
        rotate_mailbox,
        _RESUME_STAGE.get(normalized, ""),
        reason,
        stage,
    )


def decide_for_failure(
    message: str,
    *,
    stage: str = "",
    status_code: int | None = None,
    error_code: str = "",
    attempt: int = 1,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RetryAction:
    """Classify a raw failure and resolve the matching retry action."""
    attribution = classify_failure(
        message, stage=stage, status_code=status_code, error_code=error_code
    )
    return decide(attribution.code, attempt=attempt, max_attempts=max_attempts, stage=stage)


def retry_policy_table() -> list[dict]:
    """Expose the policy so the dashboard can explain retry behaviour."""
    return [
        decide(code, attempt=1, max_attempts=0).to_dict()
        for code in _POLICIES
    ]