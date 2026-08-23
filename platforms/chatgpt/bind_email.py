from __future__ import annotations

import json
import secrets
import time
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from core.base_mailbox import BaseMailbox, MailboxAccount
from core.proxy_utils import infer_proxy_region, pin_711proxy_session
from platforms.chatgpt.constants import (
    CHATGPT_APP,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
)
from platforms.chatgpt.oauth import generate_oauth_url
from platforms.chatgpt.register import (
    RegistrationEngine,
    _extract_chatgpt_account_id,
    _generate_datadog_trace_headers,
    _response_error,
)


class _PhoneIdentityService:
    service_type = type("ST", (), {"value": "phone"})()


def _export_session_cookies(session) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    jar = getattr(getattr(session, "cookies", None), "jar", None)
    if jar is not None:
        for cookie in jar:
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if name and value:
                records.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": str(getattr(cookie, "domain", "") or ""),
                        "path": str(getattr(cookie, "path", "") or "/"),
                    }
                )
    return records


def _cookie_header(records: list[dict[str, str]]) -> str:
    values = {
        str(item.get("name") or ""): str(item.get("value") or "")
        for item in records
        if str(item.get("name") or "") and str(item.get("value") or "")
    }
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
    records = []
    for part in raw.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and name and cookie_value:
            records.append({"name": name, "value": cookie_value, "domain": "", "path": "/"})
    return records


class ChatGPTBindEmailError(RuntimeError):
    """A remote add-email state transition failed."""


_TRANSIENT_PROXY_MARKERS = (
    "curl: (5)",
    "curl: (7)",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (55)",
    "curl: (56)",
    "curl: (92)",
    "proxy connect",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
)


def _is_transient_proxy_error(error: Exception) -> bool:
    message = str(error or "").lower()
    return any(marker in message for marker in _TRANSIENT_PROXY_MARKERS)


