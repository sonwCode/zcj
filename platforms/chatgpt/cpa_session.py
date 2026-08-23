from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import CHATGPT_APP


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _base64url_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_jwt_payload(token: str | None) -> dict[str, Any]:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _openai_auth(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(payload.get("https://api.openai.com/auth"))


def _openai_profile(payload: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(payload.get("https://api.openai.com/profile"))


def _normalize_timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            number = float(value)
            dt = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _epoch_seconds(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return int(number / 1000 if number > 1e11 else number)
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return 0


def build_synthetic_codex_id_token(
    *,
    email: str = "",
    account_id: str = "",
    plan_type: str = "",
    user_id: str = "",
    expires_at: str = "",
) -> str:
    if not account_id:
        return ""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    auth_info: dict[str, Any] = {"chatgpt_account_id": account_id}
    if plan_type:
        auth_info["chatgpt_plan_type"] = plan_type
    if user_id:
        auth_info["chatgpt_user_id"] = user_id
        auth_info["user_id"] = user_id
    payload: dict[str, Any] = {
        "iat": now,
        "exp": _epoch_seconds(expires_at) or now + 90 * 24 * 60 * 60,
        "https://api.openai.com/auth": auth_info,
    }
    if email:
        payload["email"] = email
    return f"{_base64url_json({'alg': 'none', 'typ': 'JWT', 'cpa_synthetic': True})}.{_base64url_json(payload)}."


def _workspace_id_from_session(
    session: dict[str, Any],
    account: dict[str, Any],
    provider_data: dict[str, Any],
    credentials: dict[str, Any],
    auth: dict[str, Any],
    id_auth: dict[str, Any],
) -> str:
    return _first_text(
        session.get("workspace_id"),
        session.get("workspaceId"),
        session.get("organization_id"),
        session.get("organizationId"),
        account.get("workspace_id"),
        account.get("workspaceId"),
        account.get("organization_id"),
        account.get("organizationId"),
        provider_data.get("workspace_id"),
        provider_data.get("workspaceId"),
        provider_data.get("organization_id"),
        provider_data.get("organizationId"),
        credentials.get("workspace_id"),
        credentials.get("workspaceId"),
        credentials.get("organization_id"),
        credentials.get("organizationId"),
        auth.get("workspace_id"),
        auth.get("workspaceId"),
        auth.get("organization_id"),
        auth.get("organizationId"),
        id_auth.get("workspace_id"),
        id_auth.get("workspaceId"),
        id_auth.get("organization_id"),
        id_auth.get("organizationId"),
    )


def _is_workspace_plan_type(plan_type: str) -> bool:
    raw = str(plan_type or "").strip().lower()
    if not raw:
        return False
    if any(token in raw for token in ("free", "personal")):
        return False
    return any(
        token in raw
        for token in (
            "workspace",
            "team",
            "business",
            "enterprise",
            "edu",
            "education",
            "teacher",
            "school",
            "k12",
        )
    )


def assert_workspace_cpa_json(cpa_json: dict[str, Any], *, workspace_id: str = "") -> None:
    plan_type = str(cpa_json.get("chatgpt_plan_type") or cpa_json.get("plan_type") or "").strip()
    account_id = str(cpa_json.get("chatgpt_account_id") or cpa_json.get("account_id") or "").strip()
    suffix = f", expected_workspace_id={workspace_id}" if workspace_id else ""
    if _is_workspace_plan_type(plan_type) and account_id:
        expected = str(workspace_id or "").strip()
        if not expected or account_id.lower() == expected.lower():
            return
        raise ValueError(
            "session is in a different workspace context after switch: "
            f"plan_type={plan_type or '-'}, account_id={account_id or '-'}{suffix}; "
            "refusing to save mismatched workspace JSON"
        )
    raise ValueError(
        "session is not in workspace context after switch: "
        f"plan_type={plan_type or '-'}, account_id={account_id or '-'}{suffix}; "
        "refusing to save personal/free JSON"
    )


def convert_chatgpt_session_to_cpa_json(
    session: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(session, dict):
        raise ValueError("session must be a JSON object")

    token = _as_dict(session.get("token"))
    credentials = _as_dict(session.get("credentials"))
    provider_data = _as_dict(session.get("providerSpecificData"))
    account = _as_dict(session.get("account"))
    user = _as_dict(session.get("user"))

    access_token = _first_text(
        session.get("accessToken"),
        session.get("access_token"),
        token.get("accessToken"),
        token.get("access_token"),
        credentials.get("accessToken"),
        credentials.get("access_token"),
    )
    if not access_token:
        raise ValueError("missing accessToken")

    session_token = _first_text(
        session.get("sessionToken"),
        session.get("session_token"),
        token.get("sessionToken"),
        token.get("session_token"),
        credentials.get("session_token"),
    )
    refresh_token = _first_text(
        session.get("refreshToken"),
        session.get("refresh_token"),
        token.get("refreshToken"),
        token.get("refresh_token"),
        credentials.get("refresh_token"),
    )
    input_id_token = _first_text(
        session.get("idToken"),
        session.get("id_token"),
        token.get("idToken"),
        token.get("id_token"),
        credentials.get("id_token"),
    )

    payload = parse_jwt_payload(access_token)
    id_payload = parse_jwt_payload(input_id_token)
    auth = _openai_auth(payload)
    id_auth = _openai_auth(id_payload)
    profile = _openai_profile(payload)

    expires_at = _first_text(
        _normalize_timestamp(payload.get("exp")),
        _normalize_timestamp(session.get("expires")),
        _normalize_timestamp(session.get("expiresAt")),
        _normalize_timestamp(session.get("expired")),
        _normalize_timestamp(session.get("expires_at")),
    )
    email = _first_text(
        user.get("email"),
        session.get("email"),
        credentials.get("email"),
        provider_data.get("email"),
        profile.get("email"),
        id_payload.get("email"),
        payload.get("email"),
    )
    account_id = _first_text(
        account.get("id"),
        session.get("account_id"),
        session.get("chatgptAccountId"),
        provider_data.get("chatgptAccountId"),
        provider_data.get("chatgpt_account_id"),
        credentials.get("chatgpt_account_id"),
        auth.get("chatgpt_account_id"),
        id_auth.get("chatgpt_account_id"),
        session.get("id") if session.get("provider") == "codex" else "",
    )
    user_id = _first_text(
        user.get("id"),
        session.get("user_id"),
        session.get("chatgptUserId"),
        provider_data.get("chatgptUserId"),
        provider_data.get("chatgpt_user_id"),
        auth.get("chatgpt_user_id"),
        auth.get("user_id"),
        id_auth.get("chatgpt_user_id"),
        id_auth.get("user_id"),
    )
    plan_type = _first_text(
        account.get("planType"),
        account.get("plan_type"),
        session.get("planType"),
        session.get("plan_type"),
        provider_data.get("chatgptPlanType"),
        provider_data.get("chatgpt_plan_type"),
        credentials.get("plan_type"),
        auth.get("chatgpt_plan_type"),
        id_auth.get("chatgpt_plan_type"),
    )
    workspace_id = _workspace_id_from_session(session, account, provider_data, credentials, auth, id_auth)
    # ChatGPT NextAuth sessions often do not expose a separate OAuth id_token.
    # Do not fabricate one: downstream importers may treat id_token as a real
    # server-issued credential and fail with token_invalidated.  Keep the field
    # populated for CPA compatibility by aliasing it to the real access_token.
    id_token = input_id_token or access_token
    exported_at = _normalize_timestamp(now or datetime.now(tz=timezone.utc))
    name = _first_text(email, "ChatGPT Account")

    data = {
        "type": "codex",
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "email": email,
        "name": name,
        "plan_type": plan_type,
        "chatgpt_plan_type": plan_type,
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "session_token": session_token,
        "last_refresh": exported_at,
        "expired": expires_at,
        "workspace_id": workspace_id or None,
        "organization_id": workspace_id or None,
        "disabled": True if session.get("disabled") else None,
    }
    return {key: value for key, value in data.items() if value is not None}


def _sanitize_file_token(value: str, fallback: str = "chatgpt-session") -> str:
    base = _first_text(value, fallback)
    base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return (base or fallback)[:80]


def _timestamp_token(now: datetime | None = None) -> str:
    dt = now or datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d_%H-%M-%S")


def save_cpa_json_locally(
    cpa_json: dict[str, Any],
    *,
    email: str = "",
    output_dir: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    target_dir = Path(output_dir) if output_dir is not None else Path("data") / "cpa_exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_sanitize_file_token(email or str(cpa_json.get('email') or 'chatgpt-session'))}_{_timestamp_token(now)}.json"
    path = target_dir / filename
    path.write_text(json.dumps(cpa_json, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.resolve()


def _parse_session_json_text(text: str, *, source: str = "session API") -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError(f"{source} returned empty body")
    try:
        data = json.loads(raw)
    except Exception as exc:
        snippet = raw[:240].replace("\n", " ")
        raise ValueError(f"{source} did not return JSON: {exc}; body={snippet!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{source} did not return a JSON object")
    return data


def _read_json_body_from_page(page) -> dict[str, Any]:
    text = page.evaluate(
        """
        () => {
          const pre = document.querySelector("pre");
          return String((pre && pre.innerText) || document.body.innerText || "");
        }
        """
    )
    return _parse_session_json_text(str(text or ""), source="session page")


def _refresh_chatgpt_after_workspace_switch(page, log=None) -> None:
    _safe_log(log, "Workspace Join: hard refresh ChatGPT after workspace switch")
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=60000)
    _page_wait(page, 800)
    keyboard = getattr(page, "keyboard", None)
    if keyboard is not None and hasattr(keyboard, "press"):
        try:
            keyboard.press("Control+F5")
            wait_for_load_state = getattr(page, "wait_for_load_state", None)
            if callable(wait_for_load_state):
                wait_for_load_state("domcontentloaded", timeout=60000)
            _page_wait(page, 1500)
            _safe_log(log, "Workspace Join: Ctrl+F5 hard refresh completed")
            return
        except Exception as exc:
            _safe_log(log, f"Workspace Join: Ctrl+F5 hard refresh failed, fallback reload: {exc}")
    reload_page = getattr(page, "reload", None)
    if callable(reload_page):
        try:
            reload_page(wait_until="domcontentloaded", timeout=60000)
            _page_wait(page, 1000)
        except Exception as exc:
            _safe_log(log, f"Workspace Join: ChatGPT reload after switch skipped: {exc}")


def _fetch_chatgpt_session_json_same_origin(page, *, log=None) -> dict[str, Any]:
    session_url = f"{CHATGPT_APP}/api/auth/session"
    try:
        payload = page.evaluate(
            """
            async ({ sessionUrl }) => {
              const response = await fetch(sessionUrl, {
                method: "GET",
                credentials: "include",
                headers: { "accept": "application/json" },
              });
              return {
                status: response.status,
                url: response.url,
                contentType: response.headers.get("content-type") || "",
                text: await response.text(),
              };
            }
            """,
            {"sessionUrl": session_url},
        )
    except Exception as exc:
        raise RuntimeError(f"browser fetch session API failed: {exc}") from exc

    if isinstance(payload, str):
        return _parse_session_json_text(payload, source="session API browser fetch")
    if not isinstance(payload, dict):
        raise ValueError(f"session API browser fetch returned non-object payload: {payload!r}")

    status = int(payload.get("status") or 0)
    response_url = str(payload.get("url") or "")
    content_type = str(payload.get("contentType") or "")
    text = str(payload.get("text") or "")
    _safe_log(
        log,
        "Workspace Join: session API browser fetch "
        f"status={status} content_type={content_type or '-'} url={response_url[:140]}",
    )
    if status != 200:
        raise ValueError(f"session API browser fetch HTTP {status}: {text[:240]!r}")
    return _parse_session_json_text(text, source="session API browser fetch")


def _load_chatgpt_session_json(page, *, attempt: int, total: int, log=None) -> dict[str, Any]:
    session_url = f"{CHATGPT_APP}/api/auth/session"
    _safe_log(log, f"Workspace Join: opening refreshed session API ({attempt}/{total})")
    try:
        return _fetch_chatgpt_session_json_same_origin(page, log=log)
    except Exception as exc:
        _safe_log(log, f"Workspace Join: browser fetch session API unavailable, fallback page open: {exc}")

    response = page.goto(session_url, wait_until="domcontentloaded", timeout=60000)
    reload_page = getattr(page, "reload", None)
    if callable(reload_page):
        try:
            reload_page(wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            _safe_log(log, f"Workspace Join: session API reload skipped: {exc}")
    try:
        if response is not None and hasattr(response, "text"):
            text = response.text()
            _safe_log(
                log,
                "Workspace Join: session API page open "
                f"status={getattr(response, 'status', '-')}",
            )
            return _parse_session_json_text(str(text or ""), source="session API response")
    except Exception as exc:
        _safe_log(log, f"Workspace Join: direct response body unavailable, reading page body: {exc}")
    return _read_json_body_from_page(page)


def _hard_refresh_chatgpt_home(page, log=None) -> None:
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=60000)
    _page_wait(page, 600)
    keyboard = getattr(page, "keyboard", None)
    if keyboard is not None and hasattr(keyboard, "press"):
        try:
            keyboard.press("Control+F5")
            wait_for_load_state = getattr(page, "wait_for_load_state", None)
            if callable(wait_for_load_state):
                wait_for_load_state("domcontentloaded", timeout=60000)
            _page_wait(page, 1200)
            _safe_log(log, "Workspace Join: Ctrl+F5 hard refresh while waiting for account switch")
            return
        except Exception as exc:
            _safe_log(log, f"Workspace Join: Ctrl+F5 while waiting failed, fallback reload: {exc}")
    reload_page = getattr(page, "reload", None)
    if callable(reload_page):
        reload_page(wait_until="domcontentloaded", timeout=60000)
        _page_wait(page, 1200)
    else:
        _page_wait(page, 1200)


def _debug_chatgpt_page_state(page) -> str:
    try:
        result = page.evaluate(
            """
            () => {
              const text = String(document.body?.innerText || document.documentElement?.innerText || "")
                .replace(/\\s+/g, " ")
                .trim()
                .slice(0, 240);
              const buttons = Array.from(document.querySelectorAll("button, [role='button'], [tabindex]:not([tabindex='-1'])"))
                .filter((el) => {
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.display !== "none" && style.visibility !== "hidden" &&
                    rect.width > 0 && rect.height > 0;
                })
                .map((el) => String(el.innerText || el.textContent || el.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim())
                .filter(Boolean)
                .slice(0, 8);
              return {
                url: location.href,
                title: document.title || "",
                readyState: document.readyState || "",
                bodyText: text,
                buttons,
              };
            }
            """
        )
        if isinstance(result, dict):
            return (
                f"url={str(result.get('url') or '')[:160]} "
                f"title={str(result.get('title') or '')[:80]!r} "
                f"ready={result.get('readyState') or '-'} "
                f"buttons={result.get('buttons') or []} "
                f"body={str(result.get('bodyText') or '')[:240]!r}"
            )
    except Exception as exc:
        return f"page state unavailable: {exc}"
    return "page state unavailable"


def _try_workspace_cpa_json_from_session(
    page,
    *,
    workspace_id: str,
    now: datetime | None,
    log=None,
) -> dict[str, Any] | None:
    try:
        session_json = _load_chatgpt_session_json(page, attempt=1, total=1, log=log)
        candidate = convert_chatgpt_session_to_cpa_json(session_json, now=now)
        assert_workspace_cpa_json(candidate, workspace_id=workspace_id)
        _safe_log(
            log,
            "Workspace Join: current session API is already workspace/EDU context; "
            "skipping profile menu switch",
        )
        return candidate
    except ValueError as exc:
        _safe_log(log, f"Workspace Join: current session API is not workspace/EDU yet: {exc}")
        return None
    except Exception as exc:
        _safe_log(log, f"Workspace Join: current session API probe failed: {exc}")
        return None


def _profile_workspace_text(page, timeout_ms: int) -> str:
    page.wait_for_function(
        """
        () => {
          const el = document.querySelector('[data-testid="accounts-profile-button"]');
          const text = String((el && (el.innerText || el.textContent || el.getAttribute("aria-label"))) || "");
          const workspaceLike = /Workspace|School|High\\s*School|Elementary|Middle\\s*School|College|University|Academy|District|Education|\\bEDU\\b|K12|\\.edu\\b|\\bGPT\\s*PRO\\b/i;
          const personalLike = /Personal|Free|Plus|Pro|\\bAccount\\b/i.test(text) && !/\\bGPT\\s*PRO\\b/i.test(text);
          return workspaceLike.test(text) && !personalLike;
          return /Workspace/i.test(text) && !/Personal|个人帐户|个人账户/.test(text);
        }
        """,
        timeout=timeout_ms,
    )
    text = page.evaluate(
        """
        () => {
          const el = document.querySelector('[data-testid="accounts-profile-button"]');
          return String((el && (el.innerText || el.textContent || el.getAttribute("aria-label"))) || "").trim();
        }
        """
    )
    return str(text or "").strip()


def _looks_like_navigation_interrupt(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "execution context was destroyed",
            "most likely because of a navigation",
            "navigation",
            "frame was detached",
            "context was destroyed",
        )
    )


def _looks_like_workspace_menu_not_ready(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "workspace row not visible",
            "workspace radio not visible",
            "workspace fallback skipped",
            "no workspace-like text",
        )
    )


_CHATGPT_WORKSPACE_UI_HELPERS = """
  const textOf = (el) => String(
    el && (el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("title")) || ""
  ).replace(/\\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const uniqElements = (items) => {
    const seen = new Set();
    const out = [];
    for (const item of items) {
      if (!item || seen.has(item)) continue;
      seen.add(item);
      out.push(item);
    }
    return out;
  };
  const clickableAncestor = (el) => {
    let node = el;
    for (let i = 0; node && i < 5; i += 1, node = node.parentElement) {
      if (node.matches && node.matches(
        'button, a, [role="button"], [role="menuitem"], [role="menuitemradio"], [tabindex]:not([tabindex="-1"]), [data-radix-collection-item]'
      )) {
        return node;
      }
    }
    return el;
  };
  const workspacePattern = /Workspace|\\u5de5\\u4f5c\\u7a7a\\u95f4|\\u5de5\\u4f5c\\u533a/i;
  const schoolPattern = /School|High\\s*School|Elementary|Middle\\s*School|College|University|Academy|District|Education|Teacher|Classroom|\\bEDU\\b|K12|\\.edu\\b/i;
  const personalPattern = /Personal|Free|Plus|Pro|\\bAccount\\b|\\u514d\\u8d39|\\u4e2a\\u4eba\\u5e10\\u6237|\\u4e2a\\u4eba\\u8d26\\u6237|\\u5e10\\u6237|\\u8d26\\u6237/i;
  const menuNoisePattern = /Help|Settings|Log\\s*out|Sign\\s*out|Customize|Personalization|\\u5e2e\\u52a9|\\u8bbe\\u7f6e|\\u9000\\u51fa|\\u4e2a\\u6027\\u5316|\\u4e2a\\u4eba\\u8d44\\u6599/i;
  const addAccountPattern = /Add\\s+another\\s+account|\\u6dfb\\u52a0\\u53e6\\u4e00\\u4e2a\\u8d26\\u6237|\\u6dfb\\u52a0\\u53e6\\u4e00\\u4e2a\\u5e10\\u6237/i;
  const loginNoisePattern = /Log\\s*in|Sign\\s*up|New\\s*chat|Search|Library|Projects?|Apps?|More|Terms|Privacy|Voice|ChatGPT|\\u65b0\\u804a\\u5929|\\u641c\\u7d22|\\u6587\\u4ef6\\u5e93|\\u9879\\u76ee|\\u5e94\\u7528|\\u66f4\\u591a/i;
  const profileTextPattern = /Profile|Account|Personal|Free|Plus|Pro|\\u514d\\u8d39|\\u4e2a\\u4eba\\u5e10\\u6237|\\u4e2a\\u4eba\\u8d26\\u6237|\\u5e10\\u6237|\\u8d26\\u6237/i;
  const proWorkspacePattern = /\\bGPT\\s*PRO\\b/i;
  const isChecked = (el) => {
    if (!el) return false;
    if (el.getAttribute("aria-checked") === "true") return true;
    if (el.getAttribute("data-state") === "checked") return true;
    return !!el.querySelector('[aria-checked="true"], [data-state="checked"]');
  };
  const isWorkspaceText = (raw) => {
    const text = String(raw || "");
    const hasWorkspaceSignal = workspacePattern.test(text) || schoolPattern.test(text) || proWorkspacePattern.test(text);
    const personalOnly = personalPattern.test(text) && !hasWorkspaceSignal;
    return !!text && hasWorkspaceSignal && !personalOnly && !addAccountPattern.test(text);
  };
  const findProfileButton = () => {
    const selectors = [
      '[data-testid="accounts-profile-button"]',
      '[data-testid*="accounts-profile" i]',
      '[data-testid*="profile-button" i]',
      '[data-testid*="account" i]',
      'button[aria-label*="profile" i]',
      'button[aria-label*="account" i]',
      '[role="button"][aria-label*="profile" i]',
      '[role="button"][aria-label*="account" i]',
      '[role="button"]',
      '[tabindex]:not([tabindex="-1"])',
      'li',
      'div'
    ];
    const candidates = uniqElements(Array.from(document.querySelectorAll(selectors.join(","))).map(clickableAncestor))
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const raw = textOf(el);
        const attrs = String(
          (el.getAttribute("data-testid") || "") + " " +
          (el.getAttribute("aria-label") || "") + " " +
          (el.getAttribute("title") || "")
        );
        const inLeftSidebar = rect.left < Math.min(390, window.innerWidth * 0.35);
        const inBottomAccountBand = rect.top > Math.max(window.innerHeight - 180, window.innerHeight * 0.68);
        const saneRow = rect.width >= 90 && rect.width <= 390 && rect.height >= 28 && rect.height <= 90;
        const navOnly = /^(?:\\u2026|\\.\\.\\.)?\\s*(New\\s+chat|Search\\s+chats|Library|Projects?|Apps?|Codex|More)$/i.test(raw);
        const looksAccountText = /Personal\\s+account|Personal|Account|Free|Plus|Pro|\\u4e2a\\u4eba\\u5e10\\u6237|\\u4e2a\\u4eba\\u8d26\\u6237/i.test(raw);
        const looksAccountAttrs = /accounts-profile|profile-button|account/i.test(attrs);
        let score = 0;
        if (inLeftSidebar) score += 100;
        if (inBottomAccountBand) score += 160;
        if (saneRow) score += 80;
        if (looksAccountText) score += 180;
        if (looksAccountAttrs) score += 180;
        if (raw.length > 140 || rect.width > 430 || rect.height > 120) score -= 300;
        if (navOnly || menuNoisePattern.test(raw) || addAccountPattern.test(raw)) score -= 500;
        if (!inLeftSidebar || !inBottomAccountBand || !saneRow) score -= 500;
        if (!looksAccountText && !looksAccountAttrs) score -= 500;
        return { el, score, raw, rect };
      })
      .filter((entry) => entry.score >= 400)
      .sort((a, b) => b.score - a.score || b.rect.top - a.rect.top || a.rect.left - b.rect.left);
    return candidates.length ? candidates[0].el : null;
  };
  const menuItemCandidates = () => {
    const selector = [
      '[role="menuitemradio"]',
      '[role="menuitem"]',
      '[data-radix-collection-item]',
      'button',
      '[role="button"]',
      '[tabindex]:not([tabindex="-1"])'
    ].join(",");
    return uniqElements(Array.from(document.querySelectorAll(selector)).map(clickableAncestor))
      .filter(visible)
      .filter((el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width < 20 || rect.height < 12) return false;
        if (rect.left > Math.min(window.innerWidth * 0.7, 860)) return false;
        return true;
      });
  };
  const workspaceMenuCandidates = () => {
    const generic = Array.from(document.querySelectorAll([
      '[role="menuitemradio"]',
      '[role="menuitem"]',
      '[role="option"]',
      '[data-radix-collection-item]',
      'button',
      '[role="button"]',
      '[tabindex]:not([tabindex="-1"])',
      'li',
      'div'
    ].join(",")))
      .filter(visible)
      .filter((el) => {
        const rect = el.getBoundingClientRect();
        const raw = textOf(el);
        if (!raw || raw.length > 140) return false;
        if (rect.width < 30 || rect.height < 14 || rect.height > 52) return false;
        if (rect.left > Math.min(window.innerWidth * 0.75, 980)) return false;
        if (rect.top < 80 && !schoolPattern.test(raw) && !workspacePattern.test(raw)) return false;
        return true;
      });
    return uniqElements([...menuItemCandidates(), ...generic]);
  };
  const workspaceChoiceScore = (item) => {
    const raw = textOf(item);
    const hasWorkspaceSignal = workspacePattern.test(raw) || schoolPattern.test(raw) || proWorkspacePattern.test(raw);
    if (!raw || addAccountPattern.test(raw) || menuNoisePattern.test(raw)) return -100;
    if (personalPattern.test(raw) && !hasWorkspaceSignal) return -100;
    if (/@(?![^\\s]*\\.edu\\b)/i.test(raw)) return -80;
    let score = 0;
    if (proWorkspacePattern.test(raw)) score += 140;
    if (workspacePattern.test(raw)) score += 100;
    if (schoolPattern.test(raw)) score += 120;
    if (/\\bEDU\\b|K12|School|\\.edu\\b|\\bGPT\\s*PRO\\b/i.test(raw)) score += 40;
    if (personalPattern.test(raw) && !proWorkspacePattern.test(raw)) score -= 30;
    return score;
  };
  const targetFromTextNodes = (pattern, scoreFn) => {
    const root = document.body || document.documentElement;
    if (!root || !window.NodeFilter) return null;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const candidates = [];
    let node;
    while ((node = walker.nextNode())) {
      const rawText = String(node.nodeValue || "").replace(/\\s+/g, " ").trim();
      if (!rawText || !pattern.test(rawText)) continue;
      let row = node.parentElement;
      let best = null;
      for (let i = 0; row && row !== root && i < 7; i += 1, row = row.parentElement) {
        if (!visible(row)) continue;
        const rect = row.getBoundingClientRect();
        const raw = textOf(row);
        if (!raw || raw.length > 180) continue;
        if (rect.width < 30 || rect.height < 14 || rect.height > 52) continue;
        if (rect.left > Math.min(window.innerWidth * 0.75, 980)) continue;
        if (rect.top < 80 && !schoolPattern.test(raw) && !workspacePattern.test(raw)) continue;
        best = row;
        if (
          row.matches?.('[role="menuitem"], [role="menuitemradio"], [role="option"], [data-radix-collection-item], button, [role="button"]')
        ) {
          break;
        }
      }
      if (!best) continue;
      const score = Number(scoreFn ? scoreFn(best) : 1);
      if (score <= 0) continue;
      const rect = best.getBoundingClientRect();
      candidates.push({ item: best, score, area: rect.width * rect.height, top: rect.top, left: rect.left });
    }
    candidates.sort((a, b) => b.score - a.score || a.area - b.area || a.top - b.top || a.left - b.left);
    return candidates.length ? candidates[0].item : null;
  };
"""


_PROFILE_MENU_TARGET_SCRIPT = """
() => {
  // PROFILE_MENU_TARGET
""" + _CHATGPT_WORKSPACE_UI_HELPERS + """
  const target = findProfileButton();
  if (!visible(target)) {
    const visibleText = Array.from(document.querySelectorAll("button, [role='button'], [tabindex]:not([tabindex='-1'])"))
      .filter(visible)
      .map(textOf)
      .filter(Boolean)
      .slice(0, 10);
    return { ok: false, summary: `profile button not visible; visible=${JSON.stringify(visibleText)}` };
  }
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    text: textOf(target),
  };
}
"""


_CHATGPT_HOME_READY_SCRIPT = """
() => {
  // CHATGPT_HOME_READY
  const raw = String(document.body?.innerText || document.documentElement?.innerText || "")
    .replace(/\\s+/g, " ")
    .trim();
  if (!/chatgpt\\.com/i.test(location.href)) return false;
  return /New chat|Search chats|Library|Projects|Apps|Ready when you are|Ask anything|Message ChatGPT|Codex|\\u65b0\\u804a\\u5929|\\u641c\\u7d22|\\u6587\\u4ef6\\u5e93|\\u9879\\u76ee|\\u5e94\\u7528/i.test(raw);
}
"""


_ACCOUNT_SUBMENU_TARGET_SCRIPT = """
() => {
  // ACCOUNT_SUBMENU_TARGET
""" + _CHATGPT_WORKSPACE_UI_HELPERS + """
  const accountPattern = /Personal|Account|Free|Plus|Pro|Team|Enterprise|\\u514d\\u8d39|\\u4e2a\\u4eba\\u5e10\\u6237|\\u4e2a\\u4eba\\u8d26\\u6237|\\u5e10\\u6237|\\u8d26\\u6237/i;
  const bottomProfileBlock = (item) => {
    const rect = item.getBoundingClientRect();
    return rect.left < Math.min(380, window.innerWidth * 0.35) &&
      rect.top > window.innerHeight - 125 &&
      rect.height >= 34 &&
      profileTextPattern.test(textOf(item));
  };
  const hasSubmenuSignal = (item) => {
    if (item.getAttribute("data-has-submenu") != null) return true;
    const popup = String(item.getAttribute("aria-haspopup") || "").toLowerCase();
    if (popup === "menu" || popup === "true") return true;
    const rect = item.getBoundingClientRect();
    return !!Array.from(item.querySelectorAll('[data-testid*="submenu" i], [data-testid*="chevron" i], svg'))
      .find((child) => {
        const childRect = child.getBoundingClientRect();
        return childRect.left > rect.left + rect.width * 0.62;
      });
  };
  const inOpenProfileMenu = (item) => {
    const rect = item.getBoundingClientRect();
    return rect.left < Math.min(380, window.innerWidth * 0.35) &&
      rect.top > window.innerHeight * 0.30 &&
      rect.top < window.innerHeight - 125 &&
      rect.width >= 120 &&
      rect.height >= 34 &&
      rect.height <= 92;
  };
  const ranked = menuItemCandidates()
    .map((item) => {
      const raw = textOf(item);
      const rect = item.getBoundingClientRect();
      let score = 0;
      if (!raw || menuNoisePattern.test(raw) || addAccountPattern.test(raw)) score -= 120;
      if (accountPattern.test(raw)) score += 120;
      if (hasSubmenuSignal(item)) score += 80;
      if (inOpenProfileMenu(item)) score += 120;
      if (bottomProfileBlock(item)) score -= 260;
      if (rect.left < 420 && rect.top > window.innerHeight * 0.35) score += 25;
      if (raw && raw.length <= 100) score += 10;
      if (/@/.test(raw)) score -= 40;
      return { item, score, raw, rect };
    })
    .filter((entry) => entry.score >= 90)
    .sort((a, b) => b.score - a.score || a.rect.top - b.rect.top || a.rect.left - b.rect.left);
  const target = ranked.length ? ranked[0].item : null;
  const items = menuItemCandidates();
  const candidates = items.map(textOf).filter(Boolean).slice(0, 8);
  if (!target) {
    return { ok: false, summary: `account submenu not visible; candidates=${JSON.stringify(candidates)}` };
  }
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  const clickX = Math.min(rect.right - 16, rect.left + rect.width * 0.92);
  return {
    ok: true,
    x: clickX,
    y: rect.top + rect.height / 2,
    text: textOf(target),
  };
}
"""


_WORKSPACE_RADIO_TARGET_SCRIPT = """
(anchor) => {
  // WORKSPACE_RADIO_TARGET
""" + _CHATGPT_WORKSPACE_UI_HELPERS + """
  const accountRight = Number(anchor?.x || 0) + Number(anchor?.width || 0);
  const accountTop = Number(anchor?.y || 0);

  // 限制搜索范围在右侧二级菜单：left > 账号行 right，top 与账号行附近重叠
  const isRightSubmenuItem = (rect) => {
    if (!Number.isFinite(accountRight) || accountRight <= 0) return true;
    return rect.left >= accountRight - 8 && rect.top >= accountTop - 160 && rect.bottom <= accountTop + 360;
  };

  const allItems = workspaceMenuCandidates();
  const items = allItems.filter((item) => isRightSubmenuItem(item.getBoundingClientRect()));
  const checkedWorkspace = items.find((item) => isChecked(item) && workspaceChoiceScore(item) > 0);
  const ranked = items
    .filter((item) => !isChecked(item))
    .map((item) => {
      const rect = item.getBoundingClientRect();
      const area = rect.width * rect.height;
      return { item, score: workspaceChoiceScore(item), raw: textOf(item), area };
    })
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score || a.area - b.area);
  const textNodeTarget = targetFromTextNodes(schoolPattern, workspaceChoiceScore);
  const target = (ranked.length ? ranked[0].item : null) || checkedWorkspace || textNodeTarget;
  const candidates = items.map(textOf).filter(Boolean).slice(0, 12);
  if (!target) {
    const allCandidates = allItems.map(textOf).filter(Boolean).slice(0, 16);
    return { ok: false, summary: `workspace radio not visible; submenuCandidates=${JSON.stringify(candidates)} allCandidates=${JSON.stringify(allCandidates)} accountRight=${accountRight}` };
  }
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
  const rect = target.getBoundingClientRect();
  const clickX = rect.left + rect.width * 0.45;
  const clickY = rect.top + rect.height / 2;
  // 点击前记录 elementFromPoint 验证
  let elementFromPointText = "";
  try {
    const hit = document.elementFromPoint(clickX, clickY);
    elementFromPointText = textOf(hit);
  } catch (_) {}
  return {
    ok: true,
    x: clickX,
    y: clickY,
    text: textOf(target),
    alreadySelected: target === checkedWorkspace,
    source: "",
    rect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
    elementFromPointText,
  };
}
"""


_WORKSPACE_RADIO_FALLBACK_TARGET_SCRIPT = """
(anchor) => {
  // WORKSPACE_RADIO_FALLBACK_TARGET
""" + _CHATGPT_WORKSPACE_UI_HELPERS + """
  const textTarget = targetFromTextNodes(schoolPattern, workspaceChoiceScore);
  if (visible(textTarget)) {
    const rect = textTarget.getBoundingClientRect();
    return {
      ok: true,
      x: rect.left + rect.width * 0.45,
      y: rect.top + rect.height / 2,
      text: textOf(textTarget),
      source: "text-node",
    };
  }

  const bodyText = textOf(document.body || document.documentElement);
  if (!schoolPattern.test(bodyText) && !workspacePattern.test(bodyText) && !proWorkspacePattern.test(bodyText)) {
    return { ok: false, summary: "workspace fallback skipped; no workspace-like text in open menu" };
  }
  const anchorX = Number(anchor?.x || 0);
  const anchorY = Number(anchor?.y || 0);
  if (!Number.isFinite(anchorX) || !Number.isFinite(anchorY) || anchorX <= 0 || anchorY <= 0) {
    return { ok: false, summary: "workspace fallback skipped; missing submenu anchor" };
  }

  // ChatGPT renders the workspace submenu to the right of the personal-account row.
  // Some Camoufox builds do not expose that submenu in the accessibility tree, so
  // use the observed menu geometry only after workspace-like text is visible.
  const scanWorkspacePoint = () => {
    const xOffsets = [250, 285, 220, 320, 185, 355];
    const yOffsets = [-150, -132, -116, -100, -84, -68, -52, -36, -20, 0, 18];
    for (const xOffset of xOffsets) {
      const x = Math.min(window.innerWidth - 24, Math.max(24, anchorX + xOffset));
      for (const yOffset of yOffsets) {
        const y = Math.min(window.innerHeight - 24, Math.max(90, anchorY + yOffset));
        let node = document.elementFromPoint(x, y);
        for (let i = 0; node && i < 7; i += 1, node = node.parentElement) {
          if (!visible(node)) continue;
          const rect = node.getBoundingClientRect();
          const raw = textOf(node);
          if (!raw || raw.length > 180) continue;
          if (rect.width < 30 || rect.height < 12 || rect.height > 96) continue;
          if (rect.left < anchorX + 24) continue;
          if (!isWorkspaceText(raw)) continue;
          return {
            x: Math.min(rect.right - 14, Math.max(rect.left + 14, x)),
            y: Math.min(rect.bottom - 8, Math.max(rect.top + 8, rect.top + Math.min(20, rect.height * 0.38))),
            text: raw,
            source: "point-scan",
          };
        }
      }
    }
    return null;
  };
  const scanned = scanWorkspacePoint();
  if (scanned) {
    return { ok: true, ...scanned };
  }
  const x = Math.min(window.innerWidth - 24, Math.max(24, anchorX + 260));
  const y = Math.min(window.innerHeight - 24, Math.max(90, anchorY - 112));
  return {
    ok: true,
    x,
    y,
    text: "workspace geometry fallback upper",
    source: "geometry-upper",
  };
}
"""


_WORKSPACE_READY_TARGET_SCRIPT = """
() => {
  // WORKSPACE_READY_TARGET
""" + _CHATGPT_WORKSPACE_UI_HELPERS + """
  const profile = findProfileButton();
  const profileText = textOf(profile);
  if (visible(profile) && isWorkspaceText(profileText)) {
    return { ok: true, source: "profile", text: profileText };
  }
  const radioItems = workspaceMenuCandidates();
  const checkedWorkspace = radioItems.find((item) => isChecked(item) && workspaceChoiceScore(item) > 0);
  if (checkedWorkspace) {
    return { ok: true, source: "selected-radio", text: textOf(checkedWorkspace) };
  }
  const welcomePattern = /Welcome\\s+to\\s+.+\\s+workspace/i;
  const nodes = Array.from(document.querySelectorAll("div, main, section, h1, h2, p, [role='status']"));
  const target = nodes.find((el) => visible(el) && welcomePattern.test(textOf(el)));
  if (target) {
    return { ok: true, source: "welcome", text: textOf(target) };
  }
  const candidates = nodes
    .filter((el) => visible(el))
    .map(textOf)
    .filter((text) => text && (/Workspace|Welcome/i.test(text) || schoolPattern.test(text)))
    .slice(0, 8);
  return { ok: false, summary: `workspace ready signal not visible; candidates=${JSON.stringify(candidates)}` };
}
"""


_WORKSPACE_STEP2_DEBUG_SCRIPT = """
(anchor) => {
  // WORKSPACE_STEP2_DEBUG
""" + _CHATGPT_WORKSPACE_UI_HELPERS + """
  const anchorX = Number(anchor?.x || 0);
  const anchorY = Number(anchor?.y || 0);
  const pointX = Math.min(window.innerWidth - 24, Math.max(24, anchorX + 260));
  const pointY = Math.min(window.innerHeight - 24, Math.max(90, anchorY - 60));
  const hit = document.elementFromPoint(pointX, pointY);
  const describe = (el) => {
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return {
      tag: String(el.tagName || "").toLowerCase(),
      role: String(el.getAttribute?.("role") || ""),
      text: textOf(el),
      rect: {
        left: Math.round(rect.left),
        top: Math.round(rect.top),
        right: Math.round(rect.right),
        bottom: Math.round(rect.bottom),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      },
      checked: isChecked(el),
      workspaceScore: workspaceChoiceScore(el),
    };
  };
  const rightCandidates = workspaceMenuCandidates()
    .map((el) => describe(el))
    .filter(Boolean)
    .filter((item) => item.rect.left > anchorX + 20)
    .slice(0, 20);
  const visibleMenuTexts = menuItemCandidates()
    .map(textOf)
    .filter(Boolean)
    .slice(0, 30);
  return {
    url: location.href,
    readyState: document.readyState,
    bodyText: String(document.body?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 500),
    anchor: { x: anchorX, y: anchorY, text: String(anchor?.text || "") },
    probePoint: { x: Math.round(pointX), y: Math.round(pointY) },
    elementFromPoint: describe(hit),
    visibleMenuTexts,
    rightCandidates,
  };
}
"""


_WORKSPACE_CLICK_DEBUG_SCRIPT = """
(target) => {
  // WORKSPACE_CLICK_DEBUG
""" + _CHATGPT_WORKSPACE_UI_HELPERS + """
  const clickX = Number(target?.x || 0);
  const clickY = Number(target?.y || 0);
  const hit = document.elementFromPoint(clickX, clickY);
  const describe = (el) => {
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return {
      tag: String(el.tagName || "").toLowerCase(),
      role: String(el.getAttribute?.("role") || ""),
      text: textOf(el),
      rect: {
        left: Math.round(rect.left),
        top: Math.round(rect.top),
        right: Math.round(rect.right),
        bottom: Math.round(rect.bottom),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      },
      checked: isChecked(el),
      workspaceScore: workspaceChoiceScore(el),
    };
  };
  const candidates = workspaceMenuCandidates()
    .map((el) => describe(el))
    .filter(Boolean)
    .slice(0, 24);
  return {
    url: location.href,
    readyState: document.readyState,
    bodyText: String(document.body?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 500),
    target: {
      x: clickX,
      y: clickY,
      text: String(target?.text || ""),
      source: String(target?.source || ""),
      alreadySelected: Boolean(target?.alreadySelected),
    },
    elementFromPoint: describe(hit),
    visibleMenuTexts: menuItemCandidates().map(textOf).filter(Boolean).slice(0, 30),
    workspaceCandidates: candidates,
  };
}
"""


def _safe_log(log, message: str) -> None:
    if callable(log):
        try:
            log(message)
        except Exception:
            pass


def _page_wait(page, ms: int) -> None:
    try:
        page.wait_for_timeout(int(ms))
    except Exception:
        time.sleep(max(int(ms), 0) / 1000)


def _env_flag_enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _debug_workspace_step2_snapshot(page, account: dict[str, Any], log=None) -> None:
    if not _env_flag_enabled("OPAI_DEBUG_WORKSPACE_STEP2"):
        return
    try:
        output_dir = Path(os.getenv("OPAI_DEBUG_WORKSPACE_DIR", "debug/workspace_step2"))
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        screenshot_path = output_dir / f"workspace_step2_{stamp}.png"
        json_path = output_dir / f"workspace_step2_{stamp}.json"

        screenshot_error = ""
        try:
            page.screenshot(path=str(screenshot_path), full_page=False)
        except Exception as exc:
            screenshot_error = str(exc)[:300]

        try:
            payload = page.evaluate(_WORKSPACE_STEP2_DEBUG_SCRIPT, account)
        except Exception as exc:
            payload = {"error": str(exc)[:500]}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        payload["screenshot"] = str(screenshot_path)
        if screenshot_error:
            payload["screenshot_error"] = screenshot_error
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _safe_log(
            log,
            "Workspace Join: step2 debug snapshot saved: "
            f"{json_path} screenshot={screenshot_path}",
        )
    except Exception as exc:
        _safe_log(log, f"Workspace Join: step2 debug snapshot failed: {exc}")


def _debug_workspace_click_snapshot(page, target: dict[str, Any], *, stage: str, log=None) -> None:
    if not _env_flag_enabled("OPAI_DEBUG_WORKSPACE_STEP2"):
        return
    safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stage or "click")).strip("_") or "click"
    try:
        output_dir = Path(os.getenv("OPAI_DEBUG_WORKSPACE_DIR", "debug/workspace_step2"))
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        screenshot_path = output_dir / f"workspace_step3_{safe_stage}_{stamp}.png"
        json_path = output_dir / f"workspace_step3_{safe_stage}_{stamp}.json"

        screenshot_error = ""
        try:
            page.screenshot(path=str(screenshot_path), full_page=False)
        except Exception as exc:
            screenshot_error = str(exc)[:300]

        try:
            payload = page.evaluate(_WORKSPACE_CLICK_DEBUG_SCRIPT, target)
        except Exception as exc:
            payload = {"error": str(exc)[:500], "target": target}
        if not isinstance(payload, dict):
            payload = {"value": payload, "target": target}
        payload["screenshot"] = str(screenshot_path)
        if screenshot_error:
            payload["screenshot_error"] = screenshot_error
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _safe_log(
            log,
            f"Workspace Join: step3 {safe_stage} snapshot saved: "
            f"{json_path} screenshot={screenshot_path}",
        )
    except Exception as exc:
        _safe_log(log, f"Workspace Join: step3 {safe_stage} snapshot failed: {exc}")


def _debug_workspace_step3_pause(page, log=None) -> None:
    if not _env_flag_enabled("OPAI_DEBUG_WORKSPACE_STEP2"):
        return
    raw = str(os.getenv("OPAI_DEBUG_WORKSPACE_STEP3_PAUSE_MS", "3000")).strip()
    try:
        pause_ms = max(0, min(10000, int(raw)))
    except Exception:
        pause_ms = 3000
    if pause_ms <= 0:
        return
    _safe_log(log, f"Workspace Join: debug pause before step3 click {pause_ms}ms")
    _page_wait(page, pause_ms)


def _remaining_ms(deadline: float, minimum: int = 1000) -> int:
    return max(int(minimum), int((deadline - time.monotonic()) * 1000))


def _dismiss_chatgpt_persistent_storage_prompt(page, log=None) -> None:
    try:
        page.context.grant_permissions(["persistent-storage"], origin=CHATGPT_APP)
    except Exception:
        pass
    try:
        keyboard = getattr(page, "keyboard", None)
        if keyboard is not None and hasattr(keyboard, "press"):
            keyboard.press("Enter")
            _page_wait(page, 150)
            _safe_log(log, "Workspace Join: handled persistent-storage prompt if present")
    except Exception:
        pass


def _wait_for_dom_target(
    page,
    script: str,
    *,
    label: str,
    timeout_ms: int,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    last_summary = ""
    while time.monotonic() <= deadline:
        try:
            result = page.evaluate(script)
        except Exception as exc:
            if _looks_like_navigation_interrupt(exc):
                raise
            last_summary = str(exc)[:200]
            result = None

        if isinstance(result, dict) and result.get("ok"):
            return result
        if isinstance(result, dict):
            last_summary = str(result.get("summary") or result)[:240]
        _page_wait(page, 150)

    if last_summary:
        return {"ok": False, "summary": f"{label}: {last_summary}"}
    return None


def _wait_for_dom_target_with_arg(
    page,
    script: str,
    arg: dict[str, Any],
    *,
    label: str,
    timeout_ms: int,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    last_summary = ""
    while time.monotonic() <= deadline:
        try:
            result = page.evaluate(script, arg)
        except Exception as exc:
            if _looks_like_navigation_interrupt(exc):
                raise
            last_summary = str(exc)[:200]
            result = None

        if isinstance(result, dict) and result.get("ok"):
            return result
        if isinstance(result, dict):
            last_summary = str(result.get("summary") or result)[:240]
        _page_wait(page, 150)

    if last_summary:
        return {"ok": False, "summary": f"{label}: {last_summary}"}
    return None


def _click_dom_target(page, target: dict[str, Any], *, label: str, log=None) -> None:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "click"):
        raise RuntimeError("page.mouse.click is not available")
    x = float(target.get("x"))
    y = float(target.get("y"))
    mouse.click(x, y)
    _safe_log(log, f"Workspace Join: clicked {label}: {target.get('text') or '-'}")


def _wait_for_clickable_dom_target(
    page,
    script: str,
    *,
    label: str,
    timeout_ms: int,
    log=None,
) -> dict[str, Any]:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "click"):
        raise RuntimeError("page.mouse.click is not available")

    result = _wait_for_dom_target(page, script, label=label, timeout_ms=timeout_ms)
    if isinstance(result, dict) and result.get("ok"):
        _click_dom_target(page, result, label=label, log=log)
        return result
    last_summary = str((result or {}).get("summary") if isinstance(result, dict) else result or "")[:240]

    raise RuntimeError(f"timed out waiting for {label}: {last_summary}")


def _move_mouse_to_target(page, target: dict[str, Any]) -> None:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "move"):
        return
    try:
        mouse.move(float(target.get("x")), float(target.get("y")))
    except Exception:
        pass


def _click_until_next_dom_target(
    page,
    *,
    click_script: str,
    next_script: str,
    next_fallback_script: str | None = None,
    click_label: str,
    next_label: str,
    timeout_ms: int,
    log=None,
    move_after_click: bool = False,
    post_click_wait_ms: int = 250,
) -> dict[str, Any]:
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "click"):
        raise RuntimeError("page.mouse.click is not available")

    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    attempts = 0
    last_summary = ""

    while time.monotonic() <= deadline:
        next_probe_timeout = min(max(int((deadline - time.monotonic()) * 1000), 1), 150)
        next_target = _wait_for_dom_target(
            page,
            next_script,
            label=next_label,
            timeout_ms=next_probe_timeout,
        )
        if isinstance(next_target, dict) and next_target.get("ok"):
            next_target["clickAttempts"] = attempts
            return next_target
        if isinstance(next_target, dict):
            last_summary = str(next_target.get("summary") or next_target)[:240]

        click_timeout = min(max(int((deadline - time.monotonic()) * 1000), 1), 700)
        click_target = _wait_for_dom_target(
            page,
            click_script,
            label=click_label,
            timeout_ms=click_timeout,
        )
        if not isinstance(click_target, dict) or not click_target.get("ok"):
            last_summary = str((click_target or {}).get("summary") if isinstance(click_target, dict) else click_target or "")[:240]
            _safe_log(log, f"Workspace Join: {click_label} not visible; keep waiting for {next_label}")
            _page_wait(page, 250)
            continue

        attempts += 1
        _click_dom_target(page, click_target, label=click_label, log=log)
        if move_after_click:
            _move_mouse_to_target(page, click_target)
        _page_wait(page, post_click_wait_ms)

        next_timeout = min(max(int((deadline - time.monotonic()) * 1000), 1), 900)
        next_target = _wait_for_dom_target(
            page,
            next_script,
            label=next_label,
            timeout_ms=next_timeout,
        )
        if isinstance(next_target, dict) and next_target.get("ok"):
            next_target["clickAttempts"] = attempts
            return next_target
        if isinstance(next_target, dict):
            last_summary = str(next_target.get("summary") or next_target)[:240]

        if next_fallback_script:
            try:
                fallback_target = page.evaluate(next_fallback_script, click_target)
            except Exception as exc:
                if _looks_like_navigation_interrupt(exc):
                    raise
                fallback_target = {"ok": False, "summary": str(exc)[:200]}
            if isinstance(fallback_target, dict) and fallback_target.get("ok"):
                fallback_target["clickAttempts"] = attempts
                _safe_log(
                    log,
                    "Workspace Join: using fallback for "
                    f"{next_label}: {fallback_target.get('text') or fallback_target.get('source') or '-'}",
                )
                return fallback_target
            if isinstance(fallback_target, dict):
                last_summary = str(fallback_target.get("summary") or fallback_target)[:240]
        _safe_log(log, f"Workspace Join: {next_label} not visible after {click_label} click, retrying")

    raise RuntimeError(
        f"timed out waiting for {next_label} after clicking {click_label}: {last_summary}"
    )


