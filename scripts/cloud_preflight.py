#!/usr/bin/env python3
"""Preflight checks for a cloud deployment.

Every failure this looks for is one that a developer machine hides:

* a stripped base image without tzdata, where the identity profile silently renders
  its timezone as UTC and the Sentinel payload stops matching the proxy exit country;
* a container whose ``/dev/shm`` is the Docker default 64MB, where Chrome crashes once
  a few pages are open;
* an empty ``APP_PASSWORD``, which makes ``AuthMiddleware`` pass every ``/api`` request
  through to an admin panel that holds account passwords and platform tokens;
* ``UVICORN_WORKERS`` > 1, which starts a second copy of the in-process scheduler in
  every worker and double-dispatches tasks;
* a disk that is about to fill up with the task event log;
* a headed browser backend with no X display.

Run it on the server before starting the service:

    python3 scripts/cloud_preflight.py

It is read-only, makes no network calls, and exits non-zero when anything FAILs.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

# Docker gives /dev/shm 64MB unless asked otherwise. Chrome degrades badly below this.
SHM_WARN_BYTES = 256 * 1024 * 1024
# Leave room for the browser profiles and the WAL before calling it a problem.
DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024


class Report:
    def __init__(self) -> None:
        self.rows: list = []

    def add(self, status: str, name: str, detail: str = "", hint: str = "") -> None:
        self.rows.append((status, name, detail, hint))

    @property
    def failures(self) -> int:
        return sum(1 for row in self.rows if row[0] == FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for row in self.rows if row[0] == WARN)

    @property
    def passes(self) -> int:
        return sum(1 for row in self.rows if row[0] == PASS)

    def render(self) -> str:
        lines = ["", "ZCJ 云部署预检", "=" * 64]
        for status, name, detail, hint in self.rows:
            suffix = (" " + detail) if detail else ""
            lines.append("[%s] %s%s" % (status, name, suffix))
            if hint:
                lines.append("       → %s" % hint)
        lines.append("=" * 64)
        lines.append(
            "结果: %d 通过, %d 警告, %d 失败"
            % (self.passes, self.warnings, self.failures)
        )
        return "\n".join(lines)


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "%.1f%s" % (value, unit)
        value /= 1024
    return "%.1fTB" % value


def check_python(report: Report) -> None:
    version = sys.version_info
    ok = version >= (3, 10)
    report.add(
        PASS if ok else FAIL,
        "Python 版本",
        "%d.%d.%d" % (version.major, version.minor, version.micro),
        "" if ok else "代码使用了 X | None 语法，需要 Python 3.10+",
    )


def check_dependencies(report: Report) -> None:
    required = ("fastapi", "sqlmodel", "sqlalchemy", "curl_cffi")
    missing = []
    for name in required:
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    report.add(
        PASS if not missing else FAIL,
        "核心依赖",
        "全部可导入" if not missing else "缺少 " + ", ".join(missing),
        "" if not missing else "pip install -r requirements.txt",
    )

    # Playwright is only needed by the browser backends and the turnstile solver.
    import importlib.util

    if importlib.util.find_spec("playwright") is not None:
        report.add(PASS, "playwright", "可导入")
    else:
        report.add(
            WARN,
            "playwright",
            "不可导入",
            "只有协议路径需要时可以忽略；有头/浏览器路径会失败。",
        )


def check_timezone_data(report: Report) -> None:
    """The single most valuable check here.

    ``js_date_string()`` catches a missing zone and falls back to UTC, so the payload
    ends up claiming ``GMT+0000`` while the proxy exit is somewhere else. That is the
    exact contradiction the identity profile exists to remove, and it only reproduces
    on hosts without tzdata.
    """
    try:
        from core.identity_profile import _REGIONS, timezone_is_available
    except Exception as exc:
        report.add(FAIL, "时区库", "无法加载身份画像: %s" % exc)
        return

    missing = sorted(name for name, spec in _REGIONS.items() if not timezone_is_available(spec[2]))
    if missing:
        report.add(
            FAIL,
            "时区库",
            "%d/%d 个地区无法解析（例如 %s）" % (len(missing), len(_REGIONS), ", ".join(missing[:3])),
            "载荷时区会退化成 UTC，与代理出口 IP 不一致。安装 tzdata，或 pip install tzdata。",
        )
    else:
        report.add(PASS, "时区库", "%d/%d 个地区可解析" % (len(_REGIONS), len(_REGIONS)))


def check_auth(report: Report) -> None:
    password = str(os.environ.get("APP_PASSWORD", "") or "").strip()
    insecure_ok = str(os.environ.get("ZCJ_ALLOW_INSECURE", "") or "").strip() == "1"
    if password:
        report.add(PASS, "APP_PASSWORD", "已设置")
    elif insecure_ok:
        report.add(WARN, "APP_PASSWORD", "未设置，且 ZCJ_ALLOW_INSECURE=1", "仅限本机自用。")
    else:
        report.add(
            FAIL,
            "APP_PASSWORD",
            "未设置",
            "所有 /api 接口无需鉴权，账号口令与平台 Token 对任何能访问端口的人开放。",
        )


def check_single_process(report: Report) -> None:
    workers = str(os.environ.get("UVICORN_WORKERS", "") or "").strip()
    if workers and workers != "1":
        report.add(
            FAIL,
            "进程模型",
            "UVICORN_WORKERS=%s" % workers,
            "调度器是进程内单例，多 worker 会重复派发任务、重复探测账号。扩容请加容器。",
        )
    else:
        report.add(PASS, "进程模型", "单进程")


def check_display(report: Report) -> None:
    display = str(os.environ.get("DISPLAY", "") or "").strip()
    if not display:
        report.add(
            INFO,
            "X display",
            "未设置",
            "headless 后端不需要；有头后端需要 Xvfb 并设置 DISPLAY。",
        )
        return
    number = display.lstrip(":")
    sock = "/tmp/.X11-unix/X%s" % number
    if os.path.exists(sock):
        report.add(PASS, "X display", "%s (%s 存在)" % (display, sock))
    else:
        report.add(
            WARN,
            "X display",
            "%s 已设置但 %s 不存在" % (display, sock),
            "有头浏览器会启动失败。确认 Xvfb 起来了，或改用 headless。",
        )


def check_chrome(report: Report) -> None:
    candidates = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/ms-playwright/chromium/chrome-linux/chrome",
    )
    found = ""
    for candidate in candidates:
        if candidate.startswith("/"):
            if os.path.exists(candidate):
                found = candidate
                break
        else:
            located = shutil.which(candidate)
            if located:
                found = located
                break
    if found:
        report.add(PASS, "浏览器", found)
    else:
        report.add(
            WARN,
            "浏览器",
            "未找到 Chrome/Chromium",
            "协议路径不需要；浏览器路径会退回 playwright 自带 Chromium（若已安装）。",
        )


def check_shm(report: Report) -> None:
    path = "/dev/shm"
    if not os.path.isdir(path):
        report.add(INFO, "/dev/shm", "不存在", "非 Linux 环境可忽略。")
        return
    try:
        total = shutil.disk_usage(path).total
    except Exception as exc:
        report.add(WARN, "/dev/shm", "无法读取: %s" % exc)
        return
    if total < SHM_WARN_BYTES:
        report.add(
            WARN,
            "/dev/shm",
            _human(total),
            "Docker 默认 64MB，Chrome 开几个页面就会崩。compose 里设 shm_size: 1gb。",
        )
    else:
        report.add(PASS, "/dev/shm", _human(total))


def _database_path() -> str:
    url = str(os.environ.get("ACCOUNT_MANAGER_DATABASE_URL", "") or "").strip()
    if not url:
        return ""
    if not url.startswith("sqlite"):
        return ""
    return url.split("///")[-1]


def check_database(report: Report) -> None:
    path = _database_path()
    if not path:
        report.add(INFO, "数据库", "非 SQLite 或未配置", "跳过 SQLite 专项检查。")
        return

    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        report.add(FAIL, "数据库", "%s 不存在" % directory, "先创建并挂载数据卷。")
        return
    if not os.access(directory, os.W_OK):
        report.add(FAIL, "数据库", "%s 不可写" % directory, "检查挂载卷权限。")
        return
    report.add(PASS, "数据库", directory)

    if os.path.exists(path):
        report.add(INFO, "数据库文件", "%s (%s)" % (path, _human(os.path.getsize(path))))
    else:
        report.add(INFO, "数据库文件", "尚未创建", "首次启动时初始化。")


def check_disk(report: Report) -> None:
    target = os.path.dirname(_database_path() or "") or "."
    if not os.path.isdir(target):
        target = "."
    try:
        usage = shutil.disk_usage(target)
    except Exception as exc:
        report.add(WARN, "磁盘空间", "无法读取: %s" % exc)
        return
    if usage.free < DISK_WARN_BYTES:
        report.add(
            WARN,
            "磁盘空间",
            "剩余 %s / 共 %s" % (_human(usage.free), _human(usage.total)),
            "任务事件表会持续增长；确认保留策略已生效或清理磁盘。",
        )
    else:
        report.add(PASS, "磁盘空间", "剩余 %s" % _human(usage.free))


def check_retention(report: Report) -> None:
    try:
        from core.retention import retention_days, retention_max_rows

        days = retention_days()
        cap = retention_max_rows()
    except Exception as exc:
        report.add(WARN, "日志保留", "无法读取配置: %s" % exc)
        return
    if days <= 0 and cap <= 0:
        report.add(
            WARN,
            "日志保留",
            "已完全关闭",
            "task_events 会无限增长。磁盘够大再关，或者调大窗口而不是关掉。",
        )
    else:
        report.add(PASS, "日志保留", "%d 天 / %s 行" % (days, cap if cap > 0 else "不限"))


def check_vnc(report: Report) -> None:
    enabled = str(os.environ.get("VNC_ENABLED", "") or "").strip() == "1"
    if not enabled:
        report.add(INFO, "VNC", "未启用", "需要人工接管浏览器时再开。")
        return
    password = str(os.environ.get("VNC_PASSWORD", "") or "").strip()
    bind = str(os.environ.get("VNC_BIND", "127.0.0.1") or "").strip()
    if not password and str(os.environ.get("ZCJ_ALLOW_INSECURE_VNC", "") or "").strip() != "1":
        report.add(
            FAIL,
            "VNC",
            "已启用但无密码",
            "x11vnc 会以 -nopw 启动，任何能连上端口的人都能接管已登录的浏览器会话。",
        )
    elif bind not in ("127.0.0.1", "localhost"):
        report.add(WARN, "VNC", "监听 %s" % bind, "建议只绑回环，通过 SSH 端口转发访问。")
    else:
        report.add(PASS, "VNC", "已启用，只监听 %s" % bind)


def run_checks() -> Report:
    report = Report()
    check_python(report)
    check_dependencies(report)
    check_timezone_data(report)
    check_auth(report)
    check_single_process(report)
    check_display(report)
    check_chrome(report)
    check_shm(report)
    check_database(report)
    check_disk(report)
    check_retention(report)
    check_vnc(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只在有警告或失败时输出",
    )
    args = parser.parse_args()

    report = run_checks()
    if not args.quiet or report.failures or report.warnings:
        print(report.render())
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
