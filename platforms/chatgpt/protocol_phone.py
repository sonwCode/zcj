"""ChatGPT phone-first registration over the HTTP auth protocol."""

from __future__ import annotations

import json
import base64
import secrets
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from core.proxy_utils import infer_proxy_region, pin_711proxy_session
from platforms.chatgpt.constants import (
    CHATGPT_APP,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    OPENAI_AUTH,
    OPENAI_API_ENDPOINTS,
)
from platforms.chatgpt.oauth import (
    generate_oauth_url,
    normalize_oauth_token_response,
    submit_callback_url,
)
from platforms.chatgpt.register import (
    RegistrationEngine,
    RegistrationResult,
    _extract_chatgpt_account_id,
    _extract_chatgpt_session_credentials,
    _generate_datadog_trace_headers,
    _response_error,
)


class _PhoneIdentityService:
    service_type = type("ST", (), {"value": "phone"})()


_PHONE_PREFIX_TO_ISO = {
    "1": "US",
    "31": "NL",
    "44": "GB",
    "49": "DE",
    "55": "BR",
    "56": "CL",
    "57": "CO",
    "60": "MY",
    "62": "ID",
    "63": "PH",
    "84": "VN",
    "91": "IN",
    "94": "LK",
}


def _phone_country_iso(phone_number: str) -> str:
    digits = "".join(char for char in str(phone_number or "") if char.isdigit())
    for length in (3, 2, 1):
        iso = _PHONE_PREFIX_TO_ISO.get(digits[:length])
        if iso:
            return iso
    return ""