def _wait_workspace_ready_after_click(page, timeout_ms: int) -> dict[str, Any]:
    ready = _wait_for_dom_target(
        page,
        _WORKSPACE_READY_TARGET_SCRIPT,
        label="workspace ready signal",
        timeout_ms=timeout_ms,
    )
    if isinstance(ready, dict) and ready.get("ok"):
        return ready
    summary = str((ready or {}).get("summary") if isinstance(ready, dict) else ready or "")[:240]
    raise RuntimeError(f"timed out waiting for workspace ready signal: {summary}")


def _ensure_chatgpt_account_button_visible(
    page,
    *,
    timeout_ms: int,
    log=None,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    refresh_attempts = 0
    last_summary = ""
    while time.monotonic() <= deadline:
        probe_timeout = min(max(int((deadline - time.monotonic()) * 1000), 1), 6000)
        result = _wait_for_dom_target(
            page,
            _PROFILE_MENU_TARGET_SCRIPT,
            label="profile button",
            timeout_ms=probe_timeout,
        )
        if isinstance(result, dict) and result.get("ok"):
            _safe_log(log, f"Workspace Join: account switch entry visible: {result.get('text') or '-'}")
            return result
        if isinstance(result, dict):
            last_summary = str(result.get("summary") or result)[:240]

        try:
            home_ready = page.evaluate(_CHATGPT_HOME_READY_SCRIPT)
        except Exception:
            home_ready = False
        if home_ready is True:
            _safe_log(
                log,
                "Workspace Join: ChatGPT home visible; stop refreshing and continue profile click attempts",
            )
            return None

        refresh_attempts += 1
        if time.monotonic() >= deadline:
            break
        _safe_log(
            log,
            "Workspace Join: free ChatGPT UI not ready for account switch; refreshing page "
            f"({refresh_attempts})",
        )
        try:
            _hard_refresh_chatgpt_home(page, log=log)
        except Exception as exc:
            _safe_log(log, f"Workspace Join: refresh while waiting for account switch failed: {exc}")
            _page_wait(page, 1200)

    if last_summary:
        _safe_log(log, f"Workspace Join: account switch entry still not visible: {last_summary}")
        _safe_log(log, f"Workspace Join: ChatGPT page state: {_debug_chatgpt_page_state(page)}")
    return None


def _finish_workspace_selection_from_open_menu(
    page,
    workspace: dict[str, Any],
    *,
    deadline: float,
    workspace_id: str = "",
    log=None,
) -> dict[str, Any]:
    if workspace.get("alreadySelected"):
        ready = _wait_for_dom_target(
            page,
            _WORKSPACE_READY_TARGET_SCRIPT,
            label="workspace ready signal",
            timeout_ms=min(_remaining_ms(deadline), 900),
        )
        if isinstance(ready, dict) and ready.get("ok"):
            return {
                "ok": True,
                "workspaceId": str(workspace_id or "").strip(),
                "selectedText": str(workspace.get("text") or ready.get("text") or ""),
                "profileText": str(ready.get("text") or workspace.get("text") or ""),
                "readySource": str(ready.get("source") or ""),
                "stepwise": True,
                "alreadySelected": True,
            }

    if not workspace.get("alreadySelected"):
        _debug_workspace_click_snapshot(page, workspace, stage="before_click", log=log)
        _debug_workspace_step3_pause(page, log=log)
        _click_dom_target(page, workspace, label="workspace radio item", log=log)
        _page_wait(page, 500)
        _debug_workspace_click_snapshot(page, workspace, stage="after_click", log=log)
        _page_wait(page, 900)
        _wait_for_dom_target(
            page,
            _WORKSPACE_RADIO_TARGET_SCRIPT,
            label="workspace radio item after selection",
            timeout_ms=300,
        )

    try:
        ready = _wait_workspace_ready_after_click(
            page,
            min(_remaining_ms(deadline, minimum=3000), 9000),
        )
        ready_text = str(ready.get("text") or workspace.get("text") or "")
        ready_source = str(ready.get("source") or "")
    except Exception as exc:
        if _looks_like_navigation_interrupt(exc):
            raise
        ready_text = str(workspace.get("text") or "")
        ready_source = "session-api-pending"
        _safe_log(
            log,
            "Workspace Join: workspace UI ready signal not visible after selection; "
            f"will verify via session API: {exc}",
        )

    return {
        "ok": True,
        "workspaceId": str(workspace_id or "").strip(),
        "selectedText": str(workspace.get("text") or ""),
        "profileText": ready_text,
        "readySource": ready_source,
        "stepwise": True,
    }


def _recover_workspace_ready_after_navigation(
    page,
    *,
    workspace_id: str = "",
    timeout_ms: int = 45000,
) -> dict[str, Any]:
    try:
        ready = _wait_workspace_ready_after_click(page, int(timeout_ms))
        return {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": "",
            "profileText": str(ready.get("text") or ""),
            "readySource": str(ready.get("source") or ""),
            "navigationRecovered": True,
        }
    except Exception:
        profile_text = _profile_workspace_text(page, int(timeout_ms))
        return {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": "",
            "profileText": profile_text,
            "readySource": "profile",
            "navigationRecovered": True,
        }


def _wait_profile_workspace_after_click(page, timeout_ms: int) -> str:
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            return _profile_workspace_text(page, int(timeout_ms))
        except Exception as exc:
            last_exc = exc
            if not _looks_like_navigation_interrupt(exc):
                raise
            _page_wait(page, 1000)
    if last_exc is not None:
        raise last_exc
    return ""


def _switch_workspace_via_profile_menu_stepwise(
    page,
    *,
    workspace_id: str = "",
    timeout_ms: int = 45000,
    log=None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    mouse = getattr(page, "mouse", None)
    if mouse is None or not hasattr(mouse, "click"):
        raise RuntimeError("page.mouse.click is not available")
    _dismiss_chatgpt_persistent_storage_prompt(page, log=log)
    profile = _wait_for_dom_target(
        page,
        _PROFILE_MENU_TARGET_SCRIPT,
        label="bottom account button",
        timeout_ms=min(_remaining_ms(deadline), 12000),
    )
    if not isinstance(profile, dict) or not profile.get("ok"):
        summary = str((profile or {}).get("summary") if isinstance(profile, dict) else profile or "")[:240]
        raise RuntimeError(f"bottom account button not visible; refusing to click sidebar nav: {summary}")
    _safe_log(log, "Workspace Join: fixed step 1/3 click bottom account")
    _click_dom_target(page, profile, label="bottom account button", log=log)
    _page_wait(page, 650)

    account = _wait_for_dom_target(
        page,
        _ACCOUNT_SUBMENU_TARGET_SCRIPT,
        label="account submenu arrow row",
        timeout_ms=min(_remaining_ms(deadline), 7000),
    )
    if not isinstance(account, dict) or not account.get("ok"):
        summary = str((account or {}).get("summary") if isinstance(account, dict) else account or "")[:240]
        raise RuntimeError(f"account submenu arrow row not visible after bottom account click: {summary}")
    _safe_log(log, "Workspace Join: fixed step 2/3 click account submenu arrow")
    _click_dom_target(page, account, label="account submenu arrow row", log=log)
    _move_mouse_to_target(page, account)
    _page_wait(page, 650)
    _debug_workspace_step2_snapshot(page, account, log=log)

    workspace = _wait_for_dom_target_with_arg(
        page,
        _WORKSPACE_RADIO_TARGET_SCRIPT,
        account,
        label="workspace row",
        timeout_ms=min(_remaining_ms(deadline), 5000),
    )
    if not isinstance(workspace, dict) or not workspace.get("ok"):
        workspace = _wait_for_dom_target_with_arg(
            page,
            _WORKSPACE_RADIO_FALLBACK_TARGET_SCRIPT,
            account,
            label="workspace row fallback",
            timeout_ms=min(_remaining_ms(deadline), 1500),
        )
    if not isinstance(workspace, dict) or not workspace.get("ok"):
        summary = str((workspace or {}).get("summary") if isinstance(workspace, dict) else workspace or "")[:240]
        raise RuntimeError(f"workspace row not visible after account submenu click: {summary}")
    _safe_log(
        log,
        "Workspace Join: fixed step 3/3 click workspace row: "
        f"text={workspace.get('text') or '-'} "
        f"rect={workspace.get('rect') or {}} "
        f"epText={workspace.get('elementFromPointText') or '-'} "
        f"clickXY=({workspace.get('x')},{workspace.get('y')})",
    )
    return _finish_workspace_selection_from_open_menu(
        page,
        workspace,
        deadline=deadline,
        workspace_id=workspace_id,
        log=log,
    )


def switch_chatgpt_workspace_via_profile_menu(
    page,
    *,
    workspace_id: str = "",
    timeout_ms: int = 45000,
    allow_already_workspace: bool = True,
    log=None,
) -> dict[str, Any]:
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=60000)
    deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000
    try:
        existing_profile_text = _profile_workspace_text(page, min(int(timeout_ms), 2500))
    except Exception:
        existing_profile_text = ""
    if existing_profile_text and allow_already_workspace:
        result = {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": "",
            "profileText": existing_profile_text,
            "alreadyWorkspace": True,
        }
        if callable(log):
            try:
                log(f"Workspace Join: already in workspace {existing_profile_text}")
            except Exception:
                pass
        return result
    if existing_profile_text:
        _safe_log(
            log,
            "Workspace Join: profile is already in a workspace, but target "
            f"{workspace_id or '-'} was not verified by session API; continuing menu switch",
        )
    last_stepwise_exc: Exception | None = None
    for attempt in range(1, 4):
        if attempt > 1:
            if time.monotonic() >= deadline:
                break
            _safe_log(
                log,
                "Workspace Join: workspace not visible in account menu yet; "
                f"refreshing and retrying profile switch ({attempt}/3)",
            )
            try:
                _hard_refresh_chatgpt_home(page, log=log)
            except Exception as refresh_exc:
                _safe_log(log, f"Workspace Join: refresh before workspace switch retry failed: {refresh_exc}")
                _page_wait(page, 1500)
            try:
                existing_profile_text = _profile_workspace_text(page, min(_remaining_ms(deadline), 2500))
            except Exception:
                existing_profile_text = ""
            if existing_profile_text and allow_already_workspace:
                result = {
                    "ok": True,
                    "workspaceId": str(workspace_id or "").strip(),
                    "selectedText": "",
                    "profileText": existing_profile_text,
                    "alreadyWorkspace": True,
                    "retryAttempt": attempt,
                }
                _safe_log(log, f"Workspace Join: already in workspace after refresh {existing_profile_text}")
                return result
            if existing_profile_text:
                _safe_log(
                    log,
                    "Workspace Join: still in a workspace after refresh, but target "
                    f"{workspace_id or '-'} is not verified; retrying menu switch",
                )
        try:
            result = _switch_workspace_via_profile_menu_stepwise(
                page,
                workspace_id=workspace_id,
                timeout_ms=min(_remaining_ms(deadline, minimum=2500), int(timeout_ms)),
                log=log,
            )
            if isinstance(result, dict) and result.get("ok"):
                if callable(log):
                    try:
                        log(
                            "Workspace Join: switched profile menu to "
                            f"{result.get('profileText') or result.get('selectedText') or workspace_id}"
                        )
                    except Exception:
                        pass
                return result
        except Exception as stepwise_exc:
            if _looks_like_navigation_interrupt(stepwise_exc):
                result = _recover_workspace_ready_after_navigation(
                    page,
                    workspace_id=workspace_id,
                    timeout_ms=timeout_ms,
                )
                if callable(log):
                    try:
                        log(
                            "Workspace Join: switched profile menu to "
                            f"{result.get('profileText') or result.get('selectedText') or workspace_id}"
                        )
                    except Exception:
                        pass
                return result
            last_stepwise_exc = stepwise_exc
            if _looks_like_workspace_menu_not_ready(stepwise_exc) and attempt < 3:
                continue
            _safe_log(log, f"Workspace Join: fixed three-step profile menu switch failed: {stepwise_exc}")
            raise RuntimeError(f"workspace switch fixed three-step failed: {stepwise_exc}") from stepwise_exc
    if last_stepwise_exc is not None:
        _safe_log(
            log,
            "Workspace Join: workspace accepted but not visible in ChatGPT account menu yet; "
            "will not save personal/free JSON. "
            f"last error: {last_stepwise_exc}",
        )
        raise RuntimeError(
            "workspace switch fixed three-step failed: workspace accepted but not visible "
            f"in ChatGPT account menu yet: {last_stepwise_exc}"
        ) from last_stepwise_exc
    try:
        result = page.evaluate(
            """
            async ({ workspaceId, timeoutMs }) => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const text = (el) => String(el?.innerText || el?.textContent || el?.getAttribute?.("aria-label") || "").trim();
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
              };
              const fire = (el, type) => {
                el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
              };
              const clickLikeUser = (el) => {
                for (const type of ["pointerover", "pointerenter", "mouseover", "mouseenter", "pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                  fire(el, type);
                }
              };
              const waitFor = async (fn, label) => {
                const end = Date.now() + timeoutMs;
                let last;
                while (Date.now() < end) {
                  last = fn();
                  if (last) return last;
                  await sleep(150);
                }
                throw new Error(`Timed out waiting for ${label}`);
              };
              const profileButton = () => document.querySelector('[data-testid="accounts-profile-button"]');
              const workspacePattern = /Workspace|\\u5de5\\u4f5c\\u7a7a\\u95f4|\\u5de5\\u4f5c\\u533a/i;
              const schoolPattern = /School|High\\s*School|Elementary|Middle\\s*School|College|University|Academy|District|Education|\\bEDU\\b|K12|\\.edu\\b/i;
              const proWorkspacePattern = /\\bGPT\\s*PRO\\b/i;
              const personalPattern = /Personal|Free|Plus|Pro|\\bAccount\\b|\\u4e2a\\u4eba\\u5e10\\u6237|\\u4e2a\\u4eba\\u8d26\\u6237|\\u5e10\\u6237|\\u8d26\\u6237/i;
              const hasWorkspaceSignal = (raw) => workspacePattern.test(raw) || schoolPattern.test(raw) || proWorkspacePattern.test(raw);
              const workspaceLike = (raw) => raw && hasWorkspaceSignal(raw) && !(personalPattern.test(raw) && !proWorkspacePattern.test(raw));
              const selectedWorkspaceLike = (raw) => raw && !(personalPattern.test(raw) && !proWorkspacePattern.test(raw));
              const scoreChoice = (item) => {
                const raw = text(item);
                let score = 0;
                if (proWorkspacePattern.test(raw)) score += 140;
                if (workspacePattern.test(raw)) score += 90;
                if (schoolPattern.test(raw)) score += 80;
                if (!personalPattern.test(raw) || proWorkspacePattern.test(raw)) score += 20;
                if (personalPattern.test(raw) && !proWorkspacePattern.test(raw)) score -= 100;
                return score;
              };

              const profile = await waitFor(() => {
                const candidate = profileButton();
                return visible(candidate) ? candidate : null;
              }, "profile button");
              clickLikeUser(profile);

              const submenuTrigger = await waitFor(() => {
                const items = Array.from(document.querySelectorAll('[role="menuitem"][data-has-submenu], [role="menuitem"][aria-haspopup="menu"]')).filter(visible);
                return items.find((item) => /Personal|Account|帐户|账户/.test(text(item))) || items[0] || null;
              }, "account submenu");
              clickLikeUser(submenuTrigger);
              fire(submenuTrigger, "mousemove");
              await sleep(350);

              const radioItems = await waitFor(() => {
                const items = Array.from(document.querySelectorAll('[role="menuitemradio"]')).filter(visible);
                return items.length ? items : null;
              }, "workspace radio items");
              const checkedWorkspace = radioItems.find((item) => item.getAttribute("aria-checked") === "true" && selectedWorkspaceLike(text(item)));
              if (checkedWorkspace) {
                const profileText = text(checkedWorkspace);
                return { ok: true, workspaceId, selectedText: profileText, profileText, alreadyWorkspace: true };
              }
              const unchecked = radioItems.filter((item) => item.getAttribute("aria-checked") === "false");
              const target = unchecked
                .map((item) => ({ item, score: scoreChoice(item) }))
                .sort((a, b) => b.score - a.score)
                .find((entry) => entry.score > 0)?.item || unchecked[0];
              if (!target) {
                throw new Error("No selectable workspace item found");
              }
              const selectedText = text(target);
              clickLikeUser(target);

              const profileText = await waitFor(() => {
                const current = profileButton();
                const currentText = text(current);
                if (!currentText) return "";
                if (selectedText && currentText.includes(selectedText)) return currentText;
                if (workspaceLike(currentText)) return currentText;
                if (/Workspace/i.test(currentText) && !/Personal|个人帐户|个人账户/.test(currentText)) return currentText;
                return "";
              }, "profile button workspace text");
              return { ok: true, workspaceId, selectedText, profileText };
            }
            """,
            {"workspaceId": str(workspace_id or "").strip(), "timeoutMs": int(timeout_ms)},
        )
    except Exception as exc:
        if not _looks_like_navigation_interrupt(exc):
            raise
        profile_text = _profile_workspace_text(page, int(timeout_ms))
        result = {
            "ok": True,
            "workspaceId": str(workspace_id or "").strip(),
            "selectedText": "",
            "profileText": profile_text,
            "navigationRecovered": True,
        }
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"workspace switch failed: {result}")
    if callable(log):
        try:
            log(f"Workspace Join: switched profile menu to {result.get('profileText') or result.get('selectedText') or workspace_id}")
        except Exception:
            pass
    return result


