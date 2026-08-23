from __future__ import annotations

import re


_FORCE_SUMMARY_MARKERS = (
    "任务已",
    "开始第",
    "预检通过",
    "使用代理",
    "正在从",
    "已成功租到号码",
    "等待短信验证码",
    "等待邮箱验证码",
    "正在等待邮箱",
    "已收到短信验证码",
    "成功获取验证码",
    "验证成功",
    "注册成功",
    "注册失败",
    "注册完成",
    "创建失败",
    "登录失败",
    "绑定失败",
    "获取失败",
    "校验失败",
    "发送失败",
    "账户创建失败",
    "账号已创建",
    "账号已保留",
    "释放",
    "换号",
    "不可用",
    "超时",
    "异常",
    "错误",
    "拒绝",
    "风控",
    "停用",
    "撤销",
    "熔断",
    "未返回",
    "未找到",
    "存活复检",
)

_DIAGNOSTIC_MARKERS = (
    "High concurrency profile:",
    "NextAuth providers 状态",
    "CSRF token:",
    "signin/openai 状态",
    "OAuth authorize URL 获取成功",
    "oai-did:",
    "Device ID:",
    "Sentinel PoW solved:",
    "Sentinel VM solved:",
    "Sentinel token 获取成功:",
    "Sentinel 已刷新:",
    "client_auth_session_dump 状态",
    "body_fields=",
    "页面加载状态",
    "验证码页加载状态",
    "session keys:",
    "accessToken 长度:",
    "已返回 continue_url",
    "continue=yes",
    "continue=no",
)

_HTTP_STATUS_RE = re.compile(r"(?:状态|HTTP)(?:\[[^\]]+\])?\s*[:=]?\s*(\d{3})", re.IGNORECASE)

_STAGE_RULES = (
    ("result", ("任务结束", "完成: 成功", "注册成功", "注册失败")),
    ("postprocess", ("refresh_token", "获取rt", "获取 RT", "添加邮箱", "绑定邮箱", "存活复检")),
    ("verification", ("验证码", "OTP", "add_phone", "add-phone", "手机号验证")),
    ("account", ("创建账户", "账户创建", "create_account", "callback", "session-token", "session 信息")),
    ("authorization", ("OAuth", "NextAuth", "Sentinel", "Device ID", "CSRF", "提交密码", "加载密码页")),
    ("identity", ("邮箱", "手机号", "租号", "号码", "SMS")),
    ("setup", ("任务已", "High concurrency", "预检", "使用代理", "IP 位置")),
)


def classify_registration_log(
    message: str,
    *,
    level: str = "info",
    event_type: str = "log",
) -> dict[str, str]:
    """Classify a registration log without changing the registration flow."""
    text = str(message or "")
    normalized_level = str(level or "info").lower()
    normalized_type = str(event_type or "log").lower()

    stage = "flow"
    for candidate, markers in _STAGE_RULES:
        if any(marker.lower() in text.lower() for marker in markers):
            stage = candidate
            break

    visibility = "summary"
    if normalized_level == "info" and normalized_type == "log":
        force_summary = any(marker.lower() in text.lower() for marker in _FORCE_SUMMARY_MARKERS)
        if not force_summary:
            status_match = _HTTP_STATUS_RE.search(text)
            successful_status = bool(status_match and int(status_match.group(1)) < 400)
            diagnostic_marker = any(marker.lower() in text.lower() for marker in _DIAGNOSTIC_MARKERS)
            if successful_status or diagnostic_marker:
                visibility = "diagnostic"

    return {"log_view": visibility, "log_stage": stage}
