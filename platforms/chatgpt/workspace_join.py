from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests as std_requests
from curl_cffi import requests as cffi_requests

from core.base_mailbox import MailboxAccount
from .cpa_session import export_workspace_cpa_session_from_browser
from .constants import CHATGPT_APP


# Deployment-specific workspace IDs must be provided by the caller or config.
# Never ship a real workspace identifier in the public source tree.
DEFAULT_WORKSPACE_IDS = ""

# ── workspace 状态枚举 ──────────────────────────────────────────────
WORKSPACE_STATUS_PENDING = "pending"
WORKSPACE_STATUS_REQUEST_OK = "request_ok"
WORKSPACE_STATUS_REQUEST_FAILED = "request_failed"
WORKSPACE_STATUS_ACCEPT_OK = "accept_ok"
WORKSPACE_STATUS_ACCEPT_FAILED = "accept_failed"
WORKSPACE_STATUS_ACCEPT_SKIPPED = "accept_skipped"
WORKSPACE_STATUS_EXPORT_OK = "export_ok"
WORKSPACE_STATUS_EXPORT_FAILED = "export_failed"
WORKSPACE_STATUS_EXPORT_SKIPPED = "export_skipped"
WORKSPACE_STATUS_SESSION_STALE = "session_stale"

WORKSPACE_STATUS_LABELS: dict[str, str] = {
    WORKSPACE_STATUS_PENDING: "未处理",
    WORKSPACE_STATUS_REQUEST_OK: "已请求",
    WORKSPACE_STATUS_REQUEST_FAILED: "请求失败",
    WORKSPACE_STATUS_ACCEPT_OK: "已接受",
    WORKSPACE_STATUS_ACCEPT_FAILED: "接受失败",
    WORKSPACE_STATUS_ACCEPT_SKIPPED: "跳过接受",
    WORKSPACE_STATUS_EXPORT_OK: "已导出",
    WORKSPACE_STATUS_EXPORT_FAILED: "导出失败",
    WORKSPACE_STATUS_EXPORT_SKIPPED: "跳过导出",
    WORKSPACE_STATUS_SESSION_STALE: "Session 非 workspace",
}


def _new_workspace_status(status: str = WORKSPACE_STATUS_PENDING) -> dict[str, Any]:
    return {
        "status": status,
        "error": "",
        "json_path": "",
    }


_EXPORT_KEYS = [\
    "access_token","refresh_token","id_token","session_token",\
    "account_id","chatgpt_account_id","email","expires_at",\
]


def _update_workspace_status(
    statuses: dict[str, Any],
    workspace_id: str,
    status: str,
    *,
    error: str = "",
    json_path: str = "",
    credentials: dict[str, Any] | None = None,
) -> None:
    entry = dict(statuses.get(workspace_id) or _new_workspace_status())
    entry["status"] = status
    if error:
        entry["error"] = _truncate(error, 300)
    if json_path:
        entry["json_path"] = _truncate(json_path, 600)
    entry["updated_at"] = _utcnow_iso()
    if isinstance(credentials, dict):
        creds = {}
        for k in _EXPORT_KEYS:
            v = credentials.get(k)
            if v not in (None, ""):
                creds[k] = str(v)
        if creds:
            entry["credentials"] = creds
    statuses[workspace_id] = entry


