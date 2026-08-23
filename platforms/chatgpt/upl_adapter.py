from __future__ import annotations

import os
import importlib.util
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


UPL_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "upl"
_URL_RE = re.compile(r"https://[^\s\"'<>]+")
_RESULT_HINTS = ("最终", "支付页 URL", "支付 URL", "扫码/授权 URL")
_IGNORED_RESULT_HOSTS = (
    "api.stripe.com",
    "chatgpt.com/backend-api/",
    "ipinfo.io",
    "ipapi.co",
    "ipwho.is",
    "ip-api.com",
    "api.myip.com",
)


def _clean_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _account_value(account: Any, *keys: str) -> str:
    extra = dict(getattr(account, "extra", {}) or {})
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return str(value).strip()
    for key in keys:
        value = getattr(account, key, None)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _proxy_lines(value: Any) -> list[str]:
    rows: list[str] = []
    for raw in str(value or "").replace("\r", "\n").split("\n"):
        row = raw.strip()
        if row and not row.startswith("#") and row not in rows:
            rows.append(row)
    return rows


def _write_private_lines(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _result_url_from_line(line: str, *, hinted: bool) -> str:
    urls = _URL_RE.findall(line)
    if not urls:
        return ""
    for url in reversed(urls):
        candidate = url.rstrip(".,);]}")
        lowered = candidate.lower()
        if any(host in lowered for host in _IGNORED_RESULT_HOSTS):
            continue
        if hinted or any(
            host in lowered
            for host in (
                "payments.stripe.com",
                "checkout.stripe.com",
                "pay.openai.com",
                "chatgpt.com/checkout/",
            )
        ):
            return candidate
    return ""


def extract_payment_link(
    account: Any,
    *,
    payment_method: str,
    params: dict[str, Any] | None = None,
    fallback_proxy: str = "",
    log_fn: Callable[[str], None] = print,
    cancel_check: Callable[[], bool] | None = None,
    upl_root: Path | None = None,
) -> dict[str, Any]:
    params = dict(params or {})
    method = str(payment_method or "").strip().lower()
    root = Path(upl_root or UPL_ROOT)
    module_path = root / "ideal_ui.py"
    spec = importlib.util.spec_from_file_location(
        f"_zcj_upl_ideal_ui_{abs(hash(str(root)))}",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"UPL 环境构建器缺失: {module_path}")
    upl_ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upl_ui)

    definition = upl_ui.PAYMENT_METHODS.get(method)
    if not definition or not definition.get("available"):
        raise ValueError(f"未知提链方式: {method}")
    script_path = Path(definition["script_path"])
    if not script_path.is_file():
        raise ValueError(f"UPL 脚本缺失: {script_path}")

    access_token = _account_value(account, "access_token", "accessToken", "token")
    session_token = _account_value(account, "session_token", "sessionToken")
    if not access_token:
        raise ValueError("账号缺少 access_token，请先刷新 Token")

    seed_rows = _proxy_lines(params.get("proxy_seeds"))
    explicit_proxy = str(params.get("proxy") or fallback_proxy or "").strip()
    if explicit_proxy and explicit_proxy not in seed_rows:
        seed_rows.append(explicit_proxy)
    independent_rows = {
        "checkout": _proxy_lines(params.get("checkout_proxies")),
        "promotion": _proxy_lines(params.get("promotion_proxies")),
        "provider": _proxy_lines(params.get("provider_proxies")),
    }
    independent = method == "upi" and any(independent_rows.values())
    if independent and not all(independent_rows.values()):
        raise ValueError("UPI 独立代理池需要同时填写 Checkout、Promotion、Provider 三组代理")
    if not seed_rows and not independent:
        raise ValueError("提链需要至少一条代理 Seed")

    timeout_seconds = _clean_int(params.get("timeout_seconds"), 600, 60, 1800)
    batch_size = _clean_int(params.get("batch_size"), 3, 1, 20)
    max_batches = _clean_int(params.get("max_batches"), 3, 1, 20)
    poll_timeout = _clean_int(params.get("poll_timeout"), 45, 5, 300)
    stopped = callable(cancel_check) and cancel_check()
    if stopped:
        raise RuntimeError("任务已取消")

    with tempfile.TemporaryDirectory(prefix=f"zcj-upl-{method}-") as temp_name:
        temp = Path(temp_name)
        seed_file = temp / "proxy_seeds.txt"
        _write_private_lines(seed_file, seed_rows or independent_rows["checkout"])
        payload: dict[str, Any] = {
            "token": access_token,
            "session_token": session_token,
            "proxy_seed_file": str(seed_file),
            "batch_size": batch_size,
            "max_batches": max_batches,
            "poll_timeout": poll_timeout,
            "promo_mode": str(params.get("promo_mode") or "campaign"),
            "promo_id": str(params.get("promo_id") or "plus-1-month-free"),
            "proxy_default_scheme": str(params.get("proxy_default_scheme") or "http"),
            "remove_failed": False,
            "blik_code": str(params.get("blik_code") or ""),
        }
        for key in ("bootstrap_country", "promotion_country", "provider_country"):
            value = str(params.get(key) or "").strip().upper()
            if value:
                payload[key] = value
        if independent:
            payload["upi_proxy_pool_mode"] = "independent"
            for stage, rows in independent_rows.items():
                path = temp / f"{stage}_proxies.txt"
                _write_private_lines(path, rows)
                payload[f"{stage}_file"] = str(path)

        env, public_config = upl_ui.build_environment(payload, method, definition)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        log_fn(
            f"UPL 提链开始: method={definition['label']} flow={public_config['payment_flow']} "
            f"batch={batch_size}x{max_batches}"
        )

        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(script_path.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            assert process.stdout is not None
            for output_line in process.stdout:
                lines.put(output_line.rstrip("\r\n"))
            lines.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        deadline = time.monotonic() + timeout_seconds
        result_url = ""
        awaiting_result = False
        tail: list[str] = []
        reader_done = False
        while not reader_done:
            if callable(cancel_check) and cancel_check():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise RuntimeError("任务已取消")
            if time.monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise TimeoutError(f"UPL 提链超时（{timeout_seconds} 秒）")
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None:
                    reader_done = True
                continue
            if line is None:
                reader_done = True
                continue
            if not line:
                continue
            tail.append(line)
            tail = tail[-20:]
            log_fn(f"[UPL] {line}")
            hinted = awaiting_result or any(marker in line for marker in _RESULT_HINTS)
            extracted = _result_url_from_line(line, hinted=hinted)
            if extracted:
                result_url = extracted
            awaiting_result = any(marker in line for marker in _RESULT_HINTS) and not extracted

        return_code = process.wait(timeout=5)
        if result_url:
            log_fn(f"UPL 提链成功: {definition['label']}")
            return {
                "url": result_url,
                "payment_url": result_url,
                "payment_method": method,
                "payment_label": definition["label"],
                "payment_flow": public_config["payment_flow"],
                "upstream_commit": "6667e4746e7597e9b7fd77dbc29c717771d9f3f5",
                "message": f"{definition['label']} 提链成功，链接已打开并复制",
            }
        reason = tail[-1] if tail else f"exit={return_code}"
        raise RuntimeError(f"UPL {definition['label']} 提链结束但未返回链接: {reason}")
