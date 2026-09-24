"""Resume a partially-registered account instead of burning another mailbox.

``platform.register()`` returns an account that already carries an access token; the
stages after it (phone binding, Codex credentials, persistence, liveness, payment) are
enrichment. When one of those fails the account is saved as ``pending_verification``,
but the next attempt ignored that row and started a brand-new registration — consuming
another mailbox and leaving an orphan behind.

This module finds those orphans and decides where a retry should re-enter the
pipeline, so a failure at a late stage costs a retry instead of a whole mailbox.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# Order matters: a resume re-enters at the first stage that failed.
PIPELINE_STAGES = (
    "account_created",
    "phone_verified",
    "credentials_ready",
    "persisted",
    "liveness",
    "probation",
    "payment",
)

RESUMABLE_STATUSES = {"pending_verification", "manual_phone_required"}

# A failure at these stages means the account itself is dead; resuming would just
# burn another retry on an account that can never succeed.
TERMINAL_STAGES = {"account_created"}


def _stage_is_terminal(value: Any) -> bool:
    """Honour the explicit terminal marker written by the pipeline.

    ``_mark_terminal_registration_account`` writes ``detail.terminal = True`` for a
    failure the remote side has already made permanent. Relying on the account
    status alone to catch that would be indirect, so the marker is read directly.
    """
    if not isinstance(value, dict):
        return False
    detail = value.get("detail")
    return isinstance(detail, dict) and detail.get("terminal") is True


@dataclass(frozen=True)
class ResumeCandidate:
    account_id: int
    platform: str
    email: str
    status: str
    resume_stage: str
    has_token: bool
    reason: str
    account: Any = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "email": self.email,
            "status": self.status,
            "resume_stage": self.resume_stage,
            "has_token": self.has_token,
            "reason": self.reason,
        }


def resume_enabled(extra: dict | None) -> bool:
    """Resuming orphans is opt-in per task, then globally."""
    raw = (extra or {}).get("resume_registration")
    if raw is None:
        raw = os.getenv("ZCJ_RESUME_REGISTRATION", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _stage_state(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("state") or value.get("status") or "").strip().lower()
    return str(value or "").strip().lower()


def first_failed_stage(stages: dict) -> str:
    for name in PIPELINE_STAGES:
        if _stage_state((stages or {}).get(name)) == "failed":
            return name
    return ""


def plan_resume(overview: dict, *, status: str = "") -> tuple[bool, str, str]:
    """Decide whether one stored account can be resumed, and from where."""
    normalized = str(status or "").strip().lower()
    if normalized not in RESUMABLE_STATUSES:
        return False, "", "状态 " + (normalized or "unknown") + " 不可续跑"

    data = overview or {}
    if str(data.get("validity_status") or "").strip().lower() == "invalid":
        return False, "", "远端已判定失效"

    pipeline = dict(data.get("registration_pipeline") or {})
    stages = dict(pipeline.get("stages") or {})
    failed = first_failed_stage(stages)
    if not failed:
        return False, "", "没有失败阶段"
    if failed in TERMINAL_STAGES or _stage_is_terminal(stages.get(failed)):
        return False, "", failed + " 为终态失败"
    return True, failed, "从 " + failed + " 续跑"


def find_resumable_accounts(
    platform: str,
    *,
    email: str = "",
    limit: int = 5,
    exclude_ids: set | None = None,
) -> list[ResumeCandidate]:
    """Return stored accounts that a retry should finish instead of replacing."""
    from sqlmodel import Session, select

    from core.db import AccountModel, engine
    from core.platform_accounts import build_platform_account

    wanted = max(int(limit or 5), 1)
    skip = {int(value) for value in (exclude_ids or set())}
    candidates: list[ResumeCandidate] = []

    with Session(engine) as session:
        stmt = select(AccountModel).where(AccountModel.platform == str(platform or ""))
        if email:
            stmt = stmt.where(AccountModel.email == str(email))
        stmt = stmt.order_by(AccountModel.id.desc()).limit(wanted * 5)
        rows = session.exec(stmt).all()
        for row in rows:
            account_id = int(row.id or 0)
            if not account_id or account_id in skip:
                continue
            try:
                account = build_platform_account(session, row)
            except Exception:
                continue
            token = str(getattr(account, "token", "") or "").strip()
            if not token:
                continue
            raw_status = getattr(account, "status", "")
            status = str(getattr(raw_status, "value", raw_status) or "")
            overview = dict((getattr(account, "extra", {}) or {}).get("account_overview") or {})
            ok, stage, reason = plan_resume(overview, status=status)
            if not ok:
                continue
            candidates.append(ResumeCandidate(
                account_id=account_id,
                platform=str(row.platform or ""),
                email=str(row.email or ""),
                status=status,
                resume_stage=stage,
                has_token=True,
                reason=reason,
                account=account,
            ))
            if len(candidates) >= wanted:
                break
    return candidates