def export_workspace_cpa_session_from_browser(
    page,
    *,
    workspace_id: str = "",
    output_dir: str | Path | None = None,
    now: datetime | None = None,
    log=None,
) -> dict[str, Any]:
    workspace_id = str(workspace_id or "").strip()
    cpa_json: dict[str, Any] | None = None
    if workspace_id:
        _refresh_chatgpt_after_workspace_switch(page, log=log)
        cpa_json = _try_workspace_cpa_json_from_session(
            page,
            workspace_id=workspace_id,
            now=now,
            log=log,
        )
        if cpa_json is None:
            _safe_log(log, "Workspace Join: switching workspace via profile menu")
            switch_chatgpt_workspace_via_profile_menu(
                page,
                workspace_id=workspace_id,
                timeout_ms=55000,
                log=log,
            )
            _refresh_chatgpt_after_workspace_switch(page, log=log)

    session_url = f"{CHATGPT_APP}/api/auth/session"
    attempts = 4 if workspace_id else 1
    last_workspace_error: Exception | None = None
    if cpa_json is None:
        for attempt in range(1, attempts + 1):
            session_json = _load_chatgpt_session_json(page, attempt=attempt, total=attempts, log=log)
            candidate = convert_chatgpt_session_to_cpa_json(session_json, now=now)
            if not workspace_id:
                cpa_json = candidate
                break
            try:
                assert_workspace_cpa_json(candidate, workspace_id=workspace_id)
                cpa_json = candidate
                break
            except ValueError as exc:
                last_workspace_error = exc
                plan = candidate.get("chatgpt_plan_type") or candidate.get("plan_type") or "-"
                account_id = candidate.get("chatgpt_account_id") or candidate.get("account_id") or "-"
                _safe_log(
                    log,
                    "Workspace Join: session API not in workspace context yet "
                    f"(attempt {attempt}/{attempts}) plan={plan} account_id={account_id}",
                )
                if attempt < attempts:
                    _refresh_chatgpt_after_workspace_switch(page, log=log)
                    _page_wait(page, 1200)
    if cpa_json is None:
        if last_workspace_error is not None:
            raise last_workspace_error
        raise ValueError("session API did not return exportable JSON")
    if workspace_id:
        cpa_json.setdefault("workspace_id", workspace_id)
        cpa_json.setdefault("organization_id", workspace_id)
        if callable(log):
            try:
                log(
                    "Workspace Join: verified workspace session "
                    f"plan={cpa_json.get('chatgpt_plan_type') or cpa_json.get('plan_type') or '-'} "
                    f"account_id={cpa_json.get('chatgpt_account_id') or cpa_json.get('account_id') or '-'}"
                )
            except Exception:
                pass
    path = save_cpa_json_locally(
        cpa_json,
        email=str(cpa_json.get("email") or ""),
        output_dir=output_dir,
        now=now,
    )
    if callable(log):
        try:
            log(f"Workspace Join: CPA JSON saved to {path}")
        except Exception:
            pass
    return {
        "ok": True,
        "path": str(path),
        "workspace_id": workspace_id,
        "session_url": session_url,
        "email": cpa_json.get("email", ""),
        "account_id": cpa_json.get("account_id", ""),
        "chatgpt_account_id": cpa_json.get("chatgpt_account_id", ""),
        "expired": cpa_json.get("expired", ""),
        "access_token": cpa_json.get("access_token", ""),
        "refresh_token": cpa_json.get("refresh_token", ""),
        "id_token": cpa_json.get("id_token", ""),
        "session_token": cpa_json.get("session_token", ""),
    }
