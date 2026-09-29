"""Runtime capability checks shared by API and task execution."""
from __future__ import annotations

import os


SERVER_RUNTIME_VALUES = {"server", "service", "docker", "headless"}


def runtime_mode() -> str:
    value = str(os.getenv("APP_RUNTIME_MODE", "desktop") or "desktop").strip().lower()
    return value or "desktop"


def is_server_runtime() -> bool:
    return runtime_mode() in SERVER_RUNTIME_VALUES


def har_capture_available() -> bool:
    # HAR 里可能包含 cookie、一次性验证码和完整请求体；
    # 对外发布的版本不暴露抓包能力。
    return False