def _merge_workspace_statuses(
    old_statuses: dict[str, Any] | None,
    new_ids: list[str],
) -> dict[str, Any]:
    """合并旧状态：保留已有 ID 的状态，新 ID 初始化为 pending。"""
    merged = dict(old_statuses or {})
    for ws_id in new_ids:
        if ws_id not in merged:
            merged[ws_id] = _new_workspace_status()
    return merged


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truncate(value: str, max_len: int) -> str:
    text = str(value or "")
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_workspace_ids(raw: Any) -> list[str]:
    text = str(raw or "").strip() or DEFAULT_WORKSPACE_IDS
    normalized = text.replace(",", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def _bool_config(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _int_config(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def workspace_join_enabled(extra: dict[str, Any] | None) -> bool:
    cfg = dict((extra or {}).get("chatgpt_workspace_join") or {})
    return _bool_config((extra or {}).get("auto_chatgpt_workspace_join", cfg.get("enabled")), False)


def workspace_join_config(extra: dict[str, Any] | None) -> dict[str, Any]:
    source = dict((extra or {}).get("chatgpt_workspace_join") or {})
    source.setdefault("workspace_ids", (extra or {}).get("workspace_ids", DEFAULT_WORKSPACE_IDS))
    source.setdefault("enabled", workspace_join_enabled(extra))
    source.setdefault("route", "request")
    source.setdefault("accept_invite", True)
    source.setdefault("export_cpa_json", True)
    source.setdefault("cpa_output_dir", "")
    source.setdefault("interval_ms", 1500)
    source.setdefault("max_retries", 3)
    source.setdefault("retry_backoff_ms", 5000)
    source.setdefault("invite_timeout", 30)
    source.setdefault("fresh_browser_export_timeout", 120)
    return source


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if callable(log):
        try:
            log(message)
        except Exception:
            pass


def _route_result_reason(result: dict[str, Any]) -> str:
    text = str(result.get("error") or result.get("text") or "").strip()
    return text[:180] if text else "-"


def _log_route_result(
    log: Callable[[str], None] | None,
    *,
    action: str,
    result: dict[str, Any],
) -> None:
    ws = str(result.get("workspace_id") or "")[:8] or "-"
    status = result.get("status") or "-"
    transport = str(result.get("transport") or "-")
    if result.get("ok"):
        _log(log, f"Workspace Join: {ws} {action} accepted HTTP {status} ({transport})")
    else:
        _log(
            log,
            f"Workspace Join: {ws} {action} failed HTTP {status} ({transport}): "
            f"{_route_result_reason(result)}",
        )


def _ensure_chatgpt_origin(page, log: Callable[[str], None] | None) -> None:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" in current_url.lower():
        return
    _log(log, "Workspace Join: 当前页不在 chatgpt.com，先打开 ChatGPT 首页")
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000)


def _fetch_access_token_from_page(page, log: Callable[[str], None] | None) -> str:
    _ensure_chatgpt_origin(page, log)
    data = page.evaluate(
        """
        async () => {
          const response = await fetch("/api/auth/session", {
            headers: { accept: "*/*" },
            credentials: "include",
          });
          const text = await response.text().catch(() => "");
          let json = {};
          try { json = text ? JSON.parse(text) : {}; } catch (_) {}
          return {
            ok: response.ok,
            status: response.status,
            accessToken: json.accessToken || json.access_token || "",
            text: text.slice(0, 300),
          };
        }
        """
    )
    result = dict(data or {})
    token = str(result.get("accessToken") or "").strip()
    if token:
        _log(log, "Workspace Join: got accessToken from current ChatGPT page")
        return token
    raise RuntimeError(
        f"ChatGPT session did not return accessToken: HTTP {result.get('status')} "
        f"{str(result.get('text') or '')[:160]}"
    )


def _is_page_unavailable_error(exc: BaseException) -> bool:
    message = str(exc or "").lower()
    return any(
        token in message
        for token in (
            "connection closed while reading from the driver",
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "context has been closed",
        )
    )


def _parse_cookie_header(cookies: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in str(cookies or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name:
            parsed[name] = value.strip()
    return parsed


def _post_workspace_join_http(
    *,
    access_token: str,
    workspace_id: str,
    route: str,
    device_id: str,
    cookies: str = "",
    proxy: str = "",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    url = f"{CHATGPT_APP}/backend-api/accounts/{workspace_id}/invites/{route}"
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "origin": CHATGPT_APP,
        "referer": f"{CHATGPT_APP}/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    cookie_header = str(cookies or "").strip()
    if cookie_header:
        headers["cookie"] = cookie_header

    proxy_url = str(proxy or "").strip()
    transports = ("curl_cffi", "requests") if proxy_url else ("requests", "curl_cffi")
    errors: list[str] = []

    for transport in transports:
        _log(
            log,
            f"Workspace Join: HTTP fallback using {transport} proxy={'yes' if proxy_url else 'no'}",
        )
        session = None
        try:
            if transport == "curl_cffi":
                session_kwargs: dict[str, Any] = {"impersonate": "chrome120"}
                if proxy_url:
                    session_kwargs["proxy"] = proxy_url
                session = cffi_requests.Session(**session_kwargs)
                response = session.post(url, headers=headers, data="", timeout=15)
            else:
                request_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "data": "",
                    "timeout": 45,
                }
                if proxy_url:
                    request_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
                response = std_requests.post(url, **request_kwargs)

            text = str(getattr(response, "text", "") or "")
            status = int(getattr(response, "status_code", 0) or 0)
            return {
                "ok": 200 <= status < 300,
                "status": status,
                "url": str(getattr(response, "url", "") or url),
                "text": text[:500],
                "workspace_id": workspace_id,
                "transport": f"http-{transport}",
            }
        except Exception as exc:
            errors.append(f"{transport}: {exc}")
            _log(log, f"Workspace Join: HTTP fallback {transport} failed: {exc}")
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    error_text = " | ".join(errors)
    return {
        "ok": False,
        "status": 0,
        "url": url,
        "text": error_text[:500],
        "workspace_id": workspace_id,
        "transport": "http",
        "error": error_text,
    }


def _accept_workspace_invite_http(
    *,
    access_token: str,
    workspace_id: str,
    device_id: str,
    cookies: str = "",
    proxy: str = "",
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not workspace_id:
        return {
            "ok": False,
            "status": 0,
            "url": "",
            "text": "missing workspace id",
            "workspace_id": workspace_id,
            "transport": "http",
            "error": "missing workspace id",
        }
    _log(log, f"Workspace Join: POST /accounts/{workspace_id[:8]}/invites/accept")
    return _post_workspace_join_http(
        access_token=access_token,
        workspace_id=workspace_id,
        route="accept",
        device_id=device_id,
        cookies=cookies,
        proxy=proxy,
        log=log,
    )


def request_workspace_join_in_browser(
    page,
    *,
    access_token: str = "",
    cookies: str = "",
    proxy: str = "",
    workspace_ids: list[str],
    route: str = "request",
    interval_ms: int = 1500,
    max_retries: int = 3,
    retry_backoff_ms: int = 5000,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    fallback_token = str(access_token or "").strip()
    cookie_header = str(cookies or "").strip()
    page_unavailable = False
    use_http_transport = bool(fallback_token)

    if fallback_token:
        access_token = fallback_token
        _log(log, "Workspace Join: using registration accessToken for HTTP join request")
    else:
        try:
            _ensure_chatgpt_origin(page, log)
        except Exception as exc:
            if not _is_page_unavailable_error(exc):
                raise
            page_unavailable = True
            _log(
                log,
                f"Workspace Join: page unavailable before request, using HTTP fallback: {exc}",
            )
        try:
            access_token = fallback_token if page_unavailable else _fetch_access_token_from_page(page, log)
        except Exception as exc:
            if not _is_page_unavailable_error(exc):
                raise
            page_unavailable = True
            _log(log, f"Workspace Join: page session fetch failed: {exc}")
            access_token = fallback_token
    if not access_token:
        raise RuntimeError("缺少 ChatGPT access_token，无法发送 workspace join request")

    results: list[dict[str, Any]] = []
    device_id = _parse_cookie_header(cookie_header).get("oai-did") or str(uuid.uuid4())
    normalized_route = str(route or "request").strip() or "request"

    for index, ws_id in enumerate(workspace_ids):
        last_result: dict[str, Any] = {}
        for attempt in range(max(int(max_retries), 0) + 1):
            _log(
                log,
                f"Workspace Join: POST /accounts/{ws_id[:8]}/invites/{normalized_route} "
                f"(第 {attempt + 1} 次)",
            )
            if page_unavailable or use_http_transport:
                last_result = _post_workspace_join_http(
                    access_token=access_token,
                    workspace_id=ws_id,
                    route=normalized_route,
                    device_id=device_id,
                    cookies=cookie_header,
                    proxy=proxy,
                    log=log,
                )
                if page_unavailable:
                    last_result["page_unavailable"] = True
            else:
                try:
                    result = page.evaluate(
                        """
                        async ({ wsId, route, token, deviceId }) => {
                          const response = await fetch(`/backend-api/accounts/${wsId}/invites/${route}`, {
                            method: "POST",
                            credentials: "include",
                            mode: "cors",
                            headers: {
                              accept: "*/*",
                              authorization: `Bearer ${token}`,
                              "content-type": "application/json",
                              "oai-device-id": deviceId,
                              "oai-language": navigator.language || "en-US",
                            },
                            body: "",
                          });
                          const text = await response.text().catch(() => "");
                          return {
                            ok: response.ok,
                            status: response.status,
                            url: response.url,
                            text: text.slice(0, 500),
                          };
                        }
                        """,
                        {
                            "wsId": ws_id,
                            "route": normalized_route,
                            "token": access_token,
                            "deviceId": device_id,
                        },
                    )
                    last_result = dict(result or {})
                    last_result["workspace_id"] = ws_id
                    last_result["transport"] = "browser"
                except Exception as exc:
                    if not _is_page_unavailable_error(exc):
                        raise
                    page_unavailable = True
                    _log(log, f"Workspace Join: browser request failed, fallback HTTP: {exc}")
                    last_result = _post_workspace_join_http(
                        access_token=access_token,
                        workspace_id=ws_id,
                        route=normalized_route,
                        device_id=device_id,
                        cookies=cookie_header,
                        proxy=proxy,
                        log=log,
                    )
                    last_result["page_unavailable"] = True
            if last_result.get("ok"):
                _log(log, f"Workspace Join: {ws_id[:8]} {normalized_route} 成功 HTTP {last_result.get('status')}")
                break
            if last_result.get("status") in (401, 403) and attempt < max(int(max_retries), 0):
                if page_unavailable or use_http_transport:
                    _log(
                        log,
                        "Workspace Join: HTTP accessToken rejected; retrying previous token without touching browser",
                    )
                else:
                    _log(log, "Workspace Join: accessToken rejected, refreshing session from page")
                    try:
                        access_token = _fetch_access_token_from_page(page, log)
                    except Exception as exc:
                        if _is_page_unavailable_error(exc):
                            page_unavailable = True
                        _log(
                            log,
                            f"Workspace Join: session refresh failed, retrying with previous token: {exc}",
                        )
            _log(
                log,
                f"Workspace Join: {ws_id[:8]} {normalized_route} 失败 HTTP {last_result.get('status')}: "
                f"{str(last_result.get('text') or '')[:180]}",
            )
            if attempt < max(int(max_retries), 0):
                time.sleep(max(int(retry_backoff_ms), 0) / 1000)
        results.append(last_result)
        if index < len(workspace_ids) - 1:
            time.sleep(max(int(interval_ms), 0) / 1000)
    return results


def open_workspace_invite_in_browser(
    page,
    invite_url: str,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    url = str(invite_url or "").strip()
    if not url:
        return {"ok": False, "error": "empty invite url"}

    invite_workspace_id = _workspace_id_from_invite_url(url, [])
    _log(log, f"Workspace Join: open invite link wId={invite_workspace_id or '-'}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_timeout(1000)
    except Exception:
        time.sleep(1)

    clicked = False
    clicked_text = ""
    try:
        click_result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== "none" && style.visibility !== "hidden" &&
                  rect.width > 0 && rect.height > 0;
              };
              const pattern = /加入工作空间|加入工作區|转至\\s*ChatGPT\\s*for\\s*Teachers|轉至\\s*ChatGPT\\s*for\\s*Teachers|Go to\\s*ChatGPT\\s*for\\s*Teachers|Join workspace|Accept invite|Accept invitation|Continue/i;
              const nodes = Array.from(document.querySelectorAll("button, a, [role='button']"));
              const target = nodes.find((el) => visible(el) && pattern.test(String(el.innerText || el.textContent || el.getAttribute("aria-label") || "")));
              if (!target) return { clicked: false, text: "", url: location.href };
              const text = String(target.innerText || target.textContent || target.getAttribute("aria-label") || "").trim();
              target.click();
              return { clicked: true, text, url: location.href };
            }
            """
        )
        clicked = bool((click_result or {}).get("clicked"))
        clicked_text = str((click_result or {}).get("text") or "")
    except Exception as exc:
        _log(log, f"Workspace Join: 邀请页按钮点击探测失败，继续观察页面: {exc}")

    if not clicked:
        last_candidates: list[str] = []
        last_error = ""
        click_script = """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== "none" && style.visibility !== "hidden" &&
                  rect.width > 0 && rect.height > 0;
              };
              const textOf = (el) => String(
                el.innerText || el.textContent || el.getAttribute("aria-label") || ""
              ).replace(/\\s+/g, " ").trim();
              const pattern = /加入工作空间|加入工作區|转至\\s*ChatGPT\\s*for\\s*Teachers|轉至\\s*ChatGPT\\s*for\\s*Teachers|Go to\\s*ChatGPT\\s*for\\s*Teachers|ChatGPT\\s*for\\s*Teachers|Join workspace|Accept invite|Accept invitation|Continue/i;
              const nodes = Array.from(document.querySelectorAll("button, a, [role='button'], [role='link']"));
              const candidates = nodes
                .filter((el) => visible(el))
                .map((el) => textOf(el))
                .filter(Boolean)
                .slice(0, 12);
              const target = nodes.find((el) => visible(el) && pattern.test(textOf(el)));
              if (!target) return { clicked: false, text: "", candidates, url: location.href };
              const text = textOf(target);
              try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
              target.click();
              return { clicked: true, text, candidates, url: location.href };
            }
            """
        deadline = time.monotonic() + 30
        attempts = 0
        while attempts < 60 and time.monotonic() <= deadline:
            attempts += 1
            try:
                click_result = page.evaluate(click_script)
                clicked = bool((click_result or {}).get("clicked"))
                clicked_text = str((click_result or {}).get("text") or "")
                raw_candidates = (click_result or {}).get("candidates") or []
                if isinstance(raw_candidates, list):
                    last_candidates = [
                        str(item)[:80] for item in raw_candidates if str(item).strip()
                    ]
                if clicked:
                    break
            except Exception as exc:
                last_error = str(exc)
            if time.monotonic() > deadline:
                break
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        if last_error and not clicked:
            _log(log, f"Workspace Join: invite button click probe failed: {last_error}")
        if last_candidates and not clicked:
            _log(log, f"Workspace Join: invite page button candidates: {last_candidates}")

    try:
        page.wait_for_timeout(2500 if clicked else 1200)
    except Exception:
        time.sleep(2.5 if clicked else 1.2)

    final_url = str(getattr(page, "url", "") or "")
    if clicked:
        _log(log, f"Workspace Join: 已点击邀请页按钮 {clicked_text or '-'}")
    if not clicked:
        _log(log, "Workspace Join: invite button not clicked; acceptance not confirmed")
    return {
        "ok": clicked,
        "invite_url": url,
        "clicked": clicked,
        "clicked_text": clicked_text,
        "final_url": final_url,
        "error": "" if clicked else "invite button not clicked",
    }


def _refresh_chatgpt_page_for_workspace_export(page, log: Callable[[str], None] | None = None) -> bool:
    try:
        _log(log, "Workspace Join: refresh ChatGPT page before switching workspace")
        page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=60000)
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            wait_for_timeout(1200)
        else:
            time.sleep(1.2)
        reload_page = getattr(page, "reload", None)
        if callable(reload_page):
            reload_page(wait_until="domcontentloaded", timeout=60000)
            if callable(wait_for_timeout):
                wait_for_timeout(1200)
            else:
                time.sleep(1.2)
        _log(log, "Workspace Join: browser page refreshed; continue workspace export")
        return True
    except Exception as exc:
        _log(log, f"Workspace Join: browser page refresh failed; will try fresh browser recovery: {exc}")
        return False


def _proxy_to_launch_config(proxy: str) -> dict[str, str] | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config: dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _cookie_header_to_browser_cookies(cookies: str, session_token: str = "") -> list[dict[str, str]]:
    parsed = _parse_cookie_header(cookies)
    token = str(session_token or "").strip()
    if token and not parsed.get("__Secure-next-auth.session-token"):
        parsed["__Secure-next-auth.session-token"] = token
    if not parsed:
        return []
    urls = (f"{CHATGPT_APP}/", "https://auth.openai.com/", "https://openai.com/")
    browser_cookies: list[dict[str, str]] = []
    for url in urls:
        for name, value in parsed.items():
            if not name or value is None:
                continue
            browser_cookies.append({"name": str(name), "value": str(value), "url": url})
    return browser_cookies


def _add_cookies_best_effort(context, cookies: list[dict[str, str]]) -> int:
    added = 0
    for cookie in cookies:
        try:
            context.add_cookies([cookie])
            added += 1
        except Exception:
            continue
    return added


def _run_with_timeout(
    fn: Callable[[], dict[str, Any]],
    *,
    timeout_sec: int,
    label: str,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    parent_logger = getattr(log_fn, "__self__", None)
    parent_subtask = None
    if parent_logger is not None and hasattr(parent_logger, "_current_subtask"):
        try:
            parent_subtask = parent_logger._current_subtask()
        except Exception:
            parent_subtask = None

    def _target() -> None:
        if parent_logger is not None and parent_subtask and parent_subtask[0]:
            try:
                parent_logger.set_subtask(parent_subtask[0], parent_subtask[1])
            except Exception:
                pass
        try:
            result_queue.put(("ok", fn()), block=False)
        except Exception as exc:
            try:
                result_queue.put(("error", exc), block=False)
            except Exception:
                pass
        finally:
            if parent_logger is not None and parent_subtask and parent_subtask[0]:
                try:
                    parent_logger.clear_subtask()
                except Exception:
                    pass

    thread = threading.Thread(target=_target, name=label, daemon=True)
    thread.start()
    try:
        status, payload = result_queue.get(timeout=max(int(timeout_sec), 1))
    except queue.Empty as exc:
        _log(log_fn, f"Workspace Join: {label} timed out after {timeout_sec}s")
        raise TimeoutError(f"{label} timed out after {timeout_sec}s") from exc
    if status == "error":
        raise payload
    return payload


def _export_workspace_cpa_session_from_fresh_browser(
    *,
    session_info: dict[str, Any],
    workspace_id: str,
    output_dir: str | None,
    proxy: str,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    cookies = str(session_info.get("cookies") or "")
    session_token = str(session_info.get("session_token") or "")
    browser_cookies = _cookie_header_to_browser_cookies(cookies, session_token=session_token)
    if not browser_cookies:
        raise RuntimeError("missing cookies/session_token for fresh browser recovery")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"Playwright unavailable for fresh browser recovery: {exc}") from exc

    launch_opts: dict[str, Any] = {"headless": True, "timeout": 30000}
    proxy_config = _proxy_to_launch_config(proxy)
    if proxy_config:
        launch_opts["proxy"] = proxy_config

    _log(log, "Workspace Join: opening fresh Playwright browser for workspace CPA export")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_opts)
        try:
            context = browser.new_context()
            added = _add_cookies_best_effort(context, browser_cookies)
            if added <= 0:
                raise RuntimeError("failed to inject cookies into fresh browser")
            _log(log, f"Workspace Join: injected cookies into fresh browser count={added}")
            page = context.new_page()
            _log(log, "Workspace Join: fresh browser opening ChatGPT home")
            page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_timeout(1200)
            except Exception:
                time.sleep(1.2)
            _log(log, "Workspace Join: fresh browser exporting workspace CPA JSON")
            return export_workspace_cpa_session_from_browser(
                page,
                workspace_id=workspace_id,
                output_dir=output_dir,
                log=log,
            )
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _export_workspace_cpa_session_from_fresh_browser_with_timeout(
    *,
    session_info: dict[str, Any],
    workspace_id: str,
    output_dir: str | None,
    proxy: str,
    log: Callable[[str], None] | None = None,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    _log(log, f"Workspace Join: fresh browser export thread started timeout={timeout_sec}s")
    return _run_with_timeout(
        lambda: _export_workspace_cpa_session_from_fresh_browser(
            session_info=session_info,
            workspace_id=workspace_id,
            output_dir=output_dir,
            proxy=proxy,
            log=log,
        ),
        timeout_sec=timeout_sec,
        label="workspace-cpa-fresh-browser-export",
        log_fn=log,
    )


def _workspace_id_from_invite_url(invite_url: str, fallback_ids: list[str]) -> str:
    try:
        values = parse_qs(urlparse(str(invite_url or "")).query)
        for key in ("wId", "wid", "workspace_id", "workspaceId"):
            value = (values.get(key) or [""])[0]
            if value:
                return str(value)
    except Exception:
        pass
    return fallback_ids[0] if fallback_ids else ""


def run_workspace_join_flow(
    page,
    session_info: dict[str, Any],
    *,
    mailbox,
    mailbox_account: MailboxAccount | None,
    config: dict[str, Any],
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    workspace_ids = parse_workspace_ids(config.get("workspace_ids"))
    if not workspace_ids:
        return {"workspace_join": {"ok": False, "error": "no workspace ids"}}

    # ── 公共状态：合并旧 workspace_statuses，不覆盖已有 ID 的记录 ──
    old_statuses = _safe_dict(session_info.get("workspace_statuses"))
    workspace_statuses = _merge_workspace_statuses(old_statuses, workspace_ids)

    request_access_token = str(session_info.get("access_token") or "")
    request_cookies = str(session_info.get("cookies") or "")
    request_proxy = str(
        session_info.get("proxy")
        or session_info.get("_proxy")
        or config.get("proxy")
        or ""
    )
    request_device_id = _parse_cookie_header(request_cookies).get("oai-did") or str(uuid.uuid4())
    output_dir = str(config.get("cpa_output_dir") or "").strip() or None
    accept_invite = _bool_config(config.get("accept_invite"), True)
    export_enabled = _bool_config(config.get("export_cpa_json"), True)

    # ── 邮箱基线 ─────────────────────────────────────────────────
    before_ids = set()
    mailbox_available = mailbox is not None and mailbox_account is not None
    if mailbox_available:
        try:
            before_ids = set(mailbox.get_current_ids(mailbox_account) or set())
            _log(log, f"Workspace Join: 邮箱邀请基线 before_ids={len(before_ids)}")
        except Exception as exc:
            _log(log, f"Workspace Join: 邮箱基线读取失败，继续等待新邮件: {exc}")
    else:
        _log(log, "Workspace Join: 缺少 mailbox 上下文，将只发送 request，不自动收邀请")

    # ── 逐 ID 处理 ───────────────────────────────────────────────
    per_id_results: list[dict[str, Any]] = []
    invite_url_used: str | None = None
    top_level_updates: dict[str, Any] = {}

    for idx, ws_id in enumerate(workspace_ids, start=1):
        _log(log, f"Workspace Join: [{idx}/{len(workspace_ids)}] 开始处理 {ws_id}")
        id_result: dict[str, Any] = {
            "workspace_id": ws_id,
            "request_ok": False,
            "accept_ok": False,
            "export_ok": False,
        }

        # ── Step 1: Request ──────────────────────────────────────
        _update_workspace_status(workspace_statuses, ws_id, WORKSPACE_STATUS_PENDING)
        try:
            req_results = request_workspace_join_in_browser(
                page,
                access_token=request_access_token,
                cookies=request_cookies,
                proxy=request_proxy,
                workspace_ids=[ws_id],
                route=str(config.get("route") or "request"),
                interval_ms=_int_config(config.get("interval_ms"), 1500),
                max_retries=_int_config(config.get("max_retries"), 3),
                retry_backoff_ms=_int_config(config.get("retry_backoff_ms"), 5000),
                log=log,
            )
            id_result["request_results"] = req_results
            req_ok = bool(req_results and req_results[0].get("ok"))
        except Exception as exc:
            _log(log, f"Workspace Join: [{ws_id}] request 异常: {exc}")
            id_result["request_error"] = str(exc)
            req_ok = False

        id_result["request_ok"] = req_ok
        if not req_ok:
            error = str(
                id_result.get("request_error")
                or (id_result.get("request_results") or [{}])[0].get("error", "")
                or "request failed"
            )
            _update_workspace_status(workspace_statuses, ws_id, WORKSPACE_STATUS_REQUEST_FAILED, error=error)
            _log(log, f"Workspace Join: [{ws_id}] request 失败，跳过后续步骤")
            per_id_results.append(id_result)
            continue
        _update_workspace_status(workspace_statuses, ws_id, WORKSPACE_STATUS_REQUEST_OK)

        # ── Step 2: Accept ───────────────────────────────────────
        if not accept_invite or not mailbox_available:
            id_result["accept_skipped"] = True
            per_id_results.append(id_result)
            continue

        invite_target_id = ws_id
        accept_result = None

        # 第一个有 mailbox 的 ID 尝试通过邮件邀请链接接受
        if idx == 1 and invite_url_used is None:
            try:
                timeout = min(_int_config(config.get("invite_timeout"), 30), 30)
                _log(log, f"Workspace Join: 等待 {ws_id} 的邀请邮件 k12-invite, timeout={timeout}s")
                invite_url = mailbox.wait_for_link(
                    mailbox_account,
                    keyword="k12-invite",
                    timeout=timeout,
                    before_ids=before_ids or None,
                )
                id_result["invite_url"] = str(invite_url or "")
                if invite_url:
                    invite_url_used = invite_url
                    parsed_invite = _workspace_id_from_invite_url(invite_url, workspace_ids)
                    if parsed_invite:
                        invite_target_id = parsed_invite
                        _log(log, f"Workspace Join: 邀请链接 wId = {invite_target_id}")
            except Exception as exc:
                _log(log, f"Workspace Join: 等待邀请邮件失败: {exc}, 回退 HTTP accept")
                id_result["invite_url"] = ""

            if id_result.get("invite_url"):
                try:
                    accept_result = open_workspace_invite_in_browser(page, id_result["invite_url"], log=log)
                except Exception as exc:
                    if _is_page_unavailable_error(exc):
                        _log(log, f"Workspace Join: 浏览器打开邀请失败，回退 HTTP accept: {exc}")
                        accept_result = _accept_workspace_invite_http(
                            access_token=request_access_token, workspace_id=invite_target_id,
                            device_id=request_device_id, cookies=request_cookies,
                            proxy=request_proxy, log=log,
                        )
                    else:
                        id_result["accept_error"] = str(exc)
            else:
                accept_result = _accept_workspace_invite_http(
                    access_token=request_access_token, workspace_id=invite_target_id,
                    device_id=request_device_id, cookies=request_cookies,
                    proxy=request_proxy, log=log,
                )
        else:
            _log(log, f"Workspace Join: [{ws_id}] 使用 HTTP 直接 accept")
            accept_result = _accept_workspace_invite_http(
                access_token=request_access_token, workspace_id=invite_target_id,
                device_id=request_device_id, cookies=request_cookies,
                proxy=request_proxy, log=log,
            )

        id_result["accept_result"] = accept_result
        accept_ok = bool(accept_result and accept_result.get("ok"))
        id_result["accept_ok"] = accept_ok
        if not accept_ok:
            error = str(
                id_result.get("accept_error")
                or (accept_result or {}).get("error", "")
                or f"HTTP {(accept_result or {}).get('status', '-')}"
            )
            _update_workspace_status(workspace_statuses, ws_id, WORKSPACE_STATUS_ACCEPT_FAILED, error=error)
            per_id_results.append(id_result)
            continue
        _update_workspace_status(workspace_statuses, ws_id, WORKSPACE_STATUS_ACCEPT_OK)

        # ── Step 3: Export ───────────────────────────────────────
        if not export_enabled:
            id_result["export_skipped"] = True
            per_id_results.append(id_result)
            continue

        try:
            _log(log, f"Workspace Join: [{ws_id}] 切换 workspace 并导出 CPA JSON")
            export_result = export_workspace_cpa_session_from_browser(
                page,
                workspace_id=invite_target_id,
                output_dir=output_dir,
                log=log,
            )
            id_result["cpa_export"] = {
                key: value
                for key, value in (export_result or {}).items()
                if key not in {"access_token", "refresh_token", "id_token", "session_token"}
            }
            if isinstance(export_result, dict) and export_result.get("ok"):
                json_path = str(export_result.get("path", ""))
                # ★ 每个成功导出的 workspace 都把凭证存入状态条目
                ws_creds = {
                    "workspace_id": invite_target_id,
                }
                for key in _EXPORT_KEYS:
                    v = export_result.get(key)
                    if v not in (None, ""):
                        ws_creds[key] = str(v)
                _update_workspace_status(
                    workspace_statuses, ws_id, WORKSPACE_STATUS_EXPORT_OK,
                    json_path=json_path, credentials=ws_creds,
                )
                id_result["export_ok"] = True

                # 第一个导出成功的写到 top_level_updates（兼容旧 account 级凭证）
                if not top_level_updates:
                    for source_key, target_key in (
                        ("access_token", "access_token"),
                        ("refresh_token", "refresh_token"),
                        ("id_token", "id_token"),
                        ("session_token", "session_token"),
                        ("account_id", "account_id"),
                        ("chatgpt_account_id", "chatgpt_account_id"),
                        ("expired", "expires_at"),
                    ):
                        value = export_result.get(source_key)
                        if value not in (None, ""):
                            top_level_updates[target_key] = value
                    top_level_updates["workspace_id"] = invite_target_id
            else:
                error = str((export_result or {}).get("error", "export failed"))
                _update_workspace_status(workspace_statuses, ws_id, WORKSPACE_STATUS_EXPORT_FAILED, error=error)
        except Exception as exc:
            error = f"CPA JSON export failed: {exc}"
            _update_workspace_status(workspace_statuses, ws_id, WORKSPACE_STATUS_EXPORT_FAILED, error=error)
            id_result["export_error"] = str(exc)
            _log(log, f"Workspace Join: [{ws_id}] {error}")

        per_id_results.append(id_result)
        _log(log, f"Workspace Join: [{idx}/{len(workspace_ids)}] {ws_id} 处理完成")

    # ── 汇总结果 ──────────────────────────────────────────────────
    # ok 判断：expost 开启 → 看 export_ok；accept 开启 → 看 accept_ok；否则看 request_ok
    if export_enabled:
        flow_ok = any(item.get("export_ok") for item in per_id_results)
    elif accept_invite:
        # 跳过接受也算 accept_skipped → 看是否有真正 accept_ok
        flow_ok = any(item.get("accept_ok") for item in per_id_results)
    else:
        flow_ok = any(item.get("request_ok") for item in per_id_results)

    summary: dict[str, Any] = {
        "ok": flow_ok,
        "workspace_ids": workspace_ids,
        "workspace_statuses": workspace_statuses,
        "per_id_results": per_id_results,
        "summary": {
            "total": len(workspace_ids),
            "request_ok": sum(1 for item in per_id_results if item.get("request_ok")),
            "accept_ok": sum(1 for item in per_id_results if item.get("accept_ok")),
            "export_ok": sum(1 for item in per_id_results if item.get("export_ok")),
        },
    }

    if not flow_ok:
        summary["error"] = "no workspace reached required stage"

    return {"workspace_join": summary, **top_level_updates, "workspace_statuses": workspace_statuses}
