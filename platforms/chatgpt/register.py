"""

注册流程引擎

从 main.py 中提取并重构的注册流程

"""



import re

import json

import time

import uuid

import base64

import random

import logging

import secrets

import string

from urllib.parse import parse_qs, urljoin, urlsplit

from typing import Optional, Dict, Any, Tuple, Callable

from dataclasses import dataclass

from datetime import datetime, timezone



from curl_cffi import requests as cffi_requests



from .oauth import OAuthManager, OAuthStart, generate_oauth_url, submit_callback_url

from .http_client import OpenAIHTTPClient, HTTPClientError

# from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType  # removed: external dep

# from ..database import crud  # removed: external dep

# from ..database.session import get_db  # removed: external dep

from .constants import (

    OPENAI_API_ENDPOINTS,

    OPENAI_PAGE_TYPES,

    generate_random_user_info,

    OTP_CODE_PATTERN,

    DEFAULT_PASSWORD_LENGTH,

    PASSWORD_CHARSET,

    AccountStatus,

    TaskStatus,

    SENTINEL_SDK_URL,

    OAUTH_REDIRECT_URI,

    OAUTH_CLIENT_ID,

    OPENAI_AUTH,

    CHATGPT_APP,

)

# from ..config.settings import get_settings  # removed: external dep





logger = logging.getLogger(__name__)





@dataclass

class RegistrationResult:

    """注册结果"""

    success: bool

    email: str = ""

    password: str = ""  # 注册密码

    account_id: str = ""

    workspace_id: str = ""

    access_token: str = ""

    refresh_token: str = ""

    id_token: str = ""

    session_token: str = ""  # 会话令牌

    error_message: str = ""

    error_code: str = ""

    logs: list = None

    metadata: dict = None

    source: str = "register"  # 'register' 或 'login'，区分账号来源



    def to_dict(self) -> Dict[str, Any]:

        """转换为字典"""

        return {

            "success": self.success,

            "email": self.email,

            "password": self.password,

            "account_id": self.account_id,

            "workspace_id": self.workspace_id,

            "access_token": self.access_token[:20] + "..." if self.access_token else "",

            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",

            "id_token": self.id_token[:20] + "..." if self.id_token else "",

            "session_token": self.session_token[:20] + "..." if self.session_token else "",

            "error_message": self.error_message,

            "error_code": self.error_code,

            "logs": self.logs or [],

            "metadata": self.metadata or {},

            "source": self.source,

        }





@dataclass

class SignupFormResult:

    """提交注册表单的结果"""

    success: bool

    page_type: str = ""  # 响应中的 page.type 字段

    is_existing_account: bool = False  # 是否为已注册账号

    response_data: Dict[str, Any] = None  # 完整的响应数据

    error_message: str = ""

    error_code: str = ""





@dataclass

class SentinelPayload:

    """Sentinel 请求结果。"""

    p: str

    c: str

    flow: str

    t: str = ""


def _response_error(response) -> tuple[str, str]:
    """Extract a stable server error code without leaking response payloads."""
    try:
        payload = response.json()
    except Exception:
        return "", ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or "").strip(), str(error.get("message") or "").strip()
    if isinstance(error, str):
        return "", error.strip()
    return "", ""


def _classify_server_error(server_code: str, default: str) -> str:
    code = str(server_code or "").strip().lower()
    if code == "invalid_state":
        return "oauth_invalid_state"
    if code in {"wrong_email_otp_code", "expired_email_otp_code", "invalid_email_otp_code"}:
        return "wrong_or_expired_otp"
    if code == "user_already_exists":
        return "email_already_exists"
    if code == "account_deactivated":
        return "email_account_deactivated"
    return default


def _extract_chatgpt_account_id(access_token: str) -> str:
    try:
        parts = str(access_token or "").split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return ""
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    if isinstance(auth, dict):
        account_id = str(auth.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return ""


def _token_value(payload: Any, *keys: str) -> str:
    """Return the first non-empty token-like value from a response mapping."""
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _extract_chatgpt_session_credentials(session_data: dict, oauth_data: Optional[dict] = None) -> dict:
    """Normalize credentials exposed by NextAuth and an optional OAuth exchange.

    The Web session normally exposes only ``accessToken`` plus a session cookie.
    Some deployments also expose refresh/id tokens, while the protocol OAuth
    exchange uses snake_case names.  Keep the actual value and source so callers
    can persist it without inventing a refresh token when the server omitted one.
    """
    session_data = session_data if isinstance(session_data, dict) else {}
    oauth_data = oauth_data if isinstance(oauth_data, dict) else {}

    access_token = _token_value(session_data, "accessToken", "access_token")
    refresh_token = _token_value(session_data, "refreshToken", "refresh_token")
    id_token = _token_value(session_data, "idToken", "id_token")
    expires_at = _token_value(
        session_data,
        "expires",
        "expiresAt",
        "expires_at",
    )
    source = "session"

    if not refresh_token:
        refresh_token = _token_value(oauth_data, "refresh_token", "refreshToken")
        if refresh_token:
            source = "oauth_callback"
    if not access_token:
        access_token = _token_value(oauth_data, "access_token", "accessToken")
    if not id_token:
        id_token = _token_value(oauth_data, "id_token", "idToken")
    if not expires_at:
        expires_at = _token_value(oauth_data, "expires_at", "expires", "expired")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_at": expires_at,
        "refresh_token_source": source if refresh_token else "",
        "refresh_token_status": "available" if refresh_token else "missing_from_session",
    }





# ─── Sentinel helpers (ported from browser_register.py) ──────────



def _generate_datadog_trace_headers() -> dict:

    trace_hex = secrets.token_hex(8).rjust(16, "0")

    parent_hex = secrets.token_hex(8).rjust(16, "0")

    trace_id = str(int(trace_hex, 16))

    parent_id = str(int(parent_hex, 16))

    return {

        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",

        "tracestate": "dd=s:1;o:rum",

        "x-datadog-origin": "rum",

        "x-datadog-parent-id": parent_id,

        "x-datadog-sampling-priority": "1",

        "x-datadog-trace-id": trace_id,

    }





class _SentinelTokenGenerator:

    """Dynamic sentinel token generator – mirrors browser_register._SentinelTokenGenerator."""



    def __init__(self, device_id: str, user_agent: str):

        self.device_id = device_id or str(uuid.uuid4())

        self.user_agent = user_agent

        self.sid = str(uuid.uuid4())



    @staticmethod

    def _fnv1a32(text: str) -> str:

        h = 2166136261

        for ch in text:

            h ^= ord(ch)

            h = (h * 16777619) & 0xFFFFFFFF

        h ^= (h >> 16)

        h = (h * 2246822507) & 0xFFFFFFFF

        h ^= (h >> 13)

        h = (h * 3266489909) & 0xFFFFFFFF

        h ^= (h >> 16)

        return f"{h & 0xFFFFFFFF:08x}"



    @staticmethod

    def _b64(data) -> str:

        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")



    def _config(self) -> list:

        perf_now = 1000 + random.random() * 49000

        return [

            "1920x1080",

            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),

            4294705152,

            random.random(),

            self.user_agent,

            SENTINEL_SDK_URL,

            None,

            None,

            "en-US",

            "en-US,en",

            random.random(),

            "webkitTemporaryStorage\u2212undefined",

            "location",

            "Object",

            perf_now,

            self.sid,

            "",

            random.choice([4, 8, 12, 16]),

            int(time.time() * 1000 - perf_now),

        ]



    def generate_requirements_token(self) -> str:

        cfg = self._config()

        cfg[3] = 1

        cfg[9] = round(5 + random.random() * 45)

        return "gAAAAAC" + self._b64(cfg)



    def generate_token(self, seed: str, difficulty: str) -> str:

        max_attempts = 500000

        cfg = self._config()

        start_ms = int(time.time() * 1000)

        diff = str(difficulty or "0")

        for nonce in range(max_attempts):

            cfg[3] = nonce

            cfg[9] = round(int(time.time() * 1000) - start_ms)

            encoded = self._b64(cfg)

            digest = self._fnv1a32((seed or "") + encoded)

            if digest[: len(diff)] <= diff:

                return "gAAAAAB" + encoded + "~S"

        return "gAAAAAB" + self._b64(None)





