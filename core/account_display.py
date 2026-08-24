from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.datetime_utils import serialize_datetime


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _format_value(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _format_reset_at(value: Any) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%m/%d %H:%M")


def _format_maybe_timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return _format_reset_at(value)
    text = _text(value)
    if text.isdigit():
        return _format_reset_at(text)
    return text


def _metric(
    key: str,
    label: str,
    value: Any,
    *,
    sub: str = "",
    percent: int | float | None = None,
    tone: str = "muted",
) -> dict[str, Any] | None:
    text = _format_value(value)
    if not text:
        return None
    payload: dict[str, Any] = {
        "key": key,
        "label": label,
        "value": text,
        "tone": tone,
    }
    if sub:
        payload["sub"] = sub
    if percent is not None:
        try:
            payload["percent"] = max(0, min(100, round(float(percent), 2)))
        except (TypeError, ValueError):
            pass
    return payload


def _append_metric(items: list[dict[str, Any]], metric: dict[str, Any] | None) -> None:
    if metric:
        items.append(metric)


def _quota_metric(key: str, label: str, limit: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(limit, dict):
        return None
    window = _safe_dict(limit.get("primary_window"))
    if not window:
        return None
    used_percent = window.get("used_percent")
    try:
        remaining_percent = max(0, min(100, 100 - float(used_percent or 0)))
    except (TypeError, ValueError):
        remaining_percent = None
    reset_label = _format_reset_at(window.get("reset_at"))
    sub = f"{reset_label} 重置" if reset_label else ""
    if remaining_percent is None:
        return _metric(key, label, "可用" if limit.get("allowed") else "受限", sub=sub, tone="good" if limit.get("allowed") else "danger")
    tone = "danger" if bool(limit.get("limit_reached")) or remaining_percent <= 0 else ("warning" if remaining_percent <= 20 else "good")
    return _metric(
        key,
        label,
        f"剩余 {remaining_percent:g}%",
        sub=sub,
        percent=remaining_percent,
        tone=tone,
    )


def _quota_period_label(limit: dict[str, Any] | None, *, plan_type: str = "") -> str:
    window = _safe_dict(_safe_dict(limit).get("primary_window"))
    try:
        window_seconds = int(window.get("limit_window_seconds") or 0)
    except (TypeError, ValueError):
        window_seconds = 0

    if window_seconds >= 28 * 24 * 60 * 60:
        return "月限额"
    if window_seconds >= 6 * 24 * 60 * 60:
        return "周限额"
    return "月限额" if _text(plan_type).lower() == "free" else "周限额"


def _build_chatgpt_metrics(overview: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    usage = _safe_dict(overview.get("chatgpt_usage") or overview.get("wham_usage"))
    if not usage:
        return primary, secondary

    plan_type = _text(usage.get("plan_type") or overview.get("plan_name") or overview.get("plan"))
    rate_limit = _safe_dict(usage.get("rate_limit"))
    code_review_limit = _safe_dict(usage.get("code_review_rate_limit"))
    _append_metric(
        primary,
        _quota_metric(
            "chatgpt_weekly_limit",
            _quota_period_label(rate_limit, plan_type=plan_type),
            rate_limit,
        ),
    )
    _append_metric(
        primary,
        _quota_metric(
            "chatgpt_code_review_weekly_limit",
            f"代码审查{_quota_period_label(code_review_limit, plan_type=plan_type)}",
            code_review_limit,
        ),
    )

    credits = _safe_dict(usage.get("credits"))
    if credits:
        if credits.get("unlimited"):
            _append_metric(secondary, _metric("chatgpt_credits", "Credits", "无限", tone="good"))
        elif credits.get("balance") not in (None, ""):
            _append_metric(secondary, _metric("chatgpt_credits", "Credits", credits.get("balance"), tone="muted"))
        if credits.get("approx_local_messages") not in (None, ""):
            _append_metric(secondary, _metric("chatgpt_local_messages", "本地消息", credits.get("approx_local_messages"), tone="muted"))
        if credits.get("approx_cloud_messages") not in (None, ""):
            _append_metric(secondary, _metric("chatgpt_cloud_messages", "云端消息", credits.get("approx_cloud_messages"), tone="muted"))
    return primary, secondary


def _build_generic_usage_metrics(overview: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []

    _append_metric(primary, _metric("remaining_credits", "剩余额度", overview.get("remaining_credits"), tone="good"))
    _append_metric(primary, _metric("usage_total", "已用额度", overview.get("usage_total"), tone="muted"))
    _append_metric(secondary, _metric("plan_credits", "总额度", overview.get("plan_credits"), tone="muted"))
    _append_metric(secondary, _metric("reset_days", "重置倒计时", overview.get("days_until_reset"), sub="天", tone="muted"))
    _append_metric(secondary, _metric("next_reset_at", "下次重置", _format_maybe_timestamp(overview.get("next_reset_at")), tone="muted"))

    usage_models = _safe_list(overview.get("usage_models"))
    if usage_models:
        sections.append(
            {
                "key": "usage_models",
                "title": "模型用量",
                "items": [
                    {
                        "title": _text(item.get("model")) or "model",
                        "metrics": [
                            metric
                            for metric in [
                                _metric("num_requests", "请求数", item.get("num_requests")),
                                _metric("remaining_requests", "剩余请求", item.get("remaining_requests"), tone="good"),
                                _metric("num_tokens", "Token", item.get("num_tokens")),
                                _metric("remaining_tokens", "剩余 Token", item.get("remaining_tokens"), tone="good"),
                            ]
                            if metric
                        ],
                    }
                    for item in usage_models
                    if isinstance(item, dict)
                ],
            }
        )

    usage_breakdowns = _safe_list(overview.get("usage_breakdowns"))
    if usage_breakdowns:
        sections.append(
            {
                "key": "usage_breakdowns",
                "title": "额度明细",
                "items": [
                    {
                        "title": _text(item.get("display_name")) or "usage",
                        "metrics": [
                            metric
                            for metric in [
                                _metric("current_usage", "已用", item.get("current_usage")),
                                _metric("usage_limit", "上限", item.get("usage_limit")),
                                _metric("remaining_usage", "剩余", item.get("remaining_usage"), tone="good"),
                                _metric("trial_status", "试用状态", item.get("trial_status")),
                                _metric("trial_expiry", "试用到期", item.get("trial_expiry")),
                                _metric("trial_remaining_usage", "试用剩余", item.get("trial_remaining_usage"), tone="good"),
                            ]
                            if metric
                        ],
                    }
                    for item in usage_breakdowns
                    if isinstance(item, dict)
                ],
            }
        )

    return primary, secondary, sections


_REGISTRATION_STAGE_LABELS = {
    "account_created": "邮箱建号",
    "phone_verified": "手机验证",
    "credentials_ready": "Codex RT 获取",
    "liveness": "首次存活质检",
    "persisted": "账号入库",
}


def _build_registration_summary(
    overview: dict[str, Any],
    *,
    lifecycle_status: str,
    validity_status: str,
) -> dict[str, Any]:
    pipeline = _safe_dict(overview.get("registration_pipeline"))
    if not pipeline:
        return {}
    stages = _safe_dict(pipeline.get("stages"))
    current_stage = _text(pipeline.get("current_stage"))
    current = _safe_dict(stages.get(current_stage))
    stage_status = _text(current.get("status") or "unknown").lower()
    registration_status = _text(pipeline.get("registration_status") or "pending").lower()
    stage_label = _REGISTRATION_STAGE_LABELS.get(current_stage, current_stage or "注册流程")
    error = _text(current.get("error"))

    terminal = validity_status == "invalid" or lifecycle_status in {
        "invalid",
        "disabled",
        "banned",
        "deactivated",
    }
    if terminal:
        state = "invalid"
        label = "注册账号已失效"
    elif registration_status == "registered":
        state = "complete"
        label = "注册完成"
    elif stage_status == "failed" or registration_status == "failed":
        state = "failed"
        label = f"{stage_label}失败"
    elif stage_status == "in_progress":
        state = "pending"
        label = f"正在{stage_label}"
    else:
        state = "pending"
        label = f"等待{stage_label}"

    core_stages = tuple(_REGISTRATION_STAGE_LABELS)
    completed = sum(
        1
        for stage in core_stages
        if _text(_safe_dict(stages.get(stage)).get("status")).lower()
        in {"passed", "not_required"}
    )
    return {
        "state": state,
        "label": label,
        "registration_status": registration_status,
        "current_stage": current_stage,
        "current_stage_label": stage_label,
        "current_stage_status": stage_status,
        "error": error,
        "completed_stages": completed,
        "total_stages": len(core_stages),
        "stages": {
            stage: {
                "label": label_value,
                "status": _text(_safe_dict(stages.get(stage)).get("status") or "waiting"),
                "error": _text(_safe_dict(stages.get(stage)).get("error")),
            }
            for stage, label_value in _REGISTRATION_STAGE_LABELS.items()
        },
    }


def build_account_display_summary(
    *,
    platform: str,
    email: str,
    lifecycle_status: str,
    validity_status: str,
    plan_state: str,
    plan_name: str,
    display_status: str,
    overview: dict[str, Any] | None,
    provider_resources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    overview = _safe_dict(overview)
    checked_at = overview.get("checked_at")
    if isinstance(checked_at, datetime):
        checked_at_value = serialize_datetime(checked_at)
    else:
        checked_at_value = _text(checked_at)

    primary_metrics: list[dict[str, Any]] = []
    secondary_metrics: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []

    effective_plan_name = _text(plan_name or overview.get("plan_name") or overview.get("plan"))
    if effective_plan_name:
        _append_metric(secondary_metrics, _metric("plan_name", "套餐", effective_plan_name, tone="muted"))
    if plan_state and plan_state != "unknown":
        _append_metric(secondary_metrics, _metric("plan_state", "套餐状态", plan_state, tone="muted"))
    if checked_at_value:
        _append_metric(secondary_metrics, _metric("checked_at", "最近检测", checked_at_value, tone="muted"))
    if overview.get("phone_number"):
        _append_metric(
            secondary_metrics,
            _metric("phone_number", "原注册手机号", overview.get("phone_number"), tone="muted"),
        )

    chatgpt_primary, chatgpt_secondary = _build_chatgpt_metrics(overview)
    primary_metrics.extend(chatgpt_primary)
    secondary_metrics.extend(chatgpt_secondary)

    generic_primary, generic_secondary, generic_sections = _build_generic_usage_metrics(overview)
    primary_metrics.extend(generic_primary)
    secondary_metrics.extend(generic_secondary)
    sections.extend(generic_sections)

    warnings: list[dict[str, Any]] = []
    registration = _build_registration_summary(
        overview,
        lifecycle_status=lifecycle_status,
        validity_status=validity_status,
    )
    if validity_status == "invalid" or lifecycle_status == "invalid":
        warnings.append({
            "key": "invalid",
            "tone": "danger",
            "message": _text(overview.get("validity_reason")) or "账号当前检测为失效",
        })
    if validity_status == "unknown":
        warnings.append({
            "key": "unknown_validity",
            "tone": "warning",
            "message": _text(overview.get("validity_reason")) or "尚未完成有效性检测",
        })
    if overview.get("quota_note"):
        warnings.append({"key": "quota_note", "tone": "warning", "message": _text(overview.get("quota_note"))})
    if overview.get("check_error"):
        warnings.append({"key": "check_error", "tone": "danger", "message": _text(overview.get("check_error"))})
    if registration and registration.get("state") in {"failed", "pending"}:
        registration_message = _text(registration.get("error")) or _text(registration.get("label"))
        warnings.append({
            "key": "registration_pipeline",
            "tone": "danger" if registration.get("state") == "failed" else "warning",
            "message": registration_message,
        })

    badges = [
        {"label": _text(chip), "tone": "muted"}
        for chip in _safe_list(overview.get("chips"))
        if _text(chip)
    ]
    for resource in provider_resources or []:
        if isinstance(resource, dict) and resource.get("resource_type") == "mailbox" and (resource.get("handle") or resource.get("display_name")):
            badges.append({"label": "邮箱验证", "tone": "muted"})
            break

    return {
        "identity": {
            "email": email,
            "remote_email": _text(overview.get("remote_email")),
            "platform": platform,
        },
        "status": {
            "display": display_status,
            "lifecycle": lifecycle_status,
            "validity": validity_status,
            "plan_state": plan_state,
            "plan_name": effective_plan_name,
            "checked_at": checked_at_value,
        },
        "primary_metrics": primary_metrics,
        "secondary_metrics": secondary_metrics,
        "badges": badges,
        "warnings": warnings,
        "sections": sections,
        "registration": registration,
    }