def _page_state(response) -> tuple[str, str, dict]:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    page = payload.get("page") if isinstance(payload, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    return (
        str(page.get("type") or "").strip(),
        str(
            payload.get("continue_url")
            or page_payload.get("continue_url")
            or page_payload.get("url")
            or ""
        ).strip(),
        payload,
    )


class ChatGPTProtocolBindEmailWorker:
    """Bind a mailbox to an existing phone-first ChatGPT account.

    The worker owns only the remote state machine.  Mailbox allocation is
    supplied by the platform action and account persistence remains the
    responsibility of ``PlatformRuntime``.
    """

    def __init__(
        self,
        *,
        phone_number: str,
        password: str,
        mailbox: BaseMailbox,
        mailbox_account: MailboxAccount | None = None,
        proxy_url: str | None = None,
        otp_timeout_seconds: int = 300,
        log_fn: Callable[[str], None] = print,
        cancel_check: Callable[[], bool] | None = None,
        existing_access_token: str = "",
        existing_session_token: str = "",
        existing_auth_cookies=None,
        existing_device_id: str = "",
    ):
        self.phone_number = str(phone_number or "").strip()
        self.password = str(password or "")
        self.mailbox = mailbox
        self.mailbox_account = mailbox_account
        self.proxy_url = str(proxy_url or "").strip() or None
        self.otp_timeout_seconds = min(max(int(otp_timeout_seconds or 300), 60), 600)
        self.log_fn = log_fn
        self.cancel_check = cancel_check if callable(cancel_check) else (lambda: False)
        self.existing_access_token = str(existing_access_token or "")
        self.existing_session_token = str(existing_session_token or "")
        self.existing_auth_cookies = _parse_cookie_records(existing_auth_cookies)
        self.existing_device_id = str(existing_device_id or "").strip()

    def _log(self, message: str) -> None:
        self.log_fn(message)

    def _raise_if_cancelled(self) -> None:
        if self.cancel_check():
            raise RuntimeError("任务已取消")

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

    def _submit_phone_password(self, engine: RegistrationEngine) -> tuple[str, str]:
        self._raise_if_cancelled()
        page_url = str(engine._authorize_final_url or f"{OPENAI_AUTH}/log-in/password")
        engine.session.get(
            page_url,
            headers={"referer": f"{OPENAI_AUTH}/log-in", "accept": "text/html,*/*"},
            timeout=20,
        )
        headers = {
            "origin": OPENAI_AUTH,
            "referer": page_url,
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            **_generate_datadog_trace_headers(),
            **self._sentinel_header(engine, "login_password"),
        }
        if engine._device_id:
            headers["oai-device-id"] = engine._device_id
        response = engine.session.post(
            OPENAI_API_ENDPOINTS["verify_password"],
            headers=headers,
            data=json.dumps({"password": self.password}),
            timeout=20,
        )
        self._log(f"手机号账号密码登录状态: {response.status_code}")
        if response.status_code != 200:
            code, message = _response_error(response)
            self._log(
                f"手机号账号密码登录失败: code={code or 'unknown'} "
                f"message={message or 'unknown'}"
            )
            raise ChatGPTBindEmailError(message or code or f"手机号账号密码登录失败: HTTP {response.status_code}")
        page_type, continue_url, _ = _page_state(response)
        self._log(f"手机号账号登录后页面: {page_type or 'unknown'}")
        return page_type, continue_url

    def _submit_phone_identifier(self, engine: RegistrationEngine) -> tuple[str, str]:
        self._raise_if_cancelled()
        page_url = str(engine._authorize_final_url or f"{OPENAI_AUTH}/log-in")
        headers = {
            "origin": OPENAI_AUTH,
            "referer": page_url,
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            **_generate_datadog_trace_headers(),
            **self._sentinel_header(engine, "authorize_continue"),
        }
        if engine._device_id:
            headers["oai-device-id"] = engine._device_id
        response = engine.session.post(
            OPENAI_API_ENDPOINTS["signup"],
            headers=headers,
            data=json.dumps(
                {
                    "username": {"value": self.phone_number, "kind": "phone_number"},
                    "screen_hint": "login",
                }
            ),
            timeout=20,
        )
        self._log(f"Codex OAuth 提交手机号状态: {response.status_code}")
        if response.status_code != 200:
            code, message = _response_error(response)
            self._log(
                f"Codex OAuth 提交手机号失败: code={code or 'unknown'} "
                f"message={message or 'unknown'}"
            )
            raise ChatGPTBindEmailError(
                message or code or f"Codex OAuth 提交手机号失败: HTTP {response.status_code}"
            )
        page_type, continue_url, _ = _page_state(response)
        self._log(f"Codex OAuth 提交手机号后页面: {page_type or 'unknown'}")
        return page_type, continue_url

    def _submit_add_email(self, engine: RegistrationEngine, page_url: str) -> tuple[str, str]:
        self._raise_if_cancelled()
        add_email_url = page_url or f"{OPENAI_AUTH}/add-email"
        engine.session.get(
            add_email_url,
            headers={"referer": f"{OPENAI_AUTH}/log-in/password", "accept": "text/html,*/*"},
            timeout=20,
        )
        headers = {
            "origin": OPENAI_AUTH,
            "referer": add_email_url,
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            **_generate_datadog_trace_headers(),
        }
        if engine._device_id:
            headers["oai-device-id"] = engine._device_id
        response = engine.session.post(
            OPENAI_API_ENDPOINTS["add_email_send"],
            headers=headers,
            data=json.dumps({"email": self.mailbox_account.email}),
            timeout=20,
        )
        self._log(f"提交绑定邮箱状态: {response.status_code}")
        if response.status_code != 200:
            code, message = _response_error(response)
            normalized = f"{code} {message}".lower()
            if "email_in_use" in normalized or "already" in normalized or "exists" in normalized:
                raise ChatGPTBindEmailError("该邮箱已绑定其他账号，请从邮箱池选择新邮箱")
            raise ChatGPTBindEmailError(message or code or f"提交绑定邮箱失败: HTTP {response.status_code}")
        page_type, continue_url, _ = _page_state(response)
        self._log(f"提交邮箱后页面: {page_type or 'unknown'}")
        if page_type not in {"email_otp_verification", "email_otp_send"}:
            raise ChatGPTBindEmailError(f"提交邮箱后未进入邮箱验证码流程: {page_type or 'unknown'}")
        return page_type, continue_url

    def _resolve_add_email_page(
        self,
        engine: RegistrationEngine,
        page_type: str,
        continue_url: str,
    ) -> str:
        """Resolve the post-login handoff before calling add-email/send.

        The auth service sometimes returns ``external_url`` with the actual
        target nested under ``page.payload.url``.  Follow that handoff first,
        then use the authenticated direct add-email route as a fallback.
        """
        if page_type == "add_email" or "/add-email" in str(continue_url or ""):
            return continue_url

        candidates = []
        if continue_url:
            candidates.append(continue_url)
        direct_add_email = f"{OPENAI_AUTH}/add-email"
        if direct_add_email not in candidates:
            candidates.append(direct_add_email)

        last_path = page_type or "unknown"
        for target in candidates:
            self._raise_if_cancelled()
            try:
                response = engine.session.get(
                    urljoin(OPENAI_AUTH, target),
                    headers={
                        "referer": f"{OPENAI_AUTH}/log-in/password",
                        "accept": "text/html,application/json,*/*",
                    },
                    allow_redirects=True,
                    timeout=25,
                )
            except Exception as exc:
                self._log(f"添加邮箱页面跳转失败: {exc}")
                continue

            final_url = str(getattr(response, "url", "") or target)
            resolved_type, resolved_continue, _ = _page_state(response)
            final_path = urlsplit(final_url).path or final_url
            last_path = final_path
            self._log(
                f"手机号登录后解析添加邮箱页: page={resolved_type or 'html'}, path={final_path}"
            )
            if (
                resolved_type == "add_email"
                or "/add-email" in final_url
                or "/add-email" in resolved_continue
            ):
                return resolved_continue or final_url

        raise ChatGPTBindEmailError(
            f"手机号账号登录后未进入添加邮箱页: {last_path}"
        )

    def _wait_for_email_code(self, before_ids: set) -> str:
        deadline = time.monotonic() + self.otp_timeout_seconds
        self._log(f"等待绑定邮箱验证码，最长 {self.otp_timeout_seconds // 60} 分钟...")
        while True:
            self._raise_if_cancelled()
            remaining = int(deadline - time.monotonic())
            if remaining <= 0:
                raise ChatGPTBindEmailError("等待绑定邮箱验证码超时")
            try:
                code = self.mailbox.wait_for_code(
                    self.mailbox_account,
                    timeout=min(remaining, 20),
                    before_ids=before_ids,
                    code_pattern=r"(?<!#)(?<!\d)(\d{6})(?!\d)",
                )
            except TimeoutError:
                continue
            except Exception as exc:
                if not _is_transient_proxy_error(exc):
                    raise
                self._log(f"邮箱取码网络中断，继续轮询: {str(exc)[:180]}")
                time.sleep(min(3, max(remaining, 1)))
                continue
            code = str(code or "").strip()
            if code:
                self._log("已获取绑定邮箱验证码")
                return code

    def _refresh_session(self, engine: RegistrationEngine, continue_url: str) -> dict:
        if continue_url:
            target = urljoin(OPENAI_AUTH, continue_url)
            try:
                response = engine.session.get(target, allow_redirects=True, timeout=25)
                self._log(f"绑定邮箱 OAuth 回调状态: {response.status_code}")
            except Exception as exc:
                self._log(f"绑定已确认，但刷新 OAuth 回调失败: {exc}")

        access_token = ""
        id_token = ""
        remote_email = ""
        session_payload: dict = {}
        try:
            response = engine.session.get(
                f"{CHATGPT_APP}/api/auth/session",
                headers={"accept": "application/json"},
                timeout=20,
            )
            if response.status_code == 200:
                session_payload = response.json() or {}
                access_token = str(session_payload.get("accessToken") or "")
                id_token = str(session_payload.get("idToken") or session_payload.get("id_token") or "")
                user = session_payload.get("user") if isinstance(session_payload.get("user"), dict) else {}
                remote_email = str(user.get("email") or session_payload.get("email") or "").strip()
        except Exception as exc:
            self._log(f"绑定已确认，但读取新 session 失败: {exc}")

        session_token = str(
            engine.session.cookies.get("__Secure-next-auth.session-token")
            or self.existing_session_token
            or ""
        )
        auth_cookies = _export_session_cookies(engine.session)
        return {
            "access_token": access_token or self.existing_access_token,
            "id_token": id_token,
            "session_token": session_token,
            "remote_email": remote_email,
            "session_refreshed": bool(access_token),
            "session": session_payload,
            "account_id": _extract_chatgpt_account_id(access_token or self.existing_access_token),
            "cookies": _cookie_header(auth_cookies),
            "auth_cookies": auth_cookies,
            "oai_device_id": str(engine._device_id or ""),
            "auth_proxy_url": str(self.proxy_url or ""),
        }

    def _restore_auth_context(self, engine: RegistrationEngine) -> None:
        for item in self.existing_auth_cookies:
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name or not value:
                continue
            kwargs = {"path": str(item.get("path") or "/")}
            domain = str(item.get("domain") or "").strip()
            if domain:
                kwargs["domain"] = domain
            engine.session.cookies.set(name, value, **kwargs)
        if self.existing_session_token and not engine.session.cookies.get(
            "__Secure-next-auth.session-token"
        ):
            engine.session.cookies.set(
                "__Secure-next-auth.session-token",
                self.existing_session_token,
                domain="chatgpt.com",
                path="/",
            )
        if self.existing_device_id:
            engine._device_id = self.existing_device_id
            if not engine.session.cookies.get("oai-did"):
                engine.session.cookies.set(
                    "oai-did",
                    self.existing_device_id,
                    domain="auth.openai.com",
                    path="/",
                )

    def _clear_auth_transaction_cookies(self, engine: RegistrationEngine) -> None:
        """Start add-email with a fresh auth step while retaining device/session identity."""
        cookie_names = {"login_session", "oai-client-auth-session"}
        retained_auth_names = {"oai-did", "__cflb", "__cf_bm", "_cfuvid", "__oailb"}
        cookies = engine.session.cookies
        jar = getattr(cookies, "jar", None)
        removed = 0
        if jar is not None:
            for cookie in list(jar):
                name = str(getattr(cookie, "name", "") or "")
                domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
                clear_auth_identity = (
                    domain in {"auth.openai.com", "openai.com"}
                    and name not in retained_auth_names
                )
                if (
                    name not in cookie_names
                    and not name.startswith("oai-client-auth-session")
                    and not clear_auth_identity
                ):
                    continue
                try:
                    jar.clear(cookie.domain, cookie.path, cookie.name)
                    removed += 1
                except KeyError:
                    continue
        else:
            delete_cookie = getattr(cookies, "delete", None)
            if callable(delete_cookie):
                for name in cookie_names:
                    try:
                        delete_cookie(name)
                        removed += 1
                    except (KeyError, ValueError):
                        continue
        self._log(f"添加邮箱授权事务已重置: removed_cookies={removed}")

    def _request_codex_oauth_hop(self, engine: RegistrationEngine, url: str):
        route_rotated = False
        attempt = 0
        while True:
            attempt += 1
            self._raise_if_cancelled()
            try:
                response = engine.session.get(
                    url,
                    allow_redirects=False,
                    timeout=30,
                )
                if response.status_code not in {502, 503, 504}:
                    return response
                error = RuntimeError(f"HTTP {response.status_code}")
            except Exception as exc:
                if not _is_transient_proxy_error(exc):
                    raise ChatGPTBindEmailError(f"添加邮箱 Codex OAuth 网络失败: {exc}") from exc
                error = exc

            if attempt >= 3:
                if not route_rotated and self._rotate_711_proxy_session(engine):
                    route_rotated = True
                    attempt = 0
                    continue
                raise ChatGPTBindEmailError(f"添加邮箱 Codex OAuth 网络失败: {error}") from error
            self._log(f"Codex OAuth 当前跳代理连接失败，重连 ({attempt}/3)")
            time.sleep(min(attempt, 2))

    def _open_codex_oauth_entry(self, engine: RegistrationEngine):
        current_url = engine.oauth_start.auth_url
        for index in range(8):
            response = self._request_codex_oauth_hop(engine, current_url)
            location = str(response.headers.get("location") or "").strip()
            if response.status_code not in {301, 302, 303, 307, 308} or not location:
                return response, str(getattr(response, "url", "") or current_url)

            next_url = urljoin(current_url, location)
            parsed = urlsplit(next_url)
            self._log(
                f"Codex OAuth 跳转[{index + 1}]: HTTP {response.status_code} "
                f"-> {parsed.hostname or '-'}{parsed.path or '/'}"
            )
            if (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}:
                query = parse_qs(parsed.query)
                error = str((query.get("error") or [""])[0])
                description = str((query.get("error_description") or [""])[0])
                if error:
                    detail = f": {description}" if description else ""
                    raise ChatGPTBindEmailError(f"Codex OAuth 返回错误 {error}{detail}")
                raise ChatGPTBindEmailError("Codex OAuth 直接完成，未进入添加邮箱流程")
            current_url = next_url
        raise ChatGPTBindEmailError("Codex OAuth 重定向次数过多")

    def _rotate_711_proxy_session(self, engine: RegistrationEngine) -> bool:
        region = infer_proxy_region(self.proxy_url)
        if not region:
            return False
        fresh_proxy = pin_711proxy_session(
            self.proxy_url,
            region=region,
            session_id=secrets.token_hex(5),
            session_minutes=180,
        )
        if not fresh_proxy or fresh_proxy == self.proxy_url:
            return False

        cookie_records = _export_session_cookies(engine.session)
        device_id = str(engine._device_id or "")
        previous_proxy = str(engine.proxy_url or "")
        engine.proxy_url = fresh_proxy
        if not engine._reset_http_session():
            engine.proxy_url = previous_proxy
            return False
        for item in cookie_records:
            kwargs = {"path": str(item.get("path") or "/")}
            domain = str(item.get("domain") or "").strip()
            if domain:
                kwargs["domain"] = domain
            engine.session.cookies.set(item["name"], item["value"], **kwargs)
        engine._device_id = device_id
        if device_id and not engine.session.cookies.get("oai-did"):
            engine.session.cookies.set(
                "oai-did",
                device_id,
                domain="auth.openai.com",
                path="/",
            )
        self.proxy_url = fresh_proxy
        self._log(f"711Proxy 添加邮箱线路已切换到同国家新 Session: country={region}")
        return True

    def _bind_authenticated_engine(self, engine: RegistrationEngine, add_email_url: str) -> dict:
        if self.mailbox_account is None:
            self.mailbox_account = self.mailbox.get_email()
        if not str(self.mailbox_account.email or "").strip():
            raise ChatGPTBindEmailError("邮箱 Provider 未返回邮箱地址")
        before_ids = set(self.mailbox.get_current_ids(self.mailbox_account) or set())
        self._log(f"准备给手机号账号添加邮箱: {self.mailbox_account.email}")

        _, email_continue_url = self._submit_add_email(engine, add_email_url)
        engine._email_otp_continue_url = email_continue_url or f"{OPENAI_AUTH}/email-verification"
        self._log("绑定邮箱验证码已由 add-email 接口发送")
        code = self._wait_for_email_code(before_ids)
        if not engine._validate_verification_code(code):
            raise ChatGPTBindEmailError(engine._step_error_message or "绑定邮箱验证码校验失败")

        refreshed = self._refresh_session(engine, "")
        expected_email = str(self.mailbox_account.email or "").strip().lower()
        remote_email = str(refreshed.get("remote_email") or "").strip().lower()
        if "@" in remote_email and remote_email != expected_email:
            raise ChatGPTBindEmailError("远端 session 返回的邮箱与本次绑定邮箱不一致")
        self._log("手机号账号添加邮箱成功，Free 注册流程不执行 Workspace 选择")
        return {"email": self.mailbox_account.email, **refreshed}

    def _authorize_add_email(self, engine: RegistrationEngine) -> str:
        engine.email = self.phone_number
        engine.password = self.password
        self._clear_auth_transaction_cookies(engine)
        engine.oauth_start = generate_oauth_url(
            client_id=CODEX_CLIENT_ID,
            redirect_uri=CODEX_REDIRECT_URI,
            scope=CODEX_SCOPE,
        )
        self._log("通过 Codex PKCE OAuth 发起添加邮箱授权...")
        response, final_url = self._open_codex_oauth_entry(engine)
        if response.status_code >= 400:
            raise ChatGPTBindEmailError(
                f"添加邮箱 Codex OAuth 启动失败: HTTP {response.status_code}"
            )
        engine._authorize_final_url = final_url
        device_id = str(engine.session.cookies.get("oai-did") or engine._device_id or "")
        if not device_id:
            raise ChatGPTBindEmailError("添加邮箱 Codex OAuth 未建立 Device ID")
        engine._device_id = device_id
        final_url = str(engine._authorize_final_url or "")
        self._log(f"添加邮箱 Codex OAuth 落点: {final_url[:160]}")
        if "/add-email" in final_url:
            return final_url
        if "/log-in" in final_url and "/log-in/password" not in final_url:
            page_type, continue_url = self._submit_phone_identifier(engine)
            if page_type == "add_email" or "/add-email" in continue_url:
                return continue_url
            if page_type == "external_url":
                return self._resolve_add_email_page(engine, page_type, continue_url)
            if page_type not in {"login_password", "password"} and "/log-in/password" not in continue_url:
                raise ChatGPTBindEmailError(
                    f"Codex OAuth 提交手机号后未进入密码页: {page_type or continue_url or 'unknown'}"
                )
            engine._authorize_final_url = continue_url or f"{OPENAI_AUTH}/log-in/password"
        if "/log-in/password" not in final_url:
            final_url = str(engine._authorize_final_url or "")
        if "/log-in/password" not in final_url:
            raise ChatGPTBindEmailError(f"手机号账号未进入密码登录页: {final_url[:160]}")
        page_type, continue_url = self._submit_phone_password(engine)
        return self._resolve_add_email_page(engine, page_type, continue_url)

    def bind_with_engine(self, engine: RegistrationEngine) -> dict:
        """Bind email without replacing the phone-registration HTTP session."""
        if not self.phone_number.startswith("+"):
            raise ChatGPTBindEmailError("账号缺少有效手机号")
        if not self.password:
            raise ChatGPTBindEmailError("手机号账号缺少登录密码")
        self._raise_if_cancelled()
        if engine.session is None and not engine._init_session():
            raise ChatGPTBindEmailError("手机号账号会话初始化失败")

        self._log("复用手机号注册会话完成添加邮箱授权确认")
        add_email_url = self._authorize_add_email(engine)
        return self._bind_authenticated_engine(engine, add_email_url)

    def run(self) -> dict:
        if not self.phone_number.startswith("+"):
            raise ChatGPTBindEmailError("账号缺少有效手机号")
        if not self.password:
            raise ChatGPTBindEmailError("手机号账号缺少登录密码")
        self._raise_if_cancelled()
        engine = RegistrationEngine(
            email_service=_PhoneIdentityService(),
            proxy_url=self.proxy_url,
            callback_logger=self.log_fn,
        )
        engine.email = self.phone_number
        engine.password = self.password
        if not engine._init_session():
            raise ChatGPTBindEmailError("手机号账号会话初始化失败")
        self._restore_auth_context(engine)
        return self.bind_with_engine(engine)
