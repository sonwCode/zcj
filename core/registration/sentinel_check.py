"""Offline self-test for the Python Sentinel SDK reimplementation.

ZCJ solves OpenAI Sentinel proof-of-work with a pure-Python reimplementation of
the SDK VM (``platforms/chatgpt/sentinel_vm.py`` plus the ``_SentinelTokenGenerator``
in ``platforms/chatgpt/register.py``). Unlike turb-gpt-free-register, which runs the
real ``sdk.js`` inside a Node ``vm`` and keeps Python only as a fallback, ZCJ has a
single implementation and no way to notice when OpenAI ships a new SDK shape: the
payload goes structurally wrong, the server rejects it, and the failure surfaces as a
generic signup error after the proxy and mailbox have already been paid for.

This module makes that drift *observable* without adding a Node runtime. It builds a
real token offline and asserts both the payload shape and its internal coherence:
the locale and timezone must agree with the resolved browser profile, the
``timeOrigin`` field must agree with the wall clock, and the PoW solver must
terminate and return a well-formed token.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass

PAYLOAD_LENGTH = 19
REQ_PREFIX = "gAAAAAC"
POW_PREFIX = "gAAAAAB"
DATE_RE = re.compile(
    r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{2} \d{4} \d{2}:\d{2}:\d{2} GMT[+-]\d{4} \([^)]+\)$"
)
SCREEN_RE = re.compile(r"^\d+x\d+$")
OFFSET_RE = re.compile(r"GMT([+-]\d{4})")
TIMEORIGIN_TOLERANCE_MS = 120000.0
POW_DIFFICULTY = "ff"


@dataclass(frozen=True)
class SentinelCheck:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class SentinelReport:
    checks: tuple[SentinelCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[SentinelCheck, ...]:
        return tuple(check for check in self.checks if not check.ok)

    def summary(self) -> str:
        if self.ok:
            return f"哨兵自检 {len(self.checks)} 项全部通过"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "failures": [c.name for c in self.failures],
            "summary": self.summary(),
        }


def _decode(token: str) -> list:
    raw = token[len(REQ_PREFIX):]
    return json.loads(base64.b64decode(raw).decode("utf-8"))


def _offset_of(text: str) -> str:
    match = OFFSET_RE.search(str(text or ""))
    return match.group(1) if match else ""


def sentinel_sdk_stamp() -> dict:
    """Identify which SDK build the solver was written against."""
    try:
        from platforms.chatgpt.constants import (
            SENTINEL_FRAME_VERSION,
            SENTINEL_SDK_URL,
            SENTINEL_SDK_VERSION,
        )
    except Exception:
        return {}
    return {
        "sdk_version": SENTINEL_SDK_VERSION,
        "frame_version": SENTINEL_FRAME_VERSION,
        "sdk_url": SENTINEL_SDK_URL,
        "payload_length": PAYLOAD_LENGTH,
    }


def run_sentinel_self_test(profile=None) -> SentinelReport:
    """Build a token offline and verify it is structurally and logically coherent."""
    try:
        from platforms.chatgpt.constants import SENTINEL_SDK_URL
        from platforms.chatgpt.register import _SentinelTokenGenerator
    except Exception as exc:
        return SentinelReport((SentinelCheck("import", False, f"无法导入哨兵实现: {exc}"),))

    if profile is None:
        from core.identity_profile import resolve_profile

        profile = resolve_profile()

    checks: list[SentinelCheck] = []
    generator = _SentinelTokenGenerator("sentinel-self-test", profile.user_agent, profile)
    token = generator.generate_requirements_token()
    checks.append(SentinelCheck("prefix", token.startswith(REQ_PREFIX), f"前缀 {token[:8]}"))

    try:
        payload = _decode(token)
    except Exception as exc:
        checks.append(SentinelCheck("decode", False, f"载荷无法解码: {exc}"))
        return SentinelReport(tuple(checks))
    checks.append(SentinelCheck("decode", True, "载荷可解码"))

    size = len(payload) if isinstance(payload, list) else -1
    checks.append(SentinelCheck("shape", size == PAYLOAD_LENGTH, f"字段数 {size}，期望 {PAYLOAD_LENGTH}"))
    if size != PAYLOAD_LENGTH:
        return SentinelReport(tuple(checks))

    screen = str(payload[0] or "")
    checks.append(SentinelCheck("screen", bool(SCREEN_RE.match(screen)), f"分辨率 {screen}"))

    date_string = str(payload[1] or "")
    checks.append(SentinelCheck("date_format", bool(DATE_RE.match(date_string)), f"Date.toString() {date_string}"))

    expected_offset = _offset_of(profile.js_date_string())
    actual_offset = _offset_of(date_string)
    checks.append(SentinelCheck(
        "timezone",
        bool(actual_offset) and actual_offset == expected_offset,
        f"载荷 {actual_offset or chr(63)} vs 画像 {expected_offset or chr(63)}",
    ))

    checks.append(SentinelCheck("user_agent", str(payload[4] or "") == profile.user_agent, "UA 与画像一致"))

    # client hints 必须与 UA 同版本、同 OS。curl_cffi 的 chrome119 及以上目标都是
    # macOS，若 UA 说 Windows 而 TLS/HTTP2 指纹说 macOS，就是 CF 一眼可辨的矛盾。
    hints = profile.headers()
    match = re.search(r"Chrome/(\d+)", str(profile.user_agent or ""))
    ua_version = match.group(1) if match else ""
    hint_version_ok = bool(ua_version) and (";v=" + chr(34) + ua_version + chr(34)) in str(hints.get("sec-ch-ua", ""))
    hint_platform_ok = hints.get("sec-ch-ua-platform") == chr(34) + str(profile.platform) + chr(34)
    ua_os_ok = True
    if profile.platform == "macOS":
        ua_os_ok = "Macintosh" in str(profile.user_agent)
    elif profile.platform == "Windows":
        ua_os_ok = "Windows NT" in str(profile.user_agent)
    checks.append(SentinelCheck(
        "client_hints",
        hint_version_ok and hint_platform_ok and ua_os_ok,
        "sec-ch-ua v%s / platform %s / UA %s" % (ua_version or chr(34), hints.get("sec-ch-ua-platform"), profile.platform),
    ))
    checks.append(SentinelCheck("sdk_url", str(payload[5] or "") == SENTINEL_SDK_URL, f"SDK {SENTINEL_SDK_URL}"))
    checks.append(SentinelCheck(
        "locale",
        str(payload[8] or "") == profile.navigator_language,
        f"载荷 {payload[8]} vs 画像 {profile.navigator_language}",
    ))

    try:
        perf_now = float(payload[14])
        time_origin = float(payload[18])
        skew = abs((time_origin + perf_now) - time.time() * 1000)
        checks.append(SentinelCheck("clock", skew <= TIMEORIGIN_TOLERANCE_MS, f"时钟偏差 {skew:.0f}ms"))
    except Exception as exc:
        checks.append(SentinelCheck("clock", False, f"时间字段异常: {exc}"))

    try:
        started = time.time()
        solved = generator.generate_token("sentinel-self-test", POW_DIFFICULTY)
        elapsed = time.time() - started
        checks.append(SentinelCheck(
            "pow",
            solved.startswith(POW_PREFIX) and solved.endswith("~S"),
            f"难度 {POW_DIFFICULTY} 用时 {elapsed:.2f}s",
        ))
    except Exception as exc:
        checks.append(SentinelCheck("pow", False, f"PoW 求解失败: {exc}"))

    return SentinelReport(tuple(checks))