class RegistrationEngine:

    """

    注册引擎

    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用

    """



    def __init__(

        self,

        email_service: Any,

        proxy_url: Optional[str] = None,

        callback_logger: Optional[Callable[[str], None]] = None,

        task_uuid: Optional[str] = None,

        preflight_location: Optional[str] = None,

    ):

        """

        初始化注册引擎



        Args:

            email_service: 邮箱服务实例

            proxy_url: 代理 URL

            callback_logger: 日志回调函数

            task_uuid: 任务 UUID（用于数据库记录）

            preflight_location: 已通过代理预检的 IP 国家代码

        """

        self.email_service = email_service

        self.proxy_url = proxy_url

        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))

        self.task_uuid = task_uuid

        self.preflight_location = str(preflight_location or "").strip().upper()



        # 创建 HTTP 客户端

        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)



        # 创建 OAuth 管理器

        from .constants import OAUTH_CLIENT_ID, OAUTH_AUTH_URL, OAUTH_TOKEN_URL, OAUTH_REDIRECT_URI, OAUTH_SCOPE

        self.oauth_manager = OAuthManager(

            client_id=OAUTH_CLIENT_ID,

            auth_url=OAUTH_AUTH_URL,

            token_url=OAUTH_TOKEN_URL,

            redirect_uri=OAUTH_REDIRECT_URI,

            scope=OAUTH_SCOPE,

            proxy_url=proxy_url  # 传递代理配置

        )



        # 状态变量

        self.email: Optional[str] = None

        self.password: Optional[str] = None  # 注册密码

        self.email_info: Optional[Dict[str, Any]] = None

        self.oauth_start: Optional[OAuthStart] = None

        # Populated only when this engine performs a direct OAuth token
        # exchange.  The normal Web flow gets credentials from NextAuth.
        self._oauth_token_info: Dict[str, Any] = {}

        self.session: Optional[cffi_requests.Session] = None

        self.session_token: Optional[str] = None  # 会话令牌

        self.logs: list = []

        self._otp_sent_at: Optional[float] = None  # 服务端确认 OTP 投递后的时间戳
        self._last_otp_error: str = ""
        self._otp_page_reached: bool = False
        self._otp_delivery_requested: bool = False
        self._otp_delivery_confirmed: bool = False
        self._otp_delivery_method: str = ""
        self._otp_delivery_http_status: int = 0
        self._otp_delivery_page_type: str = ""

        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）

        self._device_id: Optional[str] = None
        self._authorize_final_url: str = ""

        self._sentinel_token: Optional[str] = None

        self._signup_sentinel: Optional[SentinelPayload] = None

        self._password_sentinel: Optional[SentinelPayload] = None

        self._create_account_continue_url: Optional[str] = None

        self._email_otp_continue_url: Optional[str] = None

        self._email_otp_page_loaded: bool = False
        self._email_otp_csrf_token: str = ""

        self._otp_continue_url: Optional[str] = None

        self._otp_page_type: Optional[str] = None

        self._otp_response_data: dict = {}

        self._otp_external_method: str = "GET"

        self._password_next_page_type: str = ""

        self._password_continue_url: str = ""

        self._password_next_payload: dict = {}

        self._step_error_code: str = ""

        self._step_error_message: str = ""

        # Protocol mailbox registrations use the passwordless OAuth path.
        # Landing on /email-verification proves only that the challenge page
        # was reached.  It does not prove that an email was delivered; the
        # delivery action must still return a successful server response.
        self.email_otp_first: bool = False
        self._oauth_email_verification: bool = False
        self.otp_submit_delay: float = 0.0



    def _log(self, message: str, level: str = "info"):

        """记录日志"""

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")

        log_message = f"[{timestamp}] {message}"



        # 添加到日志列表

        self.logs.append(log_message)



        # 调用回调函数

        if self.callback_logger:

            self.callback_logger(message)



        # 记录到数据库（如果有关联任务）

        if self.task_uuid:

            try:

                with get_db() as db:

                    crud.append_task_log(db, self.task_uuid, message)

            except Exception as e:

                logger.warning(f"记录任务日志失败: {e}")



        # 根据级别记录到日志系统

        if level == "error":

            logger.error(message)

        elif level == "warning":

            logger.warning(message)

        else:

            logger.info(message)



    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:

        """生成随机密码"""

        # OpenAI 注册页对纯字母数字密码存在更高概率拒绝，补一个符号位更稳。

        specials = ",._!@#"

        if length < 10:

            length = 10

        core = ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length - 2))

        return (

            secrets.choice("abcdefghijklmnopqrstuvwxyz")

            + secrets.choice("0123456789")

            + secrets.choice(specials)

            + core

        )[:length]



    def _load_create_account_password_page(self) -> bool:

        """预加载 create-account/password 页面，拿到页面阶段 cookie。"""

        try:

            response = self.session.get(

                "https://auth.openai.com/create-account/password",

                headers={

                    "referer": "https://chatgpt.com/",

                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",

                },

                timeout=20,

            )

            self._log(f"加载密码页状态: {response.status_code}")

            return response.status_code == 200

        except Exception as e:

            self._log(f"加载密码页失败: {e}", "warning")

            return False



    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:

        """检查 IP 地理位置"""

        preflight_location = str(getattr(self, "preflight_location", "") or "").strip().upper()
        if preflight_location:
            return True, preflight_location

        try:

            return self.http_client.check_ip_location()

        except Exception as e:

            self._log(f"检查 IP 地理位置失败: {e}", "error")

            return False, None



    def _create_email(self) -> bool:

        """创建邮箱"""

        try:

            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")

            self.email_info = self.email_service.create_email()



            if not self.email_info or "email" not in self.email_info:

                self._log("创建邮箱失败: 返回信息不完整", "error")

                return False



            self.email = self.email_info["email"]

            self._log(f"成功创建邮箱: {self.email}")

            return True



        except Exception as e:

            self._log(f"创建邮箱失败: {e}", "error")

            return False



    def _start_oauth(self) -> bool:

        """通过 chatgpt.com NextAuth 发起 OAuth 流程"""

        try:

            from .constants import CHATGPT_APP
            from urllib.parse import urlencode

            self._log("通过 chatgpt.com NextAuth 发起 OAuth...")



            # 1. 为当前会话创建稳定 Device ID，并验证 NextAuth provider API。

            oai_did = self.session.cookies.get("oai-did", "")

            if not oai_did:

                oai_did = str(uuid.uuid4())

                for domain in ("chatgpt.com", ".chatgpt.com", "auth.openai.com", ".auth.openai.com"):

                    try:

                        self.session.cookies.set("oai-did", oai_did, domain=domain, path="/")

                    except Exception:

                        pass

                self._log("chatgpt.com 未下发 oai-did，已创建当前会话 Device ID", "warning")

            if oai_did:

                self._device_id = str(oai_did)

            self._log(f"chatgpt.com oai-did: {'yes' if oai_did else 'no'}")

            nextauth_headers = self.http_client.get_chatgpt_headers()
            providers_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/providers",
                headers=nextauth_headers,
                timeout=30,
            )
            self._log(f"NextAuth providers 状态: {providers_resp.status_code}")
            if providers_resp.status_code != 200:
                self._step_error_code = (
                    "proxy_or_access_blocked"
                    if providers_resp.status_code in {401, 403, 429}
                    else "oauth_start_failed"
                )
                return False



            # 2. 获取 CSRF token

            csrf_resp = self.session.get(
                f"{CHATGPT_APP}/api/auth/csrf",
                headers=nextauth_headers,
                timeout=30,
            )

            if csrf_resp.status_code != 200:
                self._step_error_code = (
                    "proxy_or_access_blocked"
                    if csrf_resp.status_code in {401, 403, 429}
                    else "oauth_start_failed"
                )
                self._log(f"CSRF 请求失败: HTTP {csrf_resp.status_code}", "error")
                return False

            csrf_data = csrf_resp.json()

            csrf_token = csrf_data.get("csrfToken", "")

            if not csrf_token:

                # 从 cookie 中提取

                csrf_cookie = self.session.cookies.get("__Host-next-auth.csrf-token", "")

                csrf_token = csrf_cookie.split("%7C")[0] if "%7C" in csrf_cookie else csrf_cookie.split("|")[0]

            self._log(f"CSRF token: {'yes' if csrf_token else 'no'}")



            # 3. 调用 signin/openai 获取 authorize URL

            email_otp_first = bool(self.email_otp_first)
            signin_query_data = {
                "ext-oai-did": oai_did,
                "auth_session_logging_id": str(uuid.uuid4()),
                "ext-passkey-client-capabilities": "1111",
                "screen_hint": "login_or_signup",
                "login_hint": self.email or "",
            }
            if not email_otp_first:
                signin_query_data["prompt"] = "login"
            else:
                self._log("OAuth 邮箱优先模式: prompt=省略，等待授权入口自动进入邮箱验证")

            signin_query = urlencode(signin_query_data)

            signin_url = f"{CHATGPT_APP}/api/auth/signin/openai?{signin_query}"



            signin_resp = self.session.post(

                signin_url,

                headers={
                    **nextauth_headers,
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": CHATGPT_APP,
                },

                data=urlencode(
                    {"csrfToken": csrf_token}
                    if email_otp_first
                    else {
                        "callbackUrl": f"{CHATGPT_APP}/login",
                        "csrfToken": csrf_token,
                        "json": "true",
                    }
                ),

                timeout=30,

                allow_redirects=not email_otp_first,

            )

            self._log(f"signin/openai 状态: {signin_resp.status_code}")



            if signin_resp.status_code not in (200, 302, 303):

                self._step_error_code = (
                    "proxy_or_access_blocked"
                    if signin_resp.status_code in {401, 403, 429}
                    else "oauth_start_failed"
                )
                self._log(f"signin/openai 失败: HTTP {signin_resp.status_code}", "error")

                return False



            try:
                signin_data = signin_resp.json()
                auth_url = signin_data.get("url", "")
            except Exception:
                auth_url = ""

            if not auth_url:
                auth_url = str(signin_resp.headers.get("Location") or "")

            if not auth_url:

                self._log("signin/openai 未返回 authorize URL", "error")

                return False



            self._log("OAuth authorize URL 获取成功")



            # 存储为 OAuthStart (不需要 code_verifier，由 chatgpt.com 后端处理)

            self.oauth_start = OAuthStart(

                auth_url=auth_url,

                state="",  # state 由 NextAuth 管理

                code_verifier="",  # 不需要

                redirect_uri="",  # 不需要

            )

            return True



        except Exception as e:

            self._step_error_code = "proxy_network_error"
            self._step_error_message = str(e)[:500]
            self._log(f"NextAuth OAuth 流程失败: {e}", "error")

            return False



    def _init_session(self) -> bool:

        """初始化会话"""

        try:

            self.session = self.http_client.session

            return True

        except Exception as e:

            self._log(f"初始化会话失败: {e}", "error")

            return False

    def _reset_http_session(self) -> bool:

        """Create a fresh client for an invalid OAuth state retry."""

        try:

            self.http_client = OpenAIHTTPClient(proxy_url=self.proxy_url)

            return self._init_session()

        except Exception as e:

            self._log(f"重建 HTTP 会话失败: {e}", "error")

            return False



    def _get_device_id(self) -> Optional[str]:

        """打开 authorize URL，建立 auth.openai 授权态并返回 Device ID。"""

        try:

            if not self.oauth_start:

                return None



            response = self.session.get(

                self.oauth_start.auth_url,

                timeout=30

            )

            final_url = str(getattr(response, "url", "") or "")
            self._authorize_final_url = final_url
            if self.email_otp_first and "/email-verification" in final_url:
                self._oauth_email_verification = True
                self._otp_page_reached = True
                self._email_otp_continue_url = final_url
                self._log("OAuth 已直接进入邮箱验证页；保留当前 challenge，稍后显式确认 OTP 投递")

            if response.status_code >= 400:

                self._step_error_code = (
                    "proxy_or_access_blocked"
                    if response.status_code in {401, 403, 429}
                    else "oauth_authorize_failed"
                )
                self._log(f"OAuth authorize 状态异常: {response.status_code}", "error")

                return None

            did = self.session.cookies.get("oai-did")

            login_session = self.session.cookies.get("login_session")

            if did:

                self._device_id = str(did)

            self._log(

                f"Device ID: {str(did or '')[:20]}... "

                f"(authorize_session={'yes' if login_session else 'cookie-set'})"

            )

            return did



        except Exception as e:

            self._step_error_code = "proxy_network_error"
            self._step_error_message = str(e)[:500]
            self._log(f"获取 Device ID 失败: {e}", "error")

            return None



    def _check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> Optional[SentinelPayload]:

        """检查 Sentinel 拦截（动态生成 token + 处理 PoW）"""

        try:

            ua = self.http_client.default_headers.get("User-Agent", "")

            generator = _SentinelTokenGenerator(did, ua)

            sent_p = generator.generate_requirements_token()

            sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": flow}, separators=(",", ":"))



            from .constants import SENTINEL_FRAME_URL

            response = self.http_client.post(

                OPENAI_API_ENDPOINTS["sentinel"],

                headers={

                    "origin": "https://sentinel.openai.com",

                    "referer": SENTINEL_FRAME_URL,

                    "content-type": "text/plain;charset=UTF-8",

                },

                data=sen_req_body,

            )



            if response.status_code == 200:

                data = response.json()

                sen_token = str(data.get("token") or "")

                turnstile = data.get("turnstile") or {}



                # Handle proofofwork challenge if required

                initial_p = sent_p  # keep for dx decryption

                pow_meta = data.get("proofofwork") or {}

                if pow_meta.get("required") and pow_meta.get("seed"):

                    sent_p = generator.generate_token(

                        str(pow_meta.get("seed") or ""),

                        str(pow_meta.get("difficulty") or "0"),

                    )

                    self._log(f"Sentinel PoW solved: flow={flow}")



                # Solve turnstile dx with VM

                t_value = ""

                dx_b64 = str(turnstile.get("dx") or "")

                if dx_b64:

                    try:

                        from .sentinel_vm import solve_turnstile_dx

                        from .constants import SENTINEL_SDK_URL

                        t_value = solve_turnstile_dx(dx_b64, initial_p, user_agent=ua, sdk_url=SENTINEL_SDK_URL)

                        self._log(f"Sentinel VM solved: t_len={len(t_value)} flow={flow}")

                    except Exception as vm_err:

                        self._log(f"Sentinel VM failed: {vm_err}", "warning")



                payload = SentinelPayload(

                    p=sent_p,

                    c=sen_token,

                    flow=flow,

                    t=t_value,

                )

                self._log(f"Sentinel token 获取成功: flow={flow}")

                return payload

            else:

                self._log(f"Sentinel 检查失败: flow={flow} status={response.status_code}", "warning")

                return None



        except Exception as e:

            self._log(f"Sentinel 检查异常: flow={flow} {e}", "warning")

            return None



    def _submit_signup_form(self, did: str, sen_payload: Optional[SentinelPayload]) -> SignupFormResult:

        """

        提交注册表单（通过 authorize/continue 建立 session）



        Returns:

            SignupFormResult: 提交结果，包含账号状态判断

        """

        try:

            self._device_id = did

            self._signup_sentinel = sen_payload

            self._sentinel_token = sen_payload.c if sen_payload else None

            signup_body = json.dumps({"username": {"value": self.email, "kind": "email"}, "screen_hint": "signup"})



            headers = {

                "referer": "https://auth.openai.com/create-account",

                "accept": "application/json",

                "content-type": "application/json",

                "sec-fetch-site": "same-origin",

                **_generate_datadog_trace_headers(),

            }



            if did:

                headers["oai-device-id"] = did



            if sen_payload:

                sentinel = json.dumps({

                    "p": sen_payload.p,

                    "t": sen_payload.t,

                    "c": sen_payload.c,

                    "id": did,

                    "flow": sen_payload.flow,

                }, separators=(",", ":"))

                headers["openai-sentinel-token"] = sentinel



            response = self.session.post(

                OPENAI_API_ENDPOINTS["signup"],

                headers=headers,

                data=signup_body,
                timeout=15,

            )



            self._log(f"提交注册表单状态: {response.status_code}")



            if response.status_code != 200:

                server_code, server_message = _response_error(response)

                return SignupFormResult(

                    success=False,

                    error_message=server_message or f"HTTP {response.status_code}",

                    error_code=_classify_server_error(server_code, "signup_failed"),

                )



            try:

                response_data = response.json()

            except Exception as parse_error:

                self._log(f"signup 响应非 JSON: {parse_error}", "warning")

                return SignupFormResult(

                    success=False,

                    error_message="signup 返回非 JSON",

                    error_code="signup_invalid_response",

                    response_data={},

                )



            if isinstance(response_data, dict):

                err = response_data.get("error") or response_data.get("detail") or ""

                if err:

                    err_msg = err if isinstance(err, str) else json.dumps(err)

                    self._log(f"signup 返回错误: {err_msg}", "warning")



            page_type = response_data.get("page", {}).get("type", "")

            continue_url = str(response_data.get("continue_url") or "")

            self._log(f"响应页面类型: {page_type}")



            is_existing = False

            if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:

                self._email_otp_continue_url = continue_url or "https://auth.openai.com/email-verification"

                self._log("已进入邮箱 OTP 验证流程，将显式发送验证码")



            return SignupFormResult(

                success=True,

                page_type=page_type,

                is_existing_account=is_existing,

                response_data=response_data

            )



        except Exception as e:

            self._log(f"提交注册表单失败: {e}", "error")

            return SignupFormResult(success=False, error_message=str(e), error_code="proxy_network_error")



    def _register_password(self) -> Tuple[bool, Optional[str]]:

        """注册密码"""

        try:

            ua = self.http_client.default_headers.get("User-Agent", "")

            chrome_match = re.search(r"Chrome/(\d+)", ua)

            chrome_major = str(chrome_match.group(1) if chrome_match else "136")

            sec_ch_ua = f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", "Not.A/Brand";v="99"'



            candidates = []

            while len(candidates) < 3:

                pwd = self._generate_password()

                if pwd not in candidates:

                    candidates.append(pwd)



            for index, password in enumerate(candidates, start=1):

                self.password = password



                # Reload page + refresh sentinel for each attempt (tokens are single-use)

                self._load_create_account_password_page()

                if self._device_id:

                    self._password_sentinel = self._check_sentinel(self._device_id, flow="username_password_create")

                    if self._password_sentinel:

                        self._log(

                            f"密码阶段 Sentinel 已刷新: flow={self._password_sentinel.flow} "

                            f"turnstile={'yes' if self._password_sentinel.t else 'no'}"

                        )



                self._log(f"生成密码[{index}/{len(candidates)}]（内容不写入日志）")



                register_body = json.dumps({

                    "password": password,

                    "username": self.email

                })



                register_headers = {

                    "origin": "https://auth.openai.com",

                    "referer": "https://auth.openai.com/create-account/password",

                    "accept": "application/json",

                    "content-type": "application/json",

                    "accept-language": "en-US,en;q=0.9",

                    "sec-ch-ua": sec_ch_ua,

                    "sec-ch-ua-mobile": "?0",

                    "sec-ch-ua-platform": '"Windows"',

                    "sec-fetch-dest": "empty",

                    "sec-fetch-mode": "cors",

                    "sec-fetch-site": "same-origin",

                    **_generate_datadog_trace_headers(),

                }

                if self._device_id:

                    register_headers["oai-device-id"] = self._device_id

                if self._password_sentinel and self._device_id:

                    register_headers["openai-sentinel-token"] = json.dumps({

                        "p": self._password_sentinel.p,

                        "t": self._password_sentinel.t,

                        "c": self._password_sentinel.c,

                        "id": self._device_id,

                        "flow": self._password_sentinel.flow,

                    }, separators=(",", ":"))



                response = self.session.post(

                    OPENAI_API_ENDPOINTS["register"],

                    headers=register_headers,

                    data=register_body,

                    timeout=15,

                )



                self._log(f"提交密码状态[{index}/{len(candidates)}]: {response.status_code}")



                if response.status_code == 200:

                    # 解析响应，检测已注册账号

                    try:

                        resp_data = response.json()

                        page_type = resp_data.get("page", {}).get("type", "")

                        continue_url = str(resp_data.get("continue_url") or "")

                        self._password_next_page_type = str(page_type or "")

                        self._password_continue_url = continue_url

                        page_payload = (resp_data.get("page") or {}).get("payload") or {}

                        self._password_next_payload = dict(page_payload) if isinstance(page_payload, dict) else {}

                        self._log(f"注册响应页面类型: {page_type}")

                        if page_type == OPENAI_PAGE_TYPES.get("EMAIL_OTP_VERIFICATION", "email_otp_verification"):

                            self._log("密码提交后进入邮箱 OTP 验证流程")

                            if continue_url:

                                self._email_otp_continue_url = continue_url

                                self._log("密码响应已返回 continue_url")

                    except Exception:

                        pass

                    return True, password



                error_text = response.text[:500]

                self._log(f"密码注册失败[{index}/{len(candidates)}]: {error_text}", "warning")



                try:

                    error_json = response.json()

                    error_msg = error_json.get("error", {}).get("message", "")

                    error_code = error_json.get("error", {}).get("code", "")

                    normalized_error_code = str(error_code or "").strip().lower()
                    normalized_error_message = str(error_msg or "").strip().lower()
                    if (
                        normalized_error_code in {
                            "voip_phone_disallowed",
                            "invalid_phone_number",
                            "phone_number_invalid",
                            "phone_number_disallowed",
                        }
                        or "invalid phone number" in normalized_error_message
                    ):
                        self._step_error_code = "phone_number_rejected"
                        self._step_error_message = error_msg or error_code or "手机号被注册服务拒绝"
                        self._log("手机号被注册服务拒绝，立即释放并换号", "warning")
                        return False, None

                    is_phone_identity = str(self.email or "").strip().startswith("+")
                    if is_phone_identity and normalized_error_code == "account_creation_failed":
                        self._step_error_code = "phone_number_rejected"
                        self._step_error_message = error_msg or "手机号账户创建失败"
                        self._log("手机号账户创建失败，当前号码继续提交密码无收益，立即释放并换号", "warning")
                        return False, None



                    if "already" in error_msg.lower() or "exists" in error_msg.lower() or error_code == "user_exists":

                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")

                        self._mark_email_as_registered()

                        return False, None

                except Exception:

                    pass



            return False, None



        except Exception as e:

            self._log(f"密码注册失败: {e}", "error")

            return False, None



    def _mark_email_as_registered(self):

        """标记邮箱为已注册状态（用于防止重复尝试）"""

        try:

            with get_db() as db:

                # 检查是否已存在该邮箱的记录

                existing = crud.get_account_by_email(db, self.email)

                if not existing:

                    # 创建一个失败记录，标记该邮箱已注册过

                    crud.create_account(

                        db,

                        email=self.email,

                        password="",  # 空密码表示未成功注册

                        email_service=self.email_service.service_type.value,

                        email_service_id=self.email_info.get("service_id") if self.email_info else None,

                        status="failed",

                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}

                    )

                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")

        except Exception as e:

            logger.warning(f"标记邮箱状态失败: {e}")



    def _reset_otp_delivery_state(self) -> None:
        self._otp_sent_at = None
        self._otp_delivery_requested = False
        self._otp_delivery_confirmed = False
        self._otp_delivery_method = ""
        self._otp_delivery_http_status = 0
        self._otp_delivery_page_type = ""

    def _begin_new_otp_wait(self) -> None:
        resetter = getattr(self.email_service, "begin_new_otp_wait", None)
        if not callable(resetter):
            return
        try:
            resetter()
            self._log("邮箱轮询基线已重置，后续只接受本次认证产生的新邮件")
        except Exception as exc:
            self._log(f"邮箱轮询基线重置失败: {type(exc).__name__}", "warning")

    @staticmethod
    def _otp_response_page_type(response) -> str:
        try:
            payload = response.json()
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        page = payload.get("page")
        if isinstance(page, dict):
            return str(page.get("type") or "").strip()
        return ""

    def _request_email_otp_delivery(
        self,
        *,
        session=None,
        referer: str = "",
        prefer_resend: bool = False,
        include_sentinel: bool = True,
    ) -> bool:
        """Request OTP delivery and mark it sent only after a confirmed response."""
        target_session = session or self.session
        email_verification_url = (
            referer
            or self._email_otp_continue_url
            or f"{OPENAI_AUTH}/email-verification"
        )
        self._reset_otp_delivery_state()

        headers = {
            "origin": OPENAI_AUTH,
            "referer": email_verification_url,
            "accept": "application/json, text/plain, */*",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            **_generate_datadog_trace_headers(),
        }
        if self._device_id:
            headers["oai-device-id"] = self._device_id
        csrf_token = str(getattr(self, "_email_otp_csrf_token", "") or "")
        if csrf_token:
            headers["x-csrf-token"] = csrf_token
        sentinel_payload = (
            (self._password_sentinel or self._signup_sentinel)
            if include_sentinel else None
        )
        if sentinel_payload and self._device_id:
            headers["openai-sentinel-token"] = json.dumps({
                "p": sentinel_payload.p,
                "t": sentinel_payload.t,
                "c": sentinel_payload.c,
                "id": self._device_id,
                "flow": sentinel_payload.flow,
            }, separators=(",", ":"))

        last_status = 0
        last_server_code = ""

        def request(action: str, method: str, endpoint: str):
            nonlocal last_status, last_server_code
            self._otp_delivery_requested = True
            self._otp_delivery_method = f"{action}:{method}"
            try:
                if method == "POST":
                    response = target_session.post(
                        endpoint,
                        headers={**headers, "content-type": "application/json"},
                        data="{}",
                        timeout=15,
                    )
                else:
                    response = target_session.get(
                        endpoint,
                        headers=headers,
                        timeout=15,
                    )
            except Exception as exc:
                # Do not blindly retry an indeterminate delivery request: the
                # first request may have reached the server and a duplicate can
                # rotate the challenge/code while the mailbox is being polled.
                safe_error = re.sub(
                    r"(?i)(https?://)([^/@\s]+)@",
                    r"\1***@",
                    str(exc),
                )[:300]
                self._step_error_code = "proxy_network_error"
                self._step_error_message = safe_error or type(exc).__name__
                self._log(
                    f"邮箱 OTP 投递请求异常: action={action} method={method} "
                    f"error={type(exc).__name__}",
                    "error",
                )
                return None

            last_status = int(getattr(response, "status_code", 0) or 0)
            last_server_code, _server_message = _response_error(response)
            page_type = self._otp_response_page_type(response)
            self._otp_delivery_http_status = last_status
            self._otp_delivery_page_type = page_type
            self._log(
                "邮箱 OTP 投递响应: "
                f"action={action} method={method} status={last_status} "
                f"page_type={page_type or '-'} server_code={last_server_code or '-'}"
            )

            if 200 <= last_status < 300 and not last_server_code:
                self._otp_delivery_confirmed = True
                self._otp_sent_at = time.time()
                self._step_error_code = ""
                self._step_error_message = ""
                self._log(
                    "邮箱 OTP 投递已由服务端确认: "
                    f"action={action} status={last_status}"
                )
            return response

        if prefer_resend:
            response = request("resend", "POST", OPENAI_API_ENDPOINTS["resend_otp"])
            if response is None:
                return False
            if self._otp_delivery_confirmed:
                return True
            if last_status in {401, 403, 429}:
                self._step_error_code = "proxy_or_access_blocked"
                self._step_error_message = f"HTTP {last_status}"
                return False
            self._log(
                "当前 challenge 的 OTP 重发未被接受，改用显式发送并刷新邮箱基线",
                "warning",
            )
            self._begin_new_otp_wait()

        response = request("send", "GET", OPENAI_API_ENDPOINTS["send_otp"])
        if response is None:
            return False
        if self._otp_delivery_confirmed:
            return True
        if last_status in {404, 405}:
            self._log("OTP GET 端点不接受当前方法，尝试 POST 兼容请求", "warning")
            response = request("send", "POST", OPENAI_API_ENDPOINTS["send_otp"])
            if response is None:
                return False
            if self._otp_delivery_confirmed:
                return True

        self._step_error_code = (
            "proxy_or_access_blocked"
            if last_status in {401, 403, 429}
            else "otp_delivery_failed"
        )
        detail = f"HTTP {last_status or 'unknown'}"
        if last_server_code:
            detail += f", server_code={last_server_code}"
        self._step_error_message = detail
        self._log(f"邮箱 OTP 投递未确认: {detail}", "error")
        return False

    def _send_verification_code(self) -> bool:
        """Load the challenge page and explicitly confirm email OTP delivery."""
        try:
            email_verification_url = (
                self._email_otp_continue_url
                or f"{OPENAI_AUTH}/email-verification"
            )
            self._log(f"邮箱验证页 URL: {email_verification_url[:120]}")

            if not self._email_otp_page_loaded:
                page_resp = self.session.get(
                    email_verification_url,
                    headers={
                        "referer": f"{OPENAI_AUTH}/create-account",
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                    timeout=15,
                )
                page_status = int(getattr(page_resp, "status_code", 0) or 0)
                self._log(
                    f"邮箱验证码页加载状态: {page_status}, "
                    f"body_len={len(getattr(page_resp, 'text', '') or '')}"
                )
                if page_status not in (200, 304):
                    self._step_error_code = "otp_delivery_failed"
                    self._step_error_message = f"verification_page HTTP {page_status}"
                    self._log(
                        f"邮箱验证码页加载异常，状态码: {page_status}",
                        "warning",
                    )
                    return False
                self._email_otp_page_loaded = True
                self._otp_page_reached = True
                page_text = str(getattr(page_resp, "text", "") or "")
                csrf_match = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', page_text)
                if csrf_match:
                    self._email_otp_csrf_token = csrf_match.group(1)
                    self._log("从邮箱验证页提取到 CSRF token")
                time.sleep(1.5)

            return self._request_email_otp_delivery(
                referer=email_verification_url,
                prefer_resend=bool(
                    self._oauth_email_verification or self._is_existing_account
                ),
            )
        except Exception as exc:
            self._step_error_code = "otp_delivery_failed"
            self._step_error_message = type(exc).__name__
            self._log(f"发送验证码失败: {type(exc).__name__}", "error")
            return False



    def _get_verification_code(self) -> Optional[str]:

        """获取验证码"""

        self._last_otp_error = ""

        if not bool(getattr(self, "_otp_delivery_confirmed", False)):
            self._last_otp_error = "邮箱 OTP 投递未获得服务端确认，已停止轮询"
            self._step_error_code = "otp_delivery_not_confirmed"
            self._log(self._last_otp_error, "error")
            return None

        try:

            email_id = self.email_info.get("service_id") if self.email_info else None

            import os as _os_otp_timeout

            try:

                otp_timeout = int((_os_otp_timeout.environ.get("CHATGPT_OTP_TIMEOUT_SECONDS", "") or "300").strip())

            except Exception:

                otp_timeout = 300

            if otp_timeout < 30:

                otp_timeout = 30



            elapsed_since_send = "0s"

            if self._otp_sent_at:

                elapsed_since_send = f"{time.time() - self._otp_sent_at:.0f}s"



            self._log(
                f"正在等待邮箱 {self.email} 的验证码 "
                f"(超时: {otp_timeout}s, OTP投递已确认: {elapsed_since_send}前, "
                f"方式: {self._otp_delivery_method or 'confirmed'})..."
            )



            code = self.email_service.get_verification_code(

                email=self.email,

                email_id=email_id,

                timeout=otp_timeout,

                pattern=OTP_CODE_PATTERN,

                otp_sent_at=self._otp_sent_at,

            )



            if code:

                self._log("成功获取验证码")

                return code

            else:

                self._last_otp_error = "等待验证码超时"
                self._log("等待验证码超时", "error")

                return None



        except TimeoutError as e:

            self._last_otp_error = f"等待验证码超时: {e}"[:500]
            self._log(f"等待验证码超时: {e}", "error")

            return None

        except Exception as e:

            self._last_otp_error = f"获取验证码失败: {e}"[:500]
            self._log(f"获取验证码失败: {e}", "error")

            return None



    def _validate_verification_code(self, code: str) -> bool:

        """验证验证码"""

        try:

            code_body = json.dumps({"code": code})

            # OTP validation is a separate Sentinel flow.  The browser path
            # sends this token together with the device id; omitting both can
            # make the server reject an otherwise valid OTP as invalid_state.
            sentinel_payload = None
            if self._device_id and not self._oauth_email_verification:
                sentinel_payload = self._check_sentinel(
                    self._device_id,
                    flow="email_otp_validate",
                )

            validate_headers = {
                "referer": self._email_otp_continue_url or "https://auth.openai.com/email-verification",
                "origin": "https://auth.openai.com",
                "accept": "application/json",
                "content-type": "application/json",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                **_generate_datadog_trace_headers(),
            }

            if self._device_id:
                validate_headers["oai-device-id"] = self._device_id

            if sentinel_payload:
                validate_headers["openai-sentinel-token"] = json.dumps({
                    "p": sentinel_payload.p,
                    "t": sentinel_payload.t,
                    "c": sentinel_payload.c,
                    "id": self._device_id,
                    "flow": sentinel_payload.flow,
                }, separators=(",", ":"))



            response = self.session.post(

                OPENAI_API_ENDPOINTS["validate_otp"],

                headers=validate_headers,

                data=code_body,

                timeout=15,

            )



            self._log(f"验证码校验状态: {response.status_code}")

            if response.status_code != 200:

                server_code, server_message = _response_error(response)
                self._step_error_code = _classify_server_error(server_code, "otp_validation_failed")
                self._step_error_message = server_message or f"HTTP {response.status_code}"
                self._log(
                    f"验证码校验失败: code={server_code or '-'} HTTP {response.status_code}",
                    "warning",
                )

                return False



            # 保存完整的下一步状态；external_url 的目标通常嵌套在
            # page.payload.url，不能在该状态下直接调用 create_account。

            try:

                resp_data = response.json()

                if not isinstance(resp_data, dict):
                    resp_data = {}
                page = resp_data.get("page") if isinstance(resp_data.get("page"), dict) else {}
                payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
                self._otp_response_data = resp_data
                self._otp_continue_url = str(
                    resp_data.get("continue_url")
                    or payload.get("continue_url")
                    or payload.get("url")
                    or ""
                ).strip()
                self._otp_external_method = str(
                    resp_data.get("method") or payload.get("method") or "GET"
                ).upper()
                self._otp_page_type = str(page.get("type") or "").strip()

                self._log(f"验证码校验 -> page_type={self._otp_page_type}")

            except Exception:

                self._otp_continue_url = ""

                self._otp_page_type = ""

                self._otp_response_data = {}

                self._otp_external_method = "GET"

            return True



        except Exception as e:

            self._step_error_code = "proxy_network_error"
            self._step_error_message = str(e)
            self._log(f"验证验证码失败: {e}", "error")

            return False

    @staticmethod
    def _registration_page_type_from_url(url: str) -> str:
        """Infer only the registration states needed by the protocol flow."""
        parsed = urlsplit(str(url or ""))
        path = parsed.path.lower()
        query = parse_qs(parsed.query, keep_blank_values=True)
        if "code" in query and ("state" in query or "/api/auth/callback/" in path):
            return "oauth_callback"
        if "/about-you" in path:
            return "about_you"
        if "/api/oauth/oauth2/auth" in path:
            return "external_url"
        if parsed.hostname == urlsplit(CHATGPT_APP).hostname:
            return "chatgpt_home"
        return ""

    def _advance_external_registration_step(self, max_redirects: int = 10) -> bool:
        """Advance an OTP external handoff without consuming its OAuth callback."""
        if self._otp_external_method != "GET":
            self._step_error_code = "external_auth_method_unsupported"
            self._step_error_message = f"external_url method={self._otp_external_method}"
            self._log("外部授权交接不是 GET，请求已停止", "warning")
            return False

        current_url = urljoin(OPENAI_AUTH, str(self._otp_continue_url or "").strip())
        if not current_url:
            self._step_error_code = "external_auth_step_failed"
            self._step_error_message = "external_url 缺少目标 URL"
            return False

        allowed_hosts = {
            host
            for host in (
                urlsplit(OPENAI_AUTH).hostname,
                urlsplit(CHATGPT_APP).hostname,
            )
            if host
        }
        redirect_statuses = {301, 302, 303, 307, 308}

        for index in range(max_redirects):
            target_type = self._registration_page_type_from_url(current_url)
            if target_type == "oauth_callback":
                self._create_account_continue_url = current_url
                self._otp_page_type = target_type
                parsed = urlsplit(current_url)
                self._log(f"外部授权已到达 OAuth callback: {parsed.hostname}{parsed.path}")
                return True

            parsed = urlsplit(current_url)
            if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
                self._step_error_code = "external_auth_target_rejected"
                self._step_error_message = "external_url 目标主机不受支持"
                self._log("外部授权目标主机不受支持", "warning")
                return False

            self._log(
                f"推进外部授权[{index + 1}/{max_redirects}]: {parsed.hostname}{parsed.path}"
            )
            try:
                response = self.session.get(
                    current_url,
                    headers={
                        "referer": self._email_otp_continue_url or f"{OPENAI_AUTH}/email-verification",
                        "accept": "text/html,application/json,*/*",
                    },
                    allow_redirects=False,
                    timeout=20,
                )
            except Exception as exc:
                self._step_error_code = "proxy_network_error"
                self._step_error_message = str(exc)
                self._log(f"外部授权交接网络失败: {exc}", "warning")
                return False

            response_url = str(getattr(response, "url", "") or current_url)
            if response.status_code in redirect_statuses:
                location = str(
                    response.headers.get("Location")
                    or response.headers.get("location")
                    or ""
                ).strip()
                if not location:
                    self._step_error_code = "external_auth_step_failed"
                    self._step_error_message = "重定向响应缺少 Location"
                    return False
                current_url = urljoin(response_url, location)
                continue

            response_data = {}
            try:
                candidate = response.json()
                if isinstance(candidate, dict):
                    response_data = candidate
            except Exception:
                pass

            page = response_data.get("page") if isinstance(response_data.get("page"), dict) else {}
            payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
            page_type = str(page.get("type") or "").strip()
            next_url = str(
                response_data.get("continue_url")
                or payload.get("continue_url")
                or payload.get("url")
                or ""
            ).strip()
            next_method = str(
                response_data.get("method") or payload.get("method") or "GET"
            ).upper()
            effective_url = urljoin(response_url, next_url) if next_url else response_url
            page_type = page_type or self._registration_page_type_from_url(effective_url)

            if next_url and effective_url != response_url:
                if next_method != "GET":
                    self._step_error_code = "external_auth_method_unsupported"
                    self._step_error_message = f"external_url method={next_method}"
                    return False
                current_url = effective_url
                continue

            if page_type == "about_you":
                self._otp_page_type = "about_you"
                self._otp_continue_url = effective_url
                self._log("外部授权交接完成: page_type=about_you")
                return True
            if page_type == "oauth_callback":
                self._create_account_continue_url = effective_url
                self._otp_page_type = "oauth_callback"
                return True
            if page_type == "chatgpt_home" and self.session.cookies.get(
                "__Secure-next-auth.session-token"
            ):
                self._otp_page_type = "chatgpt_home"
                self._create_account_continue_url = effective_url
                self._log("外部授权已建立 ChatGPT session")
                return True

            self._step_error_code = "external_auth_step_failed"
            self._step_error_message = f"外部授权停在未知状态: {page_type or 'unknown'}"
            return False

        self._step_error_code = "external_auth_redirect_limit"
        self._step_error_message = f"外部授权超过 {max_redirects} 次跳转"
        self._log(self._step_error_message, "warning")
        return False



    def _create_user_account(self) -> bool:

        """创建用户账户"""

        try:

            user_info = generate_random_user_info()

            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")

            create_account_body = json.dumps(user_info)



            # 调 client_auth_session_dump 推进服务器 auth 状态机

            try:

                dump_resp = self.session.get(

                    "https://auth.openai.com/api/accounts/client_auth_session_dump",

                    headers={

                        "referer": "https://auth.openai.com/email-verification",

                        "accept": "application/json",

                    },

                    timeout=20,

                )

                self._log(f"client_auth_session_dump 状态: {dump_resp.status_code}")

            except Exception as e:

                self._log(f"client_auth_session_dump 异常: {e}", "warning")



            create_headers = {

                "referer": "https://auth.openai.com/about-you",

                "accept": "application/json",

                "content-type": "application/json",

                "origin": "https://auth.openai.com",

                "sec-fetch-site": "same-origin",

                **_generate_datadog_trace_headers(),

            }

            if self._device_id:

                create_headers["oai-device-id"] = self._device_id



            # create_account 也需要 sentinel token (flow=oauth_create_account)

            if self._device_id:

                ca_sentinel = self._check_sentinel(self._device_id, flow="oauth_create_account")

                if ca_sentinel:

                    create_headers["openai-sentinel-token"] = json.dumps({

                        "p": ca_sentinel.p,

                        "t": ca_sentinel.t,

                        "c": ca_sentinel.c,

                        "id": self._device_id,

                        "flow": ca_sentinel.flow,

                    }, separators=(",", ":"))

                    self._log(f"create_account Sentinel 已获取: flow={ca_sentinel.flow}")



            response = self.session.post(

                OPENAI_API_ENDPOINTS["create_account"],

                headers=create_headers,

                data=create_account_body,

            )



            self._log(f"账户创建状态: {response.status_code}")



            if response.status_code != 200:

                server_code, server_message = _response_error(response)
                self._step_error_code = _classify_server_error(server_code, "account_creation_failed")
                self._step_error_message = server_message or f"HTTP {response.status_code}"
                self._log(
                    f"账户创建失败: code={server_code or '-'} HTTP {response.status_code}",
                    "warning",
                )

                return False



            # 提取 continue_url（ChatGPT Web 流程直接返回 OAuth callback URL）

            try:

                resp_data = response.json()

                self._create_account_continue_url = resp_data.get("continue_url", "")

                if self._create_account_continue_url:

                    self._log("create_account 已返回 continue_url")

            except Exception:

                pass



            return True



        except Exception as e:

            self._step_error_code = "proxy_network_error"
            self._step_error_message = str(e)
            self._log(f"创建账户失败: {e}", "error")

            return False



    def _acquire_codex_callback(self) -> Optional[str]:

        """

        注册完成后，通过 Codex CLI OAuth 完整登录流程获取 callback URL。

        使用新 session，走 authorize → authorize/continue → OTP → callback 流程。

        """

        try:

            from .constants import (

                CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE,

                OPENAI_AUTH, OPENAI_API_ENDPOINTS,

            )

            import urllib.parse



            self._log("开始 Codex CLI 登录流程...")



            # 1. 创建新 HTTP client + session

            login_client = OpenAIHTTPClient(proxy_url=self.proxy_url)

            login_session = login_client.session



            # 2. 生成 Codex CLI OAuth URL (Hydra)

            codex_oauth = generate_oauth_url(

                redirect_uri=CODEX_REDIRECT_URI,

                scope=CODEX_SCOPE,

                client_id=CODEX_CLIENT_ID,

            )

            self._codex_oauth = codex_oauth



            # 3. 访问 authorize URL 获取 device_id + session cookies

            response = login_session.get(codex_oauth.auth_url, timeout=15)

            did = login_session.cookies.get("oai-did")

            self._log(f"Codex login device_id: {did}")

            if not did:

                self._log("Codex login 获取 device_id 失败", "error")

                return None



            # 4. 获取 Sentinel token

            sen_payload = None

            try:

                ua = login_client.default_headers.get("User-Agent", "")

                generator = _SentinelTokenGenerator(did, ua)

                sent_p = generator.generate_requirements_token()

                sen_req_body = json.dumps({"p": sent_p, "id": did, "flow": "authorize_continue"}, separators=(",", ":"))



                from .constants import SENTINEL_FRAME_URL

                sen_resp = login_client.post(

                    OPENAI_API_ENDPOINTS["sentinel"],

                    headers={

                        "origin": "https://sentinel.openai.com",

                        "referer": SENTINEL_FRAME_URL,

                        "content-type": "text/plain;charset=UTF-8",

                    },

                    data=sen_req_body,

                )

                if sen_resp.status_code == 200:

                    data = sen_resp.json()

                    turnstile = data.get("turnstile") or {}

                    pow_meta = data.get("proofofwork") or {}

                    if pow_meta.get("required") and pow_meta.get("seed"):

                        sent_p = generator.generate_token(

                            str(pow_meta.get("seed") or ""),

                            str(pow_meta.get("difficulty") or "0"),

                        )

                    t_raw = turnstile.get("dx", "")

                    t_val = ""

                    if t_raw:

                        try:

                            t_val = generator.decrypt_turnstile(t_raw, sent_p)

                        except Exception:

                            pass

                    sen_payload = SentinelPayload(p=sent_p, t=t_val, c=str(data.get("token") or ""), flow="authorize_continue")

                    self._log("Codex login Sentinel 已获取")

            except Exception as e:

                self._log(f"Codex login Sentinel 失败: {e}", "warning")



            # 5. authorize/continue 提交邮箱（登录已有账号）

            signup_body = f'{{"username":{{"value":"{self.email}","kind":"email"}},"screen_hint":"login"}}'

            headers = {

                "referer": "https://auth.openai.com/log-in",

                "accept": "application/json",

                "content-type": "application/json",

            }

            if sen_payload:

                headers["openai-sentinel-token"] = json.dumps({

                    "p": sen_payload.p, "t": sen_payload.t, "c": sen_payload.c,

                    "id": did, "flow": sen_payload.flow,

                }, separators=(",", ":"))



            resp = login_session.post(OPENAI_API_ENDPOINTS["signup"], headers=headers, data=signup_body)

            self._log(f"Codex login authorize/continue: {resp.status_code}")

            if resp.status_code != 200:

                self._log(f"Codex login authorize/continue 失败: {resp.text[:200]}", "error")

                return None



            resp_data = resp.json()

            page_type = resp_data.get("page", {}).get("type", "")

            self._log(f"Codex login page_type: {page_type}")



            # 6. 如果需要 OTP，等待第二次验证码

            if page_type == "email_otp_verification":
                self._begin_new_otp_wait()
                if not self._request_email_otp_delivery(
                    session=login_session,
                    referer=f"{OPENAI_AUTH}/email-verification",
                    prefer_resend=True,
                    include_sentinel=False,
                ):
                    self._log("Codex login OTP 投递未获服务端确认", "error")
                    return None

                self._log("等待第二次验证码...")

                code = self._get_verification_code()

                if not code:

                    self._log("Codex login 获取验证码失败", "error")

                    return None



                # 验证 OTP

                code_body = f'{{"code":"{code}"}}'

                otp_resp = login_session.post(

                    OPENAI_API_ENDPOINTS["validate_otp"],

                    headers={

                        "referer": "https://auth.openai.com/email-verification",

                        "accept": "application/json",

                        "content-type": "application/json",

                    },

                    data=code_body,

                )

                self._log(f"Codex login OTP 校验: {otp_resp.status_code}")

                if otp_resp.status_code != 200:

                    self._log(f"Codex login OTP 失败: {otp_resp.text[:200]}", "error")

                    return None



                otp_data = otp_resp.json()

                otp_page = otp_data.get("page", {}).get("type", "")

                self._log(f"Codex login OTP -> page_type={otp_page}")



                if otp_page == "add_phone":

                    self._log("Codex CLI 登录仍需 add_phone，无法跳过", "error")

                    return None



            # 7. 需要密码登录

            elif page_type in ("login_password", "create_account_password"):

                self._log(f"Codex login 提交密码...")

                if not self.password:

                    self._log("无密码可用", "error")

                    return None



                # 加载密码页获取 sentinel

                login_session.get(f"{OPENAI_AUTH}/log-in/password", timeout=15)

                pwd_sentinel = None

                try:

                    ua2 = login_client.default_headers.get("User-Agent", "")

                    gen2 = _SentinelTokenGenerator(did, ua2)

                    sp2 = gen2.generate_requirements_token()

                    sr2 = json.dumps({"p": sp2, "id": did, "flow": "login_password"}, separators=(",", ":"))

                    from .constants import SENTINEL_FRAME_URL as SF2

                    sr2_resp = login_client.post(

                        OPENAI_API_ENDPOINTS["sentinel"],

                        headers={"origin": "https://sentinel.openai.com", "referer": SF2, "content-type": "text/plain;charset=UTF-8"},

                        data=sr2,

                    )

                    if sr2_resp.status_code == 200:

                        d2 = sr2_resp.json()

                        pm2 = d2.get("proofofwork") or {}

                        if pm2.get("required") and pm2.get("seed"):

                            sp2 = gen2.generate_token(str(pm2.get("seed") or ""), str(pm2.get("difficulty") or "0"))

                        tr2 = (d2.get("turnstile") or {}).get("dx", "")

                        tv2 = ""

                        if tr2:

                            try: tv2 = gen2.decrypt_turnstile(tr2, sp2)

                            except: pass

                        pwd_sentinel = SentinelPayload(p=sp2, t=tv2, c=str(d2.get("token") or ""), flow="login_password")

                        self._log("Codex login 密码 Sentinel 已获取")

                except Exception as e:

                    self._log(f"Codex login 密码 Sentinel 失败: {e}", "warning")



                pwd_headers = {

                    "origin": OPENAI_AUTH,

                    "referer": f"{OPENAI_AUTH}/log-in/password",

                    "accept": "application/json",

                    "content-type": "application/json",

                }

                if did:

                    pwd_headers["oai-device-id"] = did

                if pwd_sentinel:

                    pwd_headers["openai-sentinel-token"] = json.dumps({

                        "p": pwd_sentinel.p, "t": pwd_sentinel.t, "c": pwd_sentinel.c,

                        "id": did, "flow": pwd_sentinel.flow,

                    }, separators=(",", ":"))



                pwd_body = json.dumps({"password": self.password, "username": self.email})

                pwd_resp = login_session.post(OPENAI_API_ENDPOINTS["register"], headers=pwd_headers, data=pwd_body)

                self._log(f"Codex login 密码提交: {pwd_resp.status_code}")

                if pwd_resp.status_code != 200:

                    self._log(f"Codex login 密码失败: {pwd_resp.text[:200]}", "error")

                    return None



                pwd_data = pwd_resp.json()

                pwd_page = pwd_data.get("page", {}).get("type", "")

                self._log(f"Codex login 密码 -> page_type={pwd_page}")



                # 密码后可能需要 OTP

                if pwd_page == "email_otp_verification" or pwd_page == "email_otp_send":
                    self._begin_new_otp_wait()
                    if not self._request_email_otp_delivery(
                        session=login_session,
                        referer=f"{OPENAI_AUTH}/email-verification",
                        prefer_resend=True,
                        include_sentinel=False,
                    ):
                        self._log("Codex login OTP 投递未获服务端确认", "error")
                        return None

                    self._log("Codex login: 等待验证码...")

                    code = self._get_verification_code()

                    if not code:

                        self._log("Codex login 获取验证码失败", "error")

                        return None

                    code_body = f'{{"code":"{code}"}}'

                    otp_resp = login_session.post(

                        OPENAI_API_ENDPOINTS["validate_otp"],

                        headers={"referer": f"{OPENAI_AUTH}/email-verification", "accept": "application/json", "content-type": "application/json"},

                        data=code_body,

                    )

                    self._log(f"Codex login OTP: {otp_resp.status_code}")

                    if otp_resp.status_code != 200:

                        self._log(f"Codex login OTP 失败: {otp_resp.text[:200]}", "error")

                        return None

                    otp_data = otp_resp.json()

                    otp_page = otp_data.get("page", {}).get("type", "")

                    self._log(f"Codex login OTP -> page_type={otp_page}")

                    if otp_page == "add_phone":

                        self._log("Codex CLI 登录仍需 add_phone", "error")

                        return None



            # 8. 重新访问 authorize URL 获取回调

            self._log("Codex login: 重新访问 OAuth URL 获取回调...")

            response = login_session.get(codex_oauth.auth_url, allow_redirects=False, timeout=15)

            max_redirects = 10

            current_url = codex_oauth.auth_url

            for i in range(max_redirects):

                if response.status_code not in (301, 302, 303, 307, 308):

                    break

                location = response.headers.get("Location", "")

                if not location:

                    break

                next_url = urllib.parse.urljoin(current_url, location)

                self._log(f"Codex login 重定向 {i+1}: {next_url[:80]}...")

                if "code=" in next_url and "state=" in next_url:

                    self._log("找到 Codex CLI 回调 URL")

                    return next_url

                current_url = next_url

                response = login_session.get(current_url, allow_redirects=False, timeout=15)



            self._log(f"Codex login 最终: status={response.status_code}, url={current_url[:100]}", "warning")

            return None



        except Exception as e:

            self._log(f"Codex CLI 登录流程失败: {e}", "error")

            return None



    def _get_workspace_id(self) -> Optional[str]:

        """获取 Workspace ID"""

        try:

            auth_cookie = self.session.cookies.get("oai-client-auth-session")

            if not auth_cookie:

                self._log("未能获取到授权 Cookie", "error")

                return None



            # 解码 JWT

            import base64

            import json as json_module



            try:

                segments = auth_cookie.split(".")

                if len(segments) < 1:

                    self._log("授权 Cookie 格式错误", "error")

                    return None



                # 解码第一个 segment

                payload = segments[0]

                pad = "=" * ((4 - (len(payload) % 4)) % 4)

                decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))

                auth_json = json_module.loads(decoded.decode("utf-8"))



                workspaces = auth_json.get("workspaces") or []

                if not workspaces:

                    self._log("授权 Cookie 里没有 workspace 信息", "error")

                    return None



                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()

                if not workspace_id:

                    self._log("无法解析 workspace_id", "error")

                    return None



                self._log(f"Workspace ID: {workspace_id}")

                return workspace_id



            except Exception as e:

                self._log(f"解析授权 Cookie 失败: {e}", "error")

                return None



        except Exception as e:

            self._log(f"获取 Workspace ID 失败: {e}", "error")

            return None



    def _select_workspace(self, workspace_id: str) -> Optional[str]:

        """选择 Workspace"""

        try:

            select_body = f'{{"workspace_id":"{workspace_id}"}}'



            response = self.session.post(

                OPENAI_API_ENDPOINTS["select_workspace"],

                headers={

                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",

                    "content-type": "application/json",

                },

                data=select_body,

            )



            if response.status_code != 200:

                self._log(f"选择 workspace 失败: {response.status_code}", "error")

                self._log(f"响应: {response.text[:200]}", "warning")

                return None



            continue_url = str((response.json() or {}).get("continue_url") or "").strip()

            if not continue_url:

                self._log("workspace/select 响应里缺少 continue_url", "error")

                return None



            self._log(f"Continue URL: {continue_url[:100]}...")

            return continue_url



        except Exception as e:

            self._log(f"选择 Workspace 失败: {e}", "error")

            return None



    def _follow_redirects(self, start_url: str) -> Optional[str]:

        """跟随重定向链，寻找回调 URL"""

        try:

            current_url = start_url

            max_redirects = 6



            for i in range(max_redirects):

                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")



                response = self.session.get(

                    current_url,

                    allow_redirects=False,

                    timeout=15

                )



                location = response.headers.get("Location") or ""



                # 如果不是重定向状态码，停止

                if response.status_code not in [301, 302, 303, 307, 308]:

                    self._log(f"非重定向状态码: {response.status_code}")

                    break



                if not location:

                    self._log("重定向响应缺少 Location 头")

                    break



                # 构建下一个 URL

                import urllib.parse

                next_url = urllib.parse.urljoin(current_url, location)



                # 检查是否包含回调参数

                if "code=" in next_url and "state=" in next_url:

                    self._log(f"找到回调 URL: {next_url[:100]}...")

                    return next_url



                current_url = next_url



            self._log("未能在重定向链中找到回调 URL", "error")

            return None



        except Exception as e:

            self._log(f"跟随重定向失败: {e}", "error")

            return None



    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:

        """处理 OAuth 回调"""

        try:

            if not self.oauth_start:

                self._log("OAuth 流程未初始化", "error")

                return None



            self._log("处理 OAuth 回调...")

            token_info = self.oauth_manager.handle_callback(

                callback_url=callback_url,

                expected_state=self.oauth_start.state,

                code_verifier=self.oauth_start.code_verifier

            )



            self._oauth_token_info = dict(token_info or {})
            self._log(
                "OAuth 授权成功: "
                f"access_token={'有' if self._oauth_token_info.get('access_token') else '无'}, "
                f"refresh_token={'有' if self._oauth_token_info.get('refresh_token') else '无'}"
            )

            return token_info



        except Exception as e:

            self._log(f"处理 OAuth 回调失败: {e}", "error")

            return None



    def run(self) -> RegistrationResult:

        """

        执行完整的注册流程



        支持已注册账号自动登录：

        - 如果检测到邮箱已注册，自动切换到登录流程

        - 已注册账号跳过：设置密码、发送验证码、创建用户账户

        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调



        Returns:

            RegistrationResult: 注册结果

        """

        result = RegistrationResult(success=False, logs=self.logs)

        self._step_error_code = ""
        self._step_error_message = ""



        try:

            self._log("=" * 60)

            self._log("开始注册流程")

            self._log("=" * 60)



            # 1. 检查 IP 地理位置

            self._log("1. 检查 IP 地理位置...")

            ip_ok, location = self._check_ip_location()

            if not ip_ok:

                result.error_message = f"IP 地理位置不支持: {location}"

                result.error_code = "unsupported_region" if location else "proxy_network_error"

                self._log(f"IP 检查失败: {location}", "error")

                return result



            self._log(f"IP 位置: {location}")



            # 2. 创建邮箱

            self._log("2. 创建邮箱...")

            if not self._create_email():

                result.error_message = "创建邮箱失败"

                return result



            result.email = self.email



            # 3. 初始化会话

            self._log("3. 初始化会话...")

            if not self._init_session():

                result.error_message = "初始化会话失败"

                return result



            # 4. 开始 OAuth 流程

            self._log("4. 开始 OAuth 授权流程...")

            if not self._start_oauth():

                detail = str(getattr(self, "_step_error_message", "") or "").strip()
                result.error_message = f"开始 OAuth 流程失败: {detail}" if detail else "开始 OAuth 流程失败"

                result.error_code = getattr(self, "_step_error_code", "") or "oauth_start_failed"

                return result



            # 5. 获取 Device ID

            self._log("5. 获取 Device ID...")

            did = self._get_device_id()

            if not did:

                detail = str(getattr(self, "_step_error_message", "") or "").strip()
                result.error_message = f"获取 Device ID 失败: {detail}" if detail else "获取 Device ID 失败"

                result.error_code = getattr(self, "_step_error_code", "") or "oauth_authorize_failed"

                return result



            # 6. 检查 Sentinel 拦截

            self._log("6. 检查 Sentinel 拦截...")

            sen_payload = self._check_sentinel(did)

            if sen_payload:

                self._signup_sentinel = sen_payload
                self._log("Sentinel 检查通过")

            else:

                self._log("Sentinel 检查失败或未启用", "warning")



            # OAuth passwordless flow has already advanced to email OTP.  A
            # second authorize/continue would consume the one-time auth state.
            if self.email_otp_first and self._oauth_email_verification:
                self._log("7. OAuth 已建立邮箱验证状态，跳过重复提交注册表单")
                signup_page_type = OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
            else:
                self._log("7. 提交注册表单...")
                signup_result = self._submit_signup_form(did, sen_payload)

                if (
                    not signup_result.success
                    and signup_result.error_code == "oauth_invalid_state"
                ):
                    self._log("OAuth 授权态失效，重新建立完整会话后重试一次", "warning")
                    if not self._reset_http_session() or not self._start_oauth():
                        result.error_message = "OAuth 授权态重建失败"
                        result.error_code = "oauth_invalid_state"
                        return result
                    did = self._get_device_id()
                    if not did:
                        result.error_message = "OAuth 授权态重建后获取 Device ID 失败"
                        result.error_code = "oauth_invalid_state"
                        return result
                    sen_payload = self._check_sentinel(did)
                    signup_result = self._submit_signup_form(did, sen_payload)

                if not signup_result.success:
                    result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                    result.error_code = signup_result.error_code or "signup_failed"
                    return result

                signup_page_type = signup_result.page_type or ""



            # 8. 根据授权页状态决定是否需要密码步骤

            if signup_page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:

                self._log("8. 已进入邮箱验证码流程，跳过密码设置")

            elif self._is_existing_account:

                self._log("8. [已注册账号] 跳过密码设置")

            else:

                self._log("8. 注册密码...")

                password_ok, password = self._register_password()

                if not password_ok:

                    result.error_message = "注册密码失败"

                    return result



            # 9. 发送验证码（协议模式没有浏览器 JS 自动触发，必须显式调用 API）

            if self.email_otp_first and self._oauth_email_verification:
                self._log("9. 已保留 OAuth 邮箱 challenge，显式确认 OTP 投递...")
                if not self._send_verification_code():
                    result.error_message = "邮箱 OTP 投递失败"
                    result.error_code = getattr(self, "_step_error_code", "") or "otp_delivery_failed"
                    return result
            elif self._is_existing_account:

                self._log("9. [已注册账号] 重发并确认登录验证码...")

                if not self._send_verification_code():
                    result.error_message = "邮箱 OTP 投递失败"
                    result.error_code = getattr(self, "_step_error_code", "") or "otp_delivery_failed"
                    return result
            else:

                self._log("9. 发送验证码...")

                if not self._send_verification_code():
                    result.error_message = "邮箱 OTP 投递失败"
                    result.error_code = getattr(self, "_step_error_code", "") or "otp_delivery_failed"
                    return result



            # 10. 获取验证码

            self._log("10. 等待验证码...")

            code = self._get_verification_code()

            if not code:

                result.error_message = getattr(self, "_last_otp_error", "") or "获取验证码失败"

                result.error_code = "mailbox_otp_fetch_failed"

                return result

            if self.otp_submit_delay > 0:
                self._log(f"验证码已获取，等待 {self.otp_submit_delay:g}s 后提交验证...")
                time.sleep(self.otp_submit_delay)



            # 11. 验证验证码

            self._log("11. 验证验证码...")

            if not self._validate_verification_code(code):

                result.error_code = getattr(self, "_step_error_code", "") or "otp_validation_failed"
                detail = getattr(self, "_step_error_message", "")
                result.error_message = f"验证验证码失败: {detail}" if detail else "验证验证码失败"

                return result



            # 12. 根据 OTP 响应决定下一步

            if self._otp_page_type == "external_url":

                self._log("12. 跟随 OTP 返回的外部授权交接...")

                if not self._advance_external_registration_step():

                    result.error_code = getattr(self, "_step_error_code", "") or "external_auth_step_failed"
                    detail = getattr(self, "_step_error_message", "")
                    result.error_message = f"推进外部授权失败: {detail}" if detail else "推进外部授权失败"

                    return result

            if self._otp_page_type == "about_you" and not self._is_existing_account:

                # 正常注册流程: about_you → create_account

                self._log("12. 创建用户账户...")

                if not self._create_user_account():

                    result.error_code = getattr(self, "_step_error_code", "") or "account_creation_failed"
                    detail = getattr(self, "_step_error_message", "")
                    result.error_message = f"创建用户账户失败: {detail}" if detail else "创建用户账户失败"

                    return result

            elif self._is_existing_account:

                self._log("12. [已注册账号] 跳过创建用户账户")

            elif self._otp_page_type in {"oauth_callback", "chatgpt_home"}:

                self._log(f"12. 授权已进入 {self._otp_page_type}，跳过重复创建账户")

            else:

                result.error_code = "unexpected_otp_page_type"
                result.error_message = f"OTP 后进入未知状态: {self._otp_page_type or 'unknown'}"

                return result



            # 13. 跟随 callback URL 到 chatgpt.com 获取 session

            callback_url = self._create_account_continue_url

            existing_session_token = self.session.cookies.get("__Secure-next-auth.session-token")

            if (not callback_url or "code=" not in str(callback_url)) and not existing_session_token:

                result.error_message = "create_account 未返回有效的 callback URL"

                result.error_code = "account_created_session_missing"

                return result



            if callback_url and "code=" in str(callback_url):

                self._log("13. 跟随 callback URL 到 chatgpt.com...")

                cb_resp = self.session.get(callback_url, timeout=20)

                self._log(f"callback 状态: {cb_resp.status_code}")

            else:

                self._log("13. 外部授权已建立 ChatGPT session，跳过重复 callback")



            # 提取 session cookie

            session_token = self.session.cookies.get("__Secure-next-auth.session-token")

            account_cookie = self.session.cookies.get("_account", "")

            if session_token:

                self._log("获取到 session-token")

            if account_cookie:

                self._log("获取到 _account cookie")



            # 14. 从 chatgpt.com/api/auth/session 获取 access_token

            from .constants import CHATGPT_APP

            self._log("14. 获取 session 信息...")

            session_resp = self.session.get(

                f"{CHATGPT_APP}/api/auth/session",

                headers={"accept": "application/json"},

                timeout=15,

            )

            self._log(f"session API 状态: {session_resp.status_code}")

            session_data = session_resp.json()
            token_data = _extract_chatgpt_session_credentials(
                session_data,
                getattr(self, "_oauth_token_info", None),
            )
            access_token = token_data["access_token"]

            user_data = session_data.get("user", {}) if isinstance(session_data, dict) else {}

            self._log(
                f"session keys: {list(session_data.keys()) if isinstance(session_data, dict) else []}"
            )

            self._log(f"accessToken 长度: {len(access_token)}")



            if not access_token:

                result.error_message = "chatgpt.com session 未返回 accessToken"

                result.error_code = "account_created_session_missing"

                return result



            self._log("NextAuth session 获取成功")



            # Codex CLI/RT/Workspace Join 已从 free 注册主流程移除
            # 提取账户信息（使用 NextAuth session token）
            # Codex CLI / RT 获取等功能已移除，作为独立任务执行

            self._log("使用 NextAuth session token")

            session_account = session_data.get("account") if isinstance(session_data, dict) else None
            session_account_id = ""
            if isinstance(session_account, dict):
                session_account_id = str(
                    session_account.get("id")
                    or session_account.get("account_id")
                    or session_account.get("accountId")
                    or ""
                ).strip()
            result.account_id = (
                _extract_chatgpt_account_id(access_token)
                or session_account_id
                or str(account_cookie or "").strip()
                or _token_value(
                    getattr(self, "_oauth_token_info", None),
                    "account_id",
                    "accountId",
                )
            )

            result.access_token = access_token
            result.refresh_token = token_data["refresh_token"]
            result.id_token = token_data["id_token"]

            if result.refresh_token:
                self._log(
                    "获取到 refresh_token（仅记录存在性，未输出原文）: "
                    f"source={token_data['refresh_token_source']}"
                )
            else:
                self._log(
                    "当前 session 未下发 refresh_token，将使用 session_token 作为刷新路径",
                    "warning",
                )



            result.password = self.password or ""

            result.source = "login" if self._is_existing_account else "register"



            if session_token:

                self.session_token = session_token

                result.session_token = session_token

                self._log(f"获取到 Session Token")



            # 17. 完成

            self._log("=" * 60)

            if self._is_existing_account:

                self._log("登录成功! (已注册账号)")

            else:

                # This engine only establishes the base web account/session.
                # Task-level phone, Codex credential, liveness, and delivery
                # gates may still follow, so do not publish a misleading final
                # success message here.
                self._log("基础账号会话创建完成")

            self._log(f"邮箱: {result.email}")

            self._log(f"Account ID: {result.account_id}")

            self._log(f"Workspace ID: {result.workspace_id}")

            self._log("=" * 60)



            result.success = True

            result.metadata = {

                "email_service": self.email_service.service_type.value,

                "proxy_used": self.proxy_url,

                "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),

                "is_existing_account": self._is_existing_account,

                "session": session_data if isinstance(session_data, dict) else {},

                "expires_at": token_data["expires_at"],

                "access_token_expires_at": token_data["expires_at"],

                "refresh_token_status": token_data["refresh_token_status"],

                "refresh_token_source": token_data["refresh_token_source"],

                "session_token_present": bool(session_token),

                "refresh_token_present": bool(result.refresh_token),

            }



            return result



        except Exception as e:

            self._log(f"注册过程中发生未预期错误: {e}", "error")

            result.error_message = str(e)

            if not result.error_code:
                result.error_code = (
                    "account_created_session_missing"
                    if self._create_account_continue_url
                    else getattr(self, "_step_error_code", "") or "registration_failed"
                )

            return result



    def save_to_database(self, result: RegistrationResult) -> bool:

        """

        保存注册结果到数据库



        Args:

            result: 注册结果



        Returns:

            是否保存成功

        """

        if not result.success:

            return False



        return True  # 由 account_manager 统一处理存库