def _find_callback_url(value, depth: int = 0) -> str:
    """Find an OAuth callback URL in a bounded JSON-like response tree."""
    if depth > 6:
        return ""
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return ""
        query = parse_qs(urlsplit(candidate).query, keep_blank_values=True)
        if query.get("code") and query.get("state"):
            return candidate
        return ""
    if isinstance(value, dict):
        for item in value.values():
            found = _find_callback_url(item, depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_callback_url(item, depth + 1)
            if found:
                return found
    return ""


def _export_session_cookies(session) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    jar = getattr(getattr(session, "cookies", None), "jar", None)
    if jar is not None:
        for cookie in jar:
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if not name or not value:
                continue
            records.append(
                {
                    "name": name,
                    "value": value,
                    "domain": str(getattr(cookie, "domain", "") or ""),
                    "path": str(getattr(cookie, "path", "") or "/"),
                }
            )
    if records:
        return records

    cookies = getattr(session, "cookies", None)
    items = getattr(cookies, "items", None)
    if callable(items):
        for name, value in items():
            if name and value:
                records.append(
                    {"name": str(name), "value": str(value), "domain": "", "path": "/"}
                )
    return records


def _cookies_to_header(records: list[dict[str, str]]) -> str:
    values: dict[str, str] = {}
    for item in records:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name and value:
            values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _parse_cookie_records(value) -> list[dict[str, str]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return [dict(item) for item in parsed if isinstance(item, dict)]
    records: list[dict[str, str]] = []
    for part in raw.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and name and cookie_value:
            records.append(
                {"name": name, "value": cookie_value, "domain": "", "path": "/"}
            )
    return records


def _cookie_value(session, name: str) -> str:
    """Read a cookie even when the jar contains the same name on two domains."""
    cookies = getattr(session, "cookies", None)
    try:
        value = cookies.get(name) if cookies is not None else ""
        if value:
            return str(value)
    except Exception:
        pass
    jar = getattr(cookies, "jar", None)
    if jar is not None:
        for cookie in reversed(list(jar)):
            if str(getattr(cookie, "name", "") or "") == name:
                value = str(getattr(cookie, "value", "") or "")
                if value:
                    return value
    return ""


def _decode_client_auth_session(raw_cookie: str) -> dict:
    first = str(raw_cookie or "").strip().split(".", 1)[0]
    if not first:
        return {}
    try:
        padding = "=" * ((4 - len(first) % 4) % 4)
        decoded = base64.urlsafe_b64decode((first + padding).encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_phone_otp_number_rejection(error_code: str, error_message: str) -> bool:
    code = str(error_code or "").strip().lower()
    message = str(error_message or "").strip().lower()
    return (
        code in {
            "invalid_phone_number",
            "phone_number_invalid",
            "phone_number_disallowed",
            "voip_phone_disallowed",
            "fraud_guard",
            "phone_number_rejected",
            "unsupported_phone_number",
        }
        or "suspicious behavior from phone numbers" in message
        or "phone numbers similar to yours" in message
        or "invalid phone number" in message
        or "fraud_guard" in message
        or "fraud guard" in message
        or "disallowed phone" in message
        or "unsupported phone" in message
    )


def _is_phone_risk_rejection(error_code: str, error_message: str) -> bool:
    """Return whether the remote explicitly classified the phone cohort as risky.

    This is different from a malformed or already-used individual number. Once
    the account/session receives a fraud-guard or similar-number verdict,
    rotating more provider tiers on that same account only adds retries to an
    already risked authorization transaction.
    """
    code = str(error_code or "").strip().lower()
    message = str(error_message or "").strip().lower()
    return (
        code == "fraud_guard"
        or "suspicious behavior from phone numbers" in message
        or "phone numbers similar to yours" in message
        or "fraud_guard" in message
        or "fraud guard" in message
    )


def _is_phone_account_rate_limited(error_code: str, error_message: str) -> bool:
    """Account/session level limit — stop swapping numbers on this account."""
    code = str(error_code or "").strip().lower()
    message = str(error_message or "").strip().lower()
    return (
        code in {
            "rate_limit_exceeded",
            "too_many_requests",
            "phone_verification_rate_limited",
        }
        or "too many phone verification" in message
        or "too many phone" in message
        or ("rate_limit" in code and "phone" in message)
        or ("rate limit" in message and "phone" in message)
        or ("too many" in message and "verification" in message)
    )


def _is_account_deactivated(error_code: str, error_message: str) -> bool:
    """Return True when the server has invalidated the email account/session.

    This is account-scoped, not a bad SMS number.  Retrying with another
    activation only burns provider inventory and can make the diagnostic less
    useful, so callers must terminate the current account flow immediately.
    """
    code = str(error_code or "").strip().lower()
    message = str(error_message or "").strip().lower()
    return (
        code in {"account_deactivated", "email_account_deactivated", "account_deleted"}
        or "deleted or deactivated" in message
        or "you do not have an account" in message
    )


class ChatGPTProtocolPhoneWorker:
    def __init__(
        self,
        *,
        phone_callback,
        proxy_url: str | None = None,
        log_fn: Callable[[str], None] = print,
        cancel_check=None,
        max_phone_attempts: int = 3,
        proxy_country: str = "",
        proxy_session_id: str = "",
        mailbox_factory: Callable[[str | None], object] | None = None,
        bind_email_after_registration: bool = True,
        email_otp_timeout_seconds: int = 300,
    ):
        self.phone_callback = phone_callback
        self.proxy_url = proxy_url
        self.log_fn = log_fn
        self.cancel_check = cancel_check if callable(cancel_check) else (lambda: False)
        if hasattr(self.phone_callback, "set_cancel_check"):
            self.phone_callback.set_cancel_check(self.cancel_check)
        self.max_phone_attempts = max(int(max_phone_attempts or 3), 1)
        self.proxy_country = str(proxy_country or "").strip().upper()
        self.proxy_session_id = (
            "".join(char for char in str(proxy_session_id or "") if char.isalnum())[:11]
            or secrets.token_hex(5)
        )
        self.mailbox_factory = mailbox_factory
        self.bind_email_after_registration = bool(bind_email_after_registration)
        self.email_otp_timeout_seconds = min(
            max(int(email_otp_timeout_seconds or 300), 60),
            600,
        )
        self._route_mismatch_logged = False
        self._route_mismatch_failures = 0
        self._similar_number_rejections = 0
        self._codex_oauth_start = None

    def _log(self, message: str, level: str = "info") -> None:
        if level == "info":
            self.log_fn(message)
            return
        try:
            self.log_fn(message, level=level)
        except TypeError:
            self.log_fn(message)

    def _reset_number(self, reason: str) -> None:
        callback = self.phone_callback
        if hasattr(callback, "mark_send_failed"):
            callback.mark_send_failed(reason)
        if hasattr(callback, "cleanup"):
            callback.cleanup()
        if hasattr(callback, "phase"):
            callback.phase = "need_number"
            callback.activation = None
            callback.completed = False

    def _validate_phone_otp(self, engine: RegistrationEngine, code: str) -> bool:
        headers = {
            "referer": f"{OPENAI_AUTH}/phone-verification",
            "origin": OPENAI_AUTH,
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            **_generate_datadog_trace_headers(),
        }
        if engine._device_id:
            headers["oai-device-id"] = engine._device_id
        response = engine.session.post(
            OPENAI_API_ENDPOINTS["validate_phone_otp"],
            headers=headers,
            data=json.dumps({"code": str(code or "").strip()}),
            timeout=20,
        )
        self._log(f"手机号验证码校验状态: {response.status_code}")
        if response.status_code != 200:
            server_code, server_message = _response_error(response)
            engine._step_error_code = server_code or "phone_otp_validation_failed"
            engine._step_error_message = server_message or f"HTTP {response.status_code}"
            return False
        data = response.json() or {}
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        engine._otp_response_data = dict(data)
        engine._otp_continue_url = str(
            data.get("continue_url")
            or payload.get("continue_url")
            or payload.get("url")
            or ""
        ).strip()
        engine._otp_page_type = str(page.get("type") or "").strip()
        self._log(
            "手机号验证后 OAuth 状态: "
            f"page={engine._otp_page_type or 'unknown'} "
            f"continue={'yes' if engine._otp_continue_url else 'no'}"
        )
        return True

    def _send_phone_otp(
        self,
        engine: RegistrationEngine,
        phone_number: str,
        *,
        resend: bool = False,
    ) -> bool:
        page_url = str(
            getattr(engine, "_password_continue_url", "")
            or f"{OPENAI_AUTH}/phone-verification"
        )
        if not resend:
            try:
                page_response = engine.session.get(
                    page_url,
                    headers={
                        "referer": f"{OPENAI_AUTH}/create-account/password",
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                    timeout=15,
                )
                self._log(f"手机号验证码页加载状态: {page_response.status_code}")
            except Exception as exc:
                engine._step_error_code = "phone_otp_send_failed"
                engine._step_error_message = str(exc)
                return False

        page_payload = dict(getattr(engine, "_password_next_payload", None) or {})
        channel = str(
            page_payload.get("phone_verification_channel")
            or page_payload.get("channel")
            or "sms"
        ).strip().lower()
        if channel not in {"sms", "whatsapp"}:
            channel = "sms"
        headers = {
            "referer": page_url,
            "origin": OPENAI_AUTH,
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            **_generate_datadog_trace_headers(),
        }
        if engine._device_id:
            headers["oai-device-id"] = engine._device_id

        remembered_body = getattr(engine, "_phone_otp_send_body", None)
        body_candidates = [remembered_body] if isinstance(remembered_body, dict) else [
            {"phone_number": phone_number, "phone_verification_channel": channel},
            {"phone_number": phone_number, "channel": channel},
            {"channel": channel},
            {},
        ]
        last_code = ""
        last_message = ""
        for body in body_candidates:
            try:
                response = engine.session.post(
                    OPENAI_API_ENDPOINTS["send_phone_otp"],
                    headers=headers,
                    data=json.dumps(body),
                    timeout=15,
                )
            except Exception as exc:
                last_code = "proxy_network_error"
                last_message = str(exc)
                continue
            self._log(
                f"手机号验证码{'重发' if resend else '发送'}状态: {response.status_code} "
                f"body_fields={','.join(body.keys()) or 'empty'}"
            )
            if response.status_code in (200, 201, 204):
                engine._phone_otp_send_body = dict(body)
                try:
                    data = response.json()
                    engine._otp_continue_url = str(data.get("continue_url") or "")
                    engine._otp_page_type = str((data.get("page") or {}).get("type") or "")
                    self._log(f"手机号验证码发送后页面类型: {engine._otp_page_type or 'unknown'}")
                except Exception:
                    pass
                return True
            last_code, last_message = _response_error(response)
            message_lower = str(last_message or "").lower()
            if _is_phone_otp_number_rejection(last_code, last_message):
                last_code = "phone_number_rejected"
                self._log("手机号段在短信发送阶段被风控，立即释放并换号")
                break
            if response.status_code in (409, 429) and any(
                marker in message_lower for marker in ("already", "recent", "wait", "sent")
            ):
                return True
        engine._step_error_code = last_code or "phone_otp_send_failed"
        engine._step_error_message = last_message or "手机号验证码发送接口未成功"
        return False

    def _send_add_phone_number(
        self,
        engine: RegistrationEngine,
        phone_number: str,
        page_url: str,
    ) -> bool:
        """Submit a new phone on the add_phone auth step.

        ``phone-otp/send`` is only valid after the number has been accepted.
        The add_phone page has a separate transition that both registers the
        candidate number for this auth transaction and sends its first OTP.
        """
        request_url = OPENAI_API_ENDPOINTS["send_add_phone"]
        headers = {
            "referer": page_url or f"{OPENAI_AUTH}/add-phone",
            "origin": OPENAI_AUTH,
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            **_generate_datadog_trace_headers(),
        }
        if engine._device_id:
            headers["oai-device-id"] = engine._device_id

        try:
            response = engine.session.post(
                request_url,
                headers=headers,
                data=json.dumps({"phone_number": phone_number}),
                allow_redirects=False,
                timeout=20,
            )
        except Exception as exc:
            engine._step_error_code = "proxy_network_error"
            engine._step_error_message = str(exc)
            return False

        self._log(f"add-phone/send 状态: {response.status_code}")
        if response.status_code not in (200, 201, 204):
            code, message = _response_error(response)
            if _is_phone_account_rate_limited(code, message):
                engine._step_error_code = "rate_limit_exceeded"
            elif _is_phone_otp_number_rejection(code, message):
                engine._step_error_code = "phone_number_rejected"
            elif str(code or "").strip().lower() in {
                "invalid_auth_step",
                "invalid_state",
            }:
                engine._step_error_code = "phone_authorization_invalid"
            else:
                engine._step_error_code = code or "phone_otp_send_failed"
            engine._step_error_message = message or f"HTTP {response.status_code}"
            self._log(
                f"add-phone/send 失败: code={code or '-'} HTTP {response.status_code}",
            )
            return False

        try:
            data = response.json() or {}
        except Exception:
            data = {}
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        continue_url = str(
            data.get("continue_url")
            or payload.get("continue_url")
            or payload.get("url")
            or ""
        ).strip()
        page_type = str(page.get("type") or "").strip()
        engine._otp_continue_url = continue_url
        engine._otp_page_type = page_type
        engine._password_next_payload = dict(payload)
        if continue_url:
            engine._password_continue_url = continue_url
        if page_type != "phone_otp_verification" and "phone-verification" not in continue_url:
            engine._step_error_code = "phone_otp_send_failed"
            engine._step_error_message = (
                f"add-phone/send 未进入手机号验证码页: {page_type or continue_url or 'unknown'}"
            )
            return False
        self._log(f"add-phone/send 后页面类型: {page_type or 'phone_verification'}")
        return True

    def _collect_session(
        self,
        engine: RegistrationEngine,
        phone_number: str,
        proxy_url: str | None,
    ) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=engine.logs)
        callback_url = str(engine._create_account_continue_url or "")
        if not callback_url or "code=" not in callback_url:
            result.error_code = "account_created_session_missing"
            result.error_message = "create_account 未返回有效的 callback URL"
            return result
        callback_response = engine.session.get(callback_url, timeout=25)
        self._log(f"OAuth callback 状态: {callback_response.status_code}")
        session_token = engine.session.cookies.get("__Secure-next-auth.session-token")
        session_response = engine.session.get(
            f"{CHATGPT_APP}/api/auth/session",
            headers={"accept": "application/json"},
            timeout=20,
        )
        session_data = session_response.json()
        token_data = _extract_chatgpt_session_credentials(
            session_data,
            getattr(engine, "_oauth_token_info", None),
        )
        access_token = token_data["access_token"]
        if not access_token:
            result.error_code = "account_created_session_missing"
            result.error_message = "chatgpt.com session 未返回 accessToken"
            return result
        account = session_data.get("account") if isinstance(session_data, dict) else {}
        account_id = _extract_chatgpt_account_id(access_token) or str(
            (account or {}).get("id") or (account or {}).get("account_id") or ""
        )
        result.success = bool(account_id)
        result.email = f"phone:{phone_number}"
        result.password = str(engine.password or "")
        result.account_id = account_id
        result.access_token = access_token
        result.refresh_token = token_data["refresh_token"]
        result.id_token = token_data["id_token"]
        result.session_token = str(session_token or "")
        result.error_code = "" if result.success else "account_created_session_missing"
        result.error_message = "" if result.success else "session 缺少 account_id"
        auth_cookies = _export_session_cookies(engine.session)
        result.metadata = {
            "phone_number": phone_number,
            "register_mode": "phone",
            "session": session_data,
            "cookies": _cookies_to_header(auth_cookies),
            "auth_cookies": auth_cookies,
            "oai_device_id": str(engine._device_id or ""),
            "auth_proxy_url": str(proxy_url or ""),
            "auth_proxy_country": _phone_country_iso(phone_number),
            "auth_proxy_session": self.proxy_session_id,
            "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": token_data["expires_at"],
            "access_token_expires_at": token_data["expires_at"],
            "refresh_token_status": token_data["refresh_token_status"],
            "refresh_token_source": token_data["refresh_token_source"],
            "session_token_present": bool(session_token),
            "refresh_token_present": bool(result.refresh_token),
        }
        return result

    def _run_number(
        self,
        phone_number: str,
        password: str,
        proxy_url: str | None,
    ) -> RegistrationResult:
        active_proxy = proxy_url
        network_attempt = 0
        route_rotated = False
        while True:
            engine = RegistrationEngine(
                email_service=_PhoneIdentityService(),
                proxy_url=active_proxy,
                callback_logger=self.log_fn,
            )
            engine.email = phone_number
            engine.password = password
            if not engine._init_session():
                error_code = "proxy_network_error"
                error_message = "手机号协议会话初始化失败"
                did = None
            elif not engine._start_oauth():
                error_code = engine._step_error_code or "oauth_start_failed"
                error_message = (
                    "手机号协议线路被 ChatGPT 拦截"
                    if error_code == "proxy_or_access_blocked"
                    else "手机号 OAuth 启动失败"
                )
                did = None
            else:
                did = engine._get_device_id()
                error_code = engine._step_error_code or "oauth_authorize_failed"
                error_message = "手机号授权态创建失败"

            if did:
                break
            if error_code != "proxy_network_error":
                return RegistrationResult(
                    success=False,
                    error_code=error_code,
                    error_message=error_message,
                )

            network_attempt += 1
            if network_attempt < 3:
                self._log(
                    f"手机号 OAuth 网络中断，保持当前号码重建会话 ({network_attempt}/3)"
                )
                time.sleep(network_attempt)
                continue
            if not route_rotated:
                fresh_session_id = secrets.token_hex(5)
                fresh_proxy = pin_711proxy_session(
                    active_proxy,
                    region=_phone_country_iso(phone_number),
                    session_id=fresh_session_id,
                    session_minutes=180,
                )
                if fresh_proxy and fresh_proxy != active_proxy:
                    self.proxy_session_id = fresh_session_id
                    active_proxy = fresh_proxy
                    route_rotated = True
                    network_attempt = 0
                    self._log(
                        "手机号 OAuth 连续断连，已切换同国家新 711Proxy Session 后继续当前号码"
                    )
                    continue
            return RegistrationResult(
                success=False,
                error_code="proxy_network_error",
                error_message=error_message,
            )
        final_url = str(engine._authorize_final_url or "")
        if "/log-in/password" in final_url:
            return RegistrationResult(success=False, error_code="phone_number_in_use", error_message="手机号已绑定现有账号")
        if "/create-account/password" not in final_url:
            return RegistrationResult(success=False, error_code="phone_flow_state_invalid", error_message=f"手机号授权落点异常: {final_url[:160]}")

        password_ok, registered_password = engine._register_password()
        if not password_ok:
            error_code = engine._step_error_code or "password_registration_failed"
            return RegistrationResult(
                success=False,
                error_code=error_code,
                error_message=engine._step_error_message or "手机号注册密码提交失败",
            )
        actual_password = str(registered_password or engine.password or password)
        engine.password = actual_password
        password_page_type = str(getattr(engine, "_password_next_page_type", "") or "")
        if password_page_type in {"phone_otp_send", "phone_otp_select_channel"}:
            self._log(f"手机号注册进入 {password_page_type}，显式触发短信发送...")
            if not self._send_phone_otp(engine, phone_number):
                return RegistrationResult(
                    success=False,
                    error_code=engine._step_error_code or "phone_otp_send_failed",
                    error_message=engine._step_error_message or "手机号验证码发送失败",
                )
        if hasattr(self.phone_callback, "set_resend_callback"):
            self.phone_callback.set_resend_callback(
                lambda: self._send_phone_otp(engine, phone_number, resend=True)
            )
        if hasattr(self.phone_callback, "mark_send_succeeded"):
            self.phone_callback.mark_send_succeeded()
        code = str(self.phone_callback() or "").strip()
        if not code:
            return RegistrationResult(success=False, error_code="phone_otp_timeout", error_message="等待短信验证码超时")
        if not self._validate_phone_otp(engine, code):
            if hasattr(self.phone_callback, "mark_code_failed"):
                self.phone_callback.mark_code_failed(engine._step_error_message)
            return RegistrationResult(
                success=False,
                error_code=engine._step_error_code or "phone_otp_validation_failed",
                error_message=engine._step_error_message or "短信验证码校验失败",
            )
        if hasattr(self.phone_callback, "report_success"):
            self.phone_callback.report_success()
        if not engine._create_user_account():
            return RegistrationResult(
                success=False,
                error_code=engine._step_error_code or "account_creation_failed",
                error_message=engine._step_error_message or "创建用户资料失败",
            )
        result = self._collect_session(engine, phone_number, active_proxy)
        if not result.success or not self.bind_email_after_registration:
            return result
        if self.mailbox_factory is None:
            result.metadata["email_binding_status"] = "failed"
            result.metadata["email_binding_error"] = "未配置邮箱 Provider"
            self._log("手机号账号已创建，但邮箱绑定未执行: 未配置邮箱 Provider")
            return result

        from platforms.chatgpt.bind_email import ChatGPTProtocolBindEmailWorker

        mailbox = self.mailbox_factory(active_proxy)
        bind_worker = ChatGPTProtocolBindEmailWorker(
            phone_number=phone_number,
            password=result.password or actual_password,
            mailbox=mailbox,
            proxy_url=active_proxy,
            otp_timeout_seconds=self.email_otp_timeout_seconds,
            log_fn=self.log_fn,
            cancel_check=self.cancel_check,
            existing_access_token=result.access_token,
            existing_session_token=result.session_token,
        )
        self._log("手机号 Free 账号已创建，复用当前会话开始添加邮箱")
        try:
            bound = bind_worker.bind_with_engine(engine)
        except Exception as exc:
            result.metadata["email_binding_status"] = "failed"
            result.metadata["email_binding_error"] = str(exc)
            self._log(f"手机号账号已保留，邮箱绑定失败，可稍后重试: {exc}")
            return result

        mailbox_account = bind_worker.mailbox_account
        mailbox_extra = dict(getattr(mailbox_account, "extra", None) or {})
        provider_key = str(mailbox_extra.get("mailbox_provider_key") or "")
        if hasattr(mailbox, "mark_registration_success"):
            try:
                mailbox.mark_registration_success(mailbox_account)
            except Exception as exc:
                self._log(f"邮箱已绑定，但邮箱池成功标签写入失败: {exc}")

        result.email = str(bound.get("email") or result.email)
        result.access_token = str(bound.get("access_token") or result.access_token)
        result.refresh_token = str(bound.get("refresh_token") or result.refresh_token)
        result.id_token = str(bound.get("id_token") or result.id_token)
        result.session_token = str(bound.get("session_token") or result.session_token)
        result.account_id = str(bound.get("account_id") or result.account_id)
        result.metadata.update(
            {
                "register_mode": "phone_with_email",
                "email_binding_status": "success",
                "email_binding_error": "",
                "mailbox_provider": provider_key,
                "verification_mailbox": {
                    "provider": provider_key,
                    "email": result.email,
                    "account_id": str(getattr(mailbox_account, "account_id", "") or ""),
                },
                "cookies": str(bound.get("cookies") or result.metadata.get("cookies") or ""),
                "auth_cookies": bound.get("auth_cookies") or result.metadata.get("auth_cookies") or [],
                "oai_device_id": str(bound.get("oai_device_id") or engine._device_id or ""),
                "auth_proxy_url": str(bound.get("auth_proxy_url") or active_proxy or ""),
                "session": bound.get("session") or result.metadata.get("session") or {},
            }
        )
        if isinstance(mailbox_extra.get("provider_account"), dict):
            result.metadata["provider_accounts"] = [mailbox_extra["provider_account"]]
        if isinstance(mailbox_extra.get("provider_resource"), dict):
            result.metadata["provider_resources"] = [mailbox_extra["provider_resource"]]
        self._log(f"双接码 Free 注册完成: {result.email}")
        return result

    def run(self, *, password: str) -> RegistrationResult:
        last_result = RegistrationResult(success=False, error_message="手机号协议注册失败")
        for attempt in range(1, self.max_phone_attempts + 1):
            if self.cancel_check():
                raise RuntimeError("任务已取消")
            phone_number = str(self.phone_callback() or "").strip()
            if not phone_number:
                raise RuntimeError("接码平台未返回手机号")
            phone_country = _phone_country_iso(phone_number)
            active_proxy = pin_711proxy_session(
                self.proxy_url,
                region=phone_country,
                session_id=self.proxy_session_id,
                session_minutes=180,
            ) or self.proxy_url
            active_proxy_country = infer_proxy_region(active_proxy) or self.proxy_country
            if active_proxy and active_proxy != self.proxy_url:
                self._log(
                    f"711Proxy 已固定注册路由: country={phone_country or 'unknown'} "
                    f"session={self.proxy_session_id} duration=180m"
                )
            route_mismatch = bool(
                active_proxy_country
                and phone_country
                and active_proxy_country != phone_country
            )
            if route_mismatch and not self._route_mismatch_logged:
                self._log(
                    f"⚠️ 代理出口国家 {active_proxy_country} 与手机号国家 {phone_country} 不一致；"
                    "若持续出现 account_creation_failed，请选择与代理出口一致的号码国家"
                )
                self._route_mismatch_logged = True
            self._log(f"手机号协议注册: 第 {attempt}/{self.max_phone_attempts} 个号码")
            last_result = self._run_number(phone_number, password, active_proxy)
            if last_result.success:
                return last_result
            rejection_message = str(last_result.error_message or "").lower()
            similar_number_risk = (
                "suspicious behavior from phone numbers" in rejection_message
                or "phone numbers similar to yours" in rejection_message
            )
            if similar_number_risk:
                self._similar_number_rejections += 1
                if self._similar_number_rejections >= 3:
                    self._log(
                        "当前国家连续 3 个 Provider/号码均被判相似号段风险，"
                        "已释放当前号码并停止继续消耗"
                    )
                    self._reset_number(last_result.error_message)
                    last_result.error_code = "phone_country_pool_rejected"
                    last_result.error_message = (
                        f"手机号国家 {phone_country or 'unknown'} 的当前号码池连续被注册服务拒绝，"
                        "请切换其他国家或号码档位"
                    )
                    break
            else:
                self._similar_number_rejections = 0
            account_creation_rejected = (
                last_result.error_code == "phone_number_rejected"
                and "failed to create account" in str(last_result.error_message or "").lower()
            )
            if route_mismatch and account_creation_rejected:
                self._route_mismatch_failures += 1
                if self._route_mismatch_failures >= 2:
                    self._log(
                        "代理与号码国家不一致且连续 2 个号码在账户创建阶段被拒绝，"
                        "已触发熔断并停止继续消耗号码"
                    )
                    self._reset_number(last_result.error_message)
                    last_result.error_code = "phone_proxy_country_mismatch"
                    last_result.error_message = (
                        f"代理出口国家 {active_proxy_country} 与手机号国家 {phone_country} 不一致，"
                        "请更换匹配国家的代理或号码"
                    )
                    break
            elif not route_mismatch:
                self._route_mismatch_failures = 0
            number_rejected = last_result.error_code in {
                "phone_number_in_use",
                "phone_number_rejected",
                "phone_otp_timeout",
                "phone_otp_validation_failed",
                "phone_flow_state_invalid",
                "password_registration_failed",
            }
            if number_rejected:
                action = "释放后换号" if attempt < self.max_phone_attempts else "释放并结束本账号"
                self._log(f"当前号码不可用，{action}: {last_result.error_code}")
                self._reset_number(last_result.error_message)
                if attempt < self.max_phone_attempts:
                    continue
            else:
                # Network/auth failures also release the activation, but they
                # do not count against the selected SMS provider's quality.
                callback = self.phone_callback
                if hasattr(callback, "cleanup"):
                    callback.cleanup()
                if hasattr(callback, "phase"):
                    callback.phase = "need_number"
                    callback.activation = None
                    callback.completed = False
            break
        return last_result


class ChatGPTProtocolEmailThenPhoneWorker(ChatGPTProtocolPhoneWorker):
    """Add phone verification to the email account created in this task.

    A fresh Codex authorization transaction identifies the existing account by
    email OTP. The worker stops after phone OTP validation, so Free registration
    never enters Workspace selection.
    """

    def __init__(
        self,
        *,
        email_service,
        phone_callback,
        proxy_url: str | None = None,
        log_fn: Callable[[str], None] = print,
        cancel_check=None,
        max_phone_attempts: int = 3,
        require_codex_refresh_token: bool = True,
        existing_account_id: str = "",
        existing_device_id: str = "",
        existing_auth_cookies=None,
        proxy_country: str = "",
    ):
        super().__init__(
            phone_callback=phone_callback,
            proxy_url=proxy_url,
            log_fn=log_fn,
            cancel_check=cancel_check,
            max_phone_attempts=max_phone_attempts,
            bind_email_after_registration=False,
            proxy_country=proxy_country,
        )
        self.email_service = email_service
        self.require_codex_refresh_token = bool(require_codex_refresh_token)
        self.existing_account_id = str(existing_account_id or "").strip()
        self.existing_device_id = str(existing_device_id or "").strip()
        self.existing_auth_cookies = _parse_cookie_records(existing_auth_cookies)
        self._auth_generation = 0

    @staticmethod
    def _seed_device_id(engine: RegistrationEngine, device_id: str) -> None:
        stable_id = str(device_id or "").strip()
        if not stable_id:
            return
        for domain in (
            "chatgpt.com",
            ".chatgpt.com",
            "auth.openai.com",
            ".auth.openai.com",
        ):
            try:
                engine.session.cookies.set(
                    "oai-did",
                    stable_id,
                    domain=domain,
                    path="/",
                )
            except Exception:
                pass
        engine._device_id = stable_id

    def _restore_auth_context(self, engine: RegistrationEngine) -> None:
        """Carry the account's real cookie jar into each follow-up OAuth session."""
        restored = 0
        for item in self.existing_auth_cookies:
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name or not value:
                continue
            path = str(item.get("path") or "/")
            domain = str(item.get("domain") or "").strip()
            target_domains = [domain] if domain else ["chatgpt.com", "auth.openai.com"]
            for target_domain in target_domains:
                try:
                    engine.session.cookies.set(
                        name,
                        value,
                        domain=target_domain,
                        path=path,
                    )
                    restored += 1
                except Exception:
                    pass
        if self.existing_device_id:
            self._seed_device_id(engine, self.existing_device_id)
        if restored:
            self._log(
                "已继承基础注册会话 Cookie: "
                f"records={len(self.existing_auth_cookies)} generation={self._auth_generation}"
            )

    def _capture_auth_context(self, engine: RegistrationEngine) -> None:
        records = _export_session_cookies(getattr(engine, "session", None))
        if records:
            self.existing_auth_cookies = records
        device_id = str(getattr(engine, "_device_id", "") or "").strip()
        if device_id:
            self.existing_device_id = device_id

    def _session_context_fields(self, engine: RegistrationEngine) -> dict:
        self._capture_auth_context(engine)
        fields: dict[str, object] = {}
        if self.existing_device_id:
            fields["oai_device_id"] = self.existing_device_id
        if self.existing_auth_cookies:
            fields["auth_cookies"] = list(self.existing_auth_cookies)
            fields["cookies"] = _cookies_to_header(self.existing_auth_cookies)
        if self._auth_generation > 0:
            fields["auth_generation"] = self._auth_generation
        return fields

    @staticmethod
    def _page_state(response) -> tuple[str, str, dict]:
        try:
            data = response.json() or {}
        except Exception:
            data = {}
        page = data.get("page") if isinstance(data, dict) else {}
        page = page if isinstance(page, dict) else {}
        payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
        continue_url = str(
            data.get("continue_url")
            or payload.get("continue_url")
            or payload.get("url")
            or ""
        ).strip()
        return str(page.get("type") or "").strip(), continue_url, payload

    @staticmethod
    def _sentinel_header(engine: RegistrationEngine, flow: str) -> dict[str, str]:
        if not engine._device_id:
            return {}
        sentinel = engine._check_sentinel(engine._device_id, flow=flow)
        if not sentinel:
            return {}
        return {
            "openai-sentinel-token": json.dumps(
                {
                    "p": sentinel.p,
                    "t": sentinel.t,
                    "c": sentinel.c,
                    "id": engine._device_id,
                    "flow": sentinel.flow,
                },
                separators=(",", ":"),
            )
        }

    @staticmethod
    def _workspace_identifiers(workspace: dict) -> set[str]:
        """Return account/workspace identifiers without trusting display labels."""
        identifiers: set[str] = set()
        if not isinstance(workspace, dict):
            return identifiers
        for key in (
            "id",
            "workspace_id",
            "workspaceId",
            "account_id",
            "accountId",
            "user_id",
            "userId",
        ):
            value = workspace.get(key)
            if value is not None and str(value).strip():
                identifiers.add(str(value).strip())
        for key in ("workspace", "account", "user"):
            nested = workspace.get(key)
            if isinstance(nested, dict):
                identifiers.update(
                    ChatGPTProtocolEmailThenPhoneWorker._workspace_identifiers(nested)
                )
        return identifiers

    @staticmethod
    def _workspace_id(workspace: dict) -> str:
        if not isinstance(workspace, dict):
            return ""
        return str(
            workspace.get("id")
            or workspace.get("workspace_id")
            or workspace.get("workspaceId")
            or ""
        ).strip()

    def _authorization_workspaces(
        self,
        engine: RegistrationEngine,
        referer: str,
    ) -> list[dict]:
        """Read the server-side chooser candidates for the current OAuth session."""
        session_cookie = _cookie_value(engine.session, "oai-client-auth-session")
        session_data = _decode_client_auth_session(session_cookie)
        workspaces = [
            dict(item)
            for item in list(session_data.get("workspaces") or [])
            if isinstance(item, dict) and self._workspace_id(item)
        ]

        # The cookie can lag behind the server-side transaction. Read the dump
        # whenever it is empty or cannot uniquely identify the persisted account.
        exact_cookie_match = bool(
            self.existing_account_id
            and any(
                self.existing_account_id in self._workspace_identifiers(item)
                for item in workspaces
            )
        )
        if not workspaces or (len(workspaces) > 1 and not exact_cookie_match):
            dump_response = engine.session.get(
                f"{OPENAI_AUTH}/api/accounts/client_auth_session_dump",
                headers={"accept": "application/json", "referer": referer},
                timeout=20,
            )
            self._log(f"OAuth 账号选择会话读取状态: {dump_response.status_code}")
            if dump_response.status_code < 400:
                try:
                    dump_data = dump_response.json() or {}
                except Exception:
                    dump_data = {}
                dump_payload = (
                    dump_data.get("data")
                    if isinstance(dump_data.get("data"), dict)
                    else {}
                )
                dumped = dump_data.get("workspaces") or dump_payload.get("workspaces") or []
                parsed_dump = [
                    dict(item)
                    for item in list(dumped)
                    if isinstance(item, dict) and self._workspace_id(item)
                ]
                if parsed_dump:
                    workspaces = parsed_dump

        deduplicated: list[dict] = []
        seen: set[str] = set()
        for workspace in workspaces:
            workspace_id = self._workspace_id(workspace)
            if not workspace_id or workspace_id in seen:
                continue
            seen.add(workspace_id)
            deduplicated.append(workspace)
        return deduplicated

    def _select_authorization_workspace(
        self,
        engine: RegistrationEngine,
        referer: str,
    ) -> tuple[str, str, dict]:
        """Select only the current persisted account on an OAuth chooser page.

        A single candidate is safe because the restored cookie jar belongs to
        the account being completed. With multiple candidates we require an
        exact account-id match so concurrent registrations cannot cross-link.
        """
        workspaces = self._authorization_workspaces(engine, referer)
        if not workspaces:
            raise RuntimeError("OAuth 账号选择页没有可选择的账号")

        selected: dict | None = None
        selection_mode = "single_session_candidate"
        if self.existing_account_id:
            matches = [
                item
                for item in workspaces
                if self.existing_account_id in self._workspace_identifiers(item)
            ]
            if len(matches) == 1:
                selected = matches[0]
                selection_mode = "exact_account_id"
            elif len(matches) > 1:
                raise RuntimeError("OAuth 账号选择页出现重复的当前账号，已停止以避免串号")
        if selected is None:
            if len(workspaces) != 1:
                raise RuntimeError(
                    "OAuth 账号选择页包含多个账号，但未找到当前账号，已停止以避免串号"
                )
            selected = workspaces[0]

        workspace_id = self._workspace_id(selected)
        select_response = engine.session.post(
            OPENAI_API_ENDPOINTS["select_workspace"],
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": OPENAI_AUTH,
                "referer": referer,
            },
            data=json.dumps({"workspace_id": workspace_id}),
            allow_redirects=False,
            timeout=20,
        )
        self._log(
            "OAuth 当前账号自动选择状态: "
            f"{select_response.status_code} mode={selection_mode} candidates={len(workspaces)}"
        )
        if select_response.status_code >= 400:
            code, message = _response_error(select_response)
            raise RuntimeError(
                message
                or code
                or f"OAuth 当前账号选择失败: HTTP {select_response.status_code}"
            )

        try:
            select_data = select_response.json() or {}
        except Exception:
            select_data = {}
        page_type, continue_url, page_payload = self._page_state(select_response)
        headers = getattr(select_response, "headers", {}) or {}
        location = str(headers.get("Location") or headers.get("location") or "").strip()
        next_url = location or continue_url or _find_callback_url(select_data)
        if not next_url and page_type == "add_phone":
            next_url = "/add-phone"
        if not next_url:
            raise RuntimeError("OAuth 当前账号选择成功，但响应缺少后续地址")
        return urljoin(referer, next_url), page_type, page_payload

    def _continue_account_chooser(
        self,
        engine: RegistrationEngine,
        chooser_url: str,
    ) -> tuple[str, str, dict]:
        """Select the account and resolve one server redirect into a flow page."""
        next_url, page_type, page_payload = self._select_authorization_workspace(
            engine,
            chooser_url,
        )
        if (
            page_type == "add_phone"
            or "/add-phone" in next_url
            or "code=" in next_url
            or "consent" in next_url
        ):
            return next_url, page_type, page_payload

        response = engine.session.get(next_url, allow_redirects=True, timeout=30)
        final_url = str(getattr(response, "url", "") or next_url)
        resolved_type, resolved_url, resolved_payload = self._page_state(response)
        if resolved_url:
            final_url = urljoin(final_url, resolved_url)
        return final_url, resolved_type, resolved_payload

    def _open_phone_challenge(self, *, email: str, password: str) -> tuple[RegistrationEngine, str, dict]:
        self._auth_generation += 1
        engine = RegistrationEngine(
            email_service=self.email_service,
            proxy_url=self.proxy_url,
            callback_logger=self.log_fn,
        )
        engine.email = email
        engine.password = password
        if not engine._init_session():
            raise RuntimeError("邮箱账号手机号验证会话初始化失败")
        self._restore_auth_context(engine)

        oauth_start = generate_oauth_url(
            client_id=CODEX_CLIENT_ID,
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
        )
        self._codex_oauth_start = oauth_start
        self._log("邮箱 Free 账号已创建，通过 Codex PKCE OAuth 进入手机号验证...")
        response = engine.session.get(oauth_start.auth_url, allow_redirects=True, timeout=30)
        final_url = str(getattr(response, "url", "") or oauth_start.auth_url)
        engine._authorize_final_url = final_url
        session_device_id = _cookie_value(engine.session, "oai-did")
        if not self.existing_device_id:
            self.existing_device_id = session_device_id
        if self.existing_device_id:
            # Preserve one account-scoped device identity across the base
            # registration, phone OAuth, and every authorization rebuild.
            self._seed_device_id(engine, self.existing_device_id)
        else:
            engine._device_id = session_device_id
        if not engine._device_id:
            raise RuntimeError("手机号验证授权未建立 Device ID")
        self._log(
            "手机号授权会话档案已复用: "
            f"device={'stable' if self.existing_device_id else 'session'} "
            f"generation={self._auth_generation} "
            f"proxy_country={self.proxy_country or 'unknown'}"
        )

        page_type = ""
        page_payload: dict = {}
        if "/choose-an-account" in final_url:
            self._log("手机号授权进入账号选择页，正在续接当前注册账号")
            final_url, page_type, page_payload = self._continue_account_chooser(
                engine,
                final_url,
            )
            engine._authorize_final_url = final_url
            if page_type:
                engine._otp_page_type = page_type
            if page_type == "add_phone" or "/add-phone" in final_url:
                engine._otp_continue_url = final_url
                return engine, final_url, page_payload
            if "code=" in final_url or "consent" in final_url:
                return engine, "", {"already_verified": True, **page_payload}
            if "/choose-an-account" in final_url:
                raise RuntimeError("OAuth 当前账号选择后仍停留在账号选择页")

        if "/add-phone" in final_url:
            return engine, final_url, {}
        if "/log-in" not in final_url or "/log-in/password" in final_url:
            if "code=" in final_url or "consent" in final_url:
                return engine, "", {"already_verified": True}
            raise RuntimeError(f"手机号验证授权落点异常: {final_url[:160]}")

        headers = {
            "origin": OPENAI_AUTH,
            "referer": final_url,
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            **_generate_datadog_trace_headers(),
            **self._sentinel_header(engine, "authorize_continue"),
        }
        headers["oai-device-id"] = engine._device_id
        response = engine.session.post(
            OPENAI_API_ENDPOINTS["signup"],
            headers=headers,
            data=json.dumps(
                {
                    "username": {"value": email, "kind": "email"},
                    "screen_hint": "login",
                }
            ),
            timeout=20,
        )
        self._log(f"手机号验证提交邮箱状态: {response.status_code}")
        if response.status_code != 200:
            code, message = _response_error(response)
            raise RuntimeError(message or code or f"提交邮箱失败: HTTP {response.status_code}")

        page_type, continue_url, page_payload = self._page_state(response)
        self._log(f"手机号验证提交邮箱后页面: {page_type or 'unknown'}")
        if page_type not in {"email_otp_verification", "email_otp_send"}:
            if page_type == "add_phone" or "/add-phone" in continue_url:
                return engine, urljoin(OPENAI_AUTH, continue_url or "/add-phone"), page_payload
            raise RuntimeError(f"邮箱账号未进入邮箱 OTP 登录: {page_type or continue_url or 'unknown'}")

        begin_wait = getattr(self.email_service, "begin_new_otp_wait", None)
        if callable(begin_wait):
            begin_wait()
        email_otp_url = urljoin(OPENAI_AUTH, continue_url or "/email-verification")
        engine._email_otp_continue_url = email_otp_url
        send_response = engine.session.get(
            OPENAI_API_ENDPOINTS["send_otp"],
            headers={"referer": email_otp_url, "accept": "application/json"},
            timeout=20,
        )
        self._log(f"手机号验证邮箱验证码发送状态: {send_response.status_code}")
        engine._otp_sent_at = time.time()
        code = engine._get_verification_code()
        if not code:
            raise RuntimeError("手机号验证登录未获取到邮箱验证码")
        if not engine._validate_verification_code(code):
            raise RuntimeError(engine._step_error_message or "手机号验证登录邮箱验证码校验失败")

        page_type = str(engine._otp_page_type or "")
        continue_url = str(engine._otp_continue_url or "")
        otp_page = (
            engine._otp_response_data.get("page")
            if isinstance(getattr(engine, "_otp_response_data", None), dict)
            else {}
        )
        otp_payload = (
            otp_page.get("payload")
            if isinstance(otp_page, dict) and isinstance(otp_page.get("payload"), dict)
            else {}
        )
        if page_type != "add_phone" and "/add-phone" not in continue_url:
            if page_type in {"consent", "sign_in_with_chatgpt_codex_consent"} or "consent" in continue_url:
                return engine, "", {"already_verified": True}
            raise RuntimeError(f"邮箱 OTP 后未进入手机号验证: {page_type or continue_url or 'unknown'}")
        return engine, urljoin(OPENAI_AUTH, continue_url or "/add-phone"), dict(otp_payload)

    def _complete_codex_oauth(self, engine: RegistrationEngine) -> dict:
        """Exchange the post-phone Codex callback for CLI credentials."""
        oauth_start = self._codex_oauth_start
        if oauth_start is None:
            return {}

        post_phone_url = str(
            getattr(engine, "_otp_continue_url", "")
            or getattr(engine, "_authorize_final_url", "")
            or ""
        ).strip()
        current_url = urljoin(OPENAI_AUTH, post_phone_url) if post_phone_url else ""
        if not current_url or "phone-verification" in current_url:
            current_url = str(oauth_start.auth_url or "").strip()
        self._log(
            "Codex OAuth 从手机号验证后的授权状态续接: "
            f"page={str(getattr(engine, '_otp_page_type', '') or 'unknown')} "
            f"url={urlsplit(current_url).path or '/'}"
        )
        redirect_statuses = {301, 302, 303, 307, 308}

        def _exchange(callback_url: str) -> dict:
            query = parse_qs(urlsplit(callback_url).query, keep_blank_values=True)
            if not query.get("code") or not query.get("state"):
                return {}
            if str(query["state"][0]) != str(oauth_start.state):
                raise RuntimeError("Codex OAuth callback state 不匹配")
            raw = submit_callback_url(
                callback_url=callback_url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                redirect_uri=CODEX_REDIRECT_URI,
                client_id=CODEX_CLIENT_ID,
                proxy_url=self.proxy_url,
            )
            raw_data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            normalized = normalize_oauth_token_response(raw_data)
            token_data = {**raw_data, **normalized}
            if not str(token_data.get("refresh_token") or "").strip():
                raise RuntimeError("Codex OAuth 未返回 refresh_token")
            return token_data

        for hop in range(12):
            try:
                query = parse_qs(urlsplit(current_url).query, keep_blank_values=True)
                if query.get("code") and query.get("state"):
                    tokens = _exchange(current_url)
                    self._log("Codex OAuth callback 已换取 refresh_token")
                    return tokens

                response = engine.session.get(
                    current_url,
                    headers={
                        "referer": f"{OPENAI_AUTH}/phone-verification",
                        "accept": "text/html,application/json,*/*",
                    },
                    allow_redirects=False,
                    timeout=25,
                )
            except Exception as exc:
                self._log(
                    f"Codex OAuth callback 获取失败: {type(exc).__name__}: {exc}",
                    "warning",
                )
                return {}

            if response.status_code in redirect_statuses:
                location = str(
                    response.headers.get("Location")
                    or response.headers.get("location")
                    or ""
                ).strip()
                if not location:
                    break
                current_url = urljoin(str(getattr(response, "url", "") or current_url), location)
                parsed = urlsplit(current_url)
                self._log(f"Codex OAuth 跳转[{hop + 1}]: {parsed.hostname}{parsed.path}")
                continue

            try:
                data = response.json() or {}
            except Exception:
                data = {}
            body_callback = _find_callback_url(data)
            if body_callback:
                tokens = _exchange(body_callback)
                self._log("Codex OAuth callback 已换取 refresh_token")
                return tokens
            page = data.get("page") if isinstance(data.get("page"), dict) else {}
            payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
            next_url = str(
                data.get("continue_url")
                or payload.get("continue_url")
                or payload.get("url")
                or ""
            ).strip()
            if next_url and str(data.get("method") or payload.get("method") or "GET").upper() == "GET":
                current_url = urljoin(str(getattr(response, "url", "") or current_url), next_url)
                continue

            consent_url = str(getattr(response, "url", "") or current_url)
            if response.status_code == 200 and (
                "consent" in consent_url
                or str(page.get("type") or "") in {"consent", "sign_in_with_chatgpt_codex_consent"}
            ):
                current_url, _selected_type, _selected_payload = (
                    self._select_authorization_workspace(engine, consent_url)
                )
                continue
            break

        self._log("Codex OAuth 未找到有效 callback", "warning")
        return {}

    def _handle_already_verified_phone_account(self, engine: RegistrationEngine) -> dict:
        """Finish the Codex exchange when the account already has phone access."""
        self._log("该邮箱账号已满足手机号验证要求，跳过租号并继续获取 RT")
        codex_tokens = self._complete_codex_oauth(engine)
        require_rt = bool(getattr(self, "require_codex_refresh_token", True))
        if not codex_tokens or not str((codex_tokens or {}).get("refresh_token") or "").strip():
            if require_rt:
                raise RuntimeError(
                    "CODEX_RT_MISSING: 手机号已验证，但 Codex OAuth 未返回 refresh_token"
                )
            self._log("手机号已验证，Codex OAuth 本次未返回 RT", "warning")
            result = {"ok": True, "already_verified": True, "phone_number": ""}
            result.update(self._session_context_fields(engine))
            return result
        result = {
            "ok": True,
            "already_verified": True,
            "phone_number": "",
            "access_token": str(codex_tokens.get("access_token") or ""),
            "refresh_token": str(codex_tokens.get("refresh_token") or ""),
            "id_token": str(codex_tokens.get("id_token") or ""),
        }
        result.update(self._session_context_fields(engine))
        return result

    def _rebuild_phone_challenge(
        self,
        *,
        email: str,
        password: str,
        reason: str,
        previous_engine: RegistrationEngine | None = None,
    ) -> tuple[RegistrationEngine, str, dict]:
        """Start a fresh phone authorization transaction for the same account.

        Once ``add-phone/send`` has advanced to phone/contact verification, the
        server-side auth state is consumed. A new SMS number must therefore use
        a new Codex PKCE transaction instead of posting to the stale session.
        """
        self._log(
            "手机号验证会话已消耗，重建同账号授权会话后再换号: "
            f"reason={reason}"
        )
        if self.cancel_check():
            raise RuntimeError("任务已取消")
        if previous_engine is not None:
            self._capture_auth_context(previous_engine)
        try:
            return self._open_phone_challenge(email=email, password=password)
        except Exception as exc:
            raise RuntimeError(f"手机号验证会话重建失败: {exc}") from exc

    def acquire_codex_credentials(self, *, email: str, password: str) -> dict:
        """Complete Codex PKCE for an existing email account without renting a phone."""
        engine, add_phone_url, page_payload = self._open_phone_challenge(
            email=email,
            password=password,
        )
        already_verified = bool(page_payload.get("already_verified"))
        if add_phone_url and not already_verified:
            return {
                "ok": False,
                "error_code": "phone_required",
                "error": "Codex OAuth requires phone verification",
            }
        tokens = self._complete_codex_oauth(engine)
        if not tokens:
            return {
                "ok": False,
                "error_code": "codex_callback_missing",
                "error": "Codex OAuth did not return a callback",
            }
        required = ("access_token", "id_token", "refresh_token")
        missing = [key for key in required if not str(tokens.get(key) or "").strip()]
        if missing:
            return {
                "ok": False,
                "error_code": "codex_tokens_incomplete",
                "error": "Codex OAuth token response missing: " + ", ".join(missing),
            }
        tokens.update(self._session_context_fields(engine))
        return {"ok": True, "data": tokens}

    def run_for_account(self, *, email: str, password: str) -> dict:
        if not email or "@" not in email:
            raise RuntimeError("邮箱注册结果缺少有效邮箱")
        if self.cancel_check():
            raise RuntimeError("任务已取消")
        engine, add_phone_url, page_payload = self._open_phone_challenge(
            email=email,
            password=password,
        )
        if page_payload.get("already_verified"):
            return self._handle_already_verified_phone_account(engine)

        last_error = "手机号验证失败"
        try:
            for attempt in range(1, self.max_phone_attempts + 1):
                if self.cancel_check():
                    raise RuntimeError("任务已取消")
                phone_number = str(self.phone_callback() or "").strip()
                if not phone_number:
                    raise RuntimeError("接码平台未返回手机号")
                phone_country = _phone_country_iso(phone_number)
                route_mismatch = bool(
                    self.proxy_country
                    and phone_country
                    and self.proxy_country != phone_country
                )
                if route_mismatch and not self._route_mismatch_logged:
                    self._log(
                        "会话一致性警告: "
                        f"注册代理国家={self.proxy_country}，手机号国家={phone_country}；"
                        "当前账号不会静默切换代理，请配置一致的代理与号码国家",
                        "warning",
                    )
                    self._route_mismatch_logged = True
                self._log(f"邮箱账号手机号验证: 第 {attempt}/{self.max_phone_attempts} 个号码")
                engine._password_continue_url = add_phone_url
                engine._password_next_payload = dict(page_payload)
                is_add_phone_step = (
                    str(engine._otp_page_type or "") == "add_phone"
                    or "add-phone" in str(add_phone_url or "")
                )
                if is_add_phone_step:
                    send_ok = self._send_add_phone_number(engine, phone_number, add_phone_url)
                else:
                    send_ok = self._send_phone_otp(engine, phone_number)
                if not send_ok:
                    error_code = str(getattr(engine, "_step_error_code", "") or "")
                    last_error = engine._step_error_message or "手机号验证码发送失败"
                    self._reset_number(last_error)
                    if _is_account_deactivated(error_code, last_error):
                        self._log(
                            "当前邮箱账号已被目标服务停用，已释放号码并终止该账号流程；不再换号重试",
                            "warning",
                        )
                        raise RuntimeError(
                            "ACCOUNT_DEACTIVATED: "
                            f"{last_error or '账号已被删除或停用'}"
                        )
                    if _is_phone_risk_rejection(error_code, last_error):
                        self._log(
                            "远端已将当前手机号号段/会话判为风险，停止在同一账号上继续换号",
                            "warning",
                        )
                        raise RuntimeError(
                            "PHONE_RISK_REJECTED: "
                            f"{last_error or error_code or 'phone fraud guard'}"
                        )
                    if error_code == "phone_authorization_invalid":
                        if attempt < self.max_phone_attempts:
                            engine, add_phone_url, page_payload = self._rebuild_phone_challenge(
                                email=email,
                                password=password,
                                reason=error_code,
                                previous_engine=engine,
                            )
                            if page_payload.get("already_verified"):
                                return self._handle_already_verified_phone_account(engine)
                            continue
                        raise RuntimeError(last_error)
                    if _is_phone_account_rate_limited(error_code, last_error):
                        # Same account is hard-limited; keep burning numbers is useless.
                        raise RuntimeError(
                            "PHONE_ACCOUNT_RATE_LIMITED: "
                            f"{last_error or 'phone verification rate limited'}"
                        )
                    if attempt < self.max_phone_attempts:
                        # Brief pause before next rental to reduce provider churn.
                        time.sleep(1.5)
                        continue
                    break

                if hasattr(self.phone_callback, "set_resend_callback"):
                    self.phone_callback.set_resend_callback(
                        lambda: self._send_phone_otp(engine, phone_number, resend=True)
                    )
                if hasattr(self.phone_callback, "mark_send_succeeded"):
                    self.phone_callback.mark_send_succeeded()
                sms_code = str(self.phone_callback() or "").strip()
                if not sms_code:
                    last_error = "等待短信验证码超时"
                    self._reset_number(last_error)
                    if attempt < self.max_phone_attempts:
                        engine, add_phone_url, page_payload = self._rebuild_phone_challenge(
                            email=email,
                            password=password,
                            reason="phone_otp_timeout",
                            previous_engine=engine,
                        )
                        if page_payload.get("already_verified"):
                            return self._handle_already_verified_phone_account(engine)
                        continue
                    break
                if not self._validate_phone_otp(engine, sms_code):
                    error_code = str(getattr(engine, "_step_error_code", "") or "")
                    last_error = engine._step_error_message or "短信验证码校验失败"
                    if hasattr(self.phone_callback, "mark_code_failed"):
                        self.phone_callback.mark_code_failed(last_error)
                    self._reset_number(last_error)
                    if _is_account_deactivated(error_code, last_error):
                        self._log(
                            "当前邮箱账号已被目标服务停用，已释放号码并终止该账号流程；不再换号重试",
                            "warning",
                        )
                        raise RuntimeError(
                            "ACCOUNT_DEACTIVATED: "
                            f"{last_error or '账号已被删除或停用'}"
                        )
                    if _is_phone_risk_rejection(error_code, last_error):
                        self._log(
                            "远端已将当前手机号号段/会话判为风险，停止在同一账号上继续换号",
                            "warning",
                        )
                        raise RuntimeError(
                            "PHONE_RISK_REJECTED: "
                            f"{last_error or error_code or 'phone fraud guard'}"
                        )
                    if _is_phone_account_rate_limited(error_code, last_error):
                        raise RuntimeError(
                            "PHONE_ACCOUNT_RATE_LIMITED: "
                            f"{last_error or 'phone verification rate limited'}"
                        )
                    if attempt < self.max_phone_attempts:
                        engine, add_phone_url, page_payload = self._rebuild_phone_challenge(
                            email=email,
                            password=password,
                            reason=error_code or "phone_otp_validation_failed",
                            previous_engine=engine,
                        )
                        if page_payload.get("already_verified"):
                            return self._handle_already_verified_phone_account(engine)
                        continue
                    break

                if hasattr(self.phone_callback, "report_success"):
                    self.phone_callback.report_success()
                codex_tokens = self._complete_codex_oauth(engine)
                require_rt = bool(getattr(self, "require_codex_refresh_token", True))
                if require_rt:
                    missing = [
                        key
                        for key in ("access_token", "id_token", "refresh_token")
                        if not str((codex_tokens or {}).get(key) or "").strip()
                    ]
                    if missing:
                        raise RuntimeError(
                            "CODEX_RT_MISSING: 手机号已通过，但 Codex OAuth 未返回完整凭据: "
                            + ", ".join(missing)
                        )
                elif self._codex_oauth_start is not None and not codex_tokens:
                    self._log(
                        "手机号验证已通过，但 Codex OAuth 未返回 refresh_token；保留当前 Free 账号，稍后可单独补取 RT",
                        "warning",
                    )
                self._log("邮箱 Free 账号手机号验证完成（含 Codex RT）")
                result_data = {
                    "ok": True,
                    "already_verified": False,
                    "phone_number": phone_number,
                    "page_type": str(engine._otp_page_type or ""),
                }
                result_data.update(self._session_context_fields(engine))
                if self.proxy_country:
                    if phone_country:
                        result_data["phone_country"] = phone_country
                    result_data["auth_proxy_country"] = self.proxy_country
                    result_data["route_country_consistent"] = not route_mismatch
                if codex_tokens:
                    result_data.update(
                        {
                            key: value
                            for key, value in {
                                "account_id": str(codex_tokens.get("account_id") or ""),
                                "access_token": str(codex_tokens.get("access_token") or ""),
                                "refresh_token": str(codex_tokens.get("refresh_token") or ""),
                                "id_token": str(codex_tokens.get("id_token") or ""),
                                "expires_at": str(codex_tokens.get("expired") or codex_tokens.get("expires_at") or ""),
                                "last_refresh": str(codex_tokens.get("last_refresh") or ""),
                            }.items()
                            if value
                        }
                    )
                return result_data
            raise RuntimeError(last_error)
        finally:
            # Always return unused/failed rentals to the SMS provider.
            try:
                if hasattr(self.phone_callback, "cleanup"):
                    self.phone_callback.cleanup()
            except Exception:
                pass
