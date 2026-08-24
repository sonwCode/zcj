"""ChatGPT / Codex CLI 平台插件"""
import json
import os
import re
import secrets
import threading
import time
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from core.proxy_pool import proxy_pool
from platforms._browser_backend import BrowserBackendConfig, resolve_runtime_browser_mode


_REMOTE_INVALID_MARKERS = (
    "deleted",
    "deactivated",
    "account_disabled",
    "account deactivated",
    "account has been deleted",
    "invalid_api_key",
    "invalid token",
    "token expired",
    "unauthorized",
)


def _check_error_http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    raw_status = getattr(response, "status_code", None)
    try:
        if raw_status is not None:
            return int(raw_status)
    except (TypeError, ValueError):
        pass
    match = re.search(r"(?:HTTP(?:/\S+)?\s*|status(?:_code)?[=: ]+)([1-5]\d{2})\b", str(exc), re.I)
    return int(match.group(1)) if match else None


def _is_cloudflare_challenge(exc: Exception) -> bool:
    """Return whether a 403 is an edge challenge, not an account verdict."""
    if _check_error_http_status(exc) != 403:
        return False
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    normalized = {str(key).lower(): str(value).lower() for key, value in headers.items()}
    if normalized.get("cf-mitigated") == "challenge":
        return True
    return (
        normalized.get("server") == "cloudflare"
        and "text/html" in normalized.get("content-type", "")
    )


def _is_explicit_remote_invalid(exc: Exception) -> tuple[bool, int | None]:
    status = _check_error_http_status(exc)
    message = str(exc or "").lower()
    if _is_cloudflare_challenge(exc):
        return False, status
    return status in {401, 403} or any(marker in message for marker in _REMOTE_INVALID_MARKERS), status


def _compact_check_error(exc: Exception) -> str:
    message = re.sub(r"\s+", " ", str(exc or exc.__class__.__name__)).strip()
    return message[:500]


def _remote_check_error_fields(exc: Exception) -> dict:
    """Extract non-secret diagnostics from a failed ChatGPT liveness call."""
    fields: dict[str, object] = {}
    status = _check_error_http_status(exc)
    if status is not None:
        fields["validity_http_status"] = status

    response = getattr(exc, "response", None)
    if response is None:
        return fields

    url = str(getattr(response, "url", "") or "")
    if "/backend-api/me" in url:
        fields["check_source"] = "backend-api/me"
    elif "/backend-api/wham/usage" in url:
        fields["check_source"] = "backend-api/wham/usage"

    headers = getattr(response, "headers", None) or {}
    normalized_headers = {
        str(key).lower(): str(value).lower()
        for key, value in headers.items()
    }
    if (
        normalized_headers.get("cf-mitigated") == "challenge"
        or (
            status == 403
            and normalized_headers.get("server") == "cloudflare"
            and "text/html" in normalized_headers.get("content-type", "")
        )
    ):
        fields["validity_error_code"] = "cloudflare_challenge"
    request_id = headers.get("x-request-id") or headers.get("X-Request-Id")
    cf_ray = headers.get("cf-ray") or headers.get("CF-Ray")
    if request_id:
        fields["validity_request_id"] = str(request_id)[:200]
    if cf_ray:
        fields["validity_cf_ray"] = str(cf_ray)[:200]

    try:
        payload = response.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return fields

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        error_type = error.get("type")
        message = error.get("message")
        if code not in (None, ""):
            fields["validity_error_code"] = str(code)[:200]
        if error_type not in (None, ""):
            fields["validity_error_type"] = str(error_type)[:200]
        if message not in (None, ""):
            fields["validity_error_message"] = str(message)[:500]
    elif isinstance(error, str) and error:
        fields["validity_error_message"] = error[:500]

    if not fields.get("validity_error_message"):
        for key in ("detail", "message"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)) and str(value):
                fields["validity_error_message"] = str(value)[:500]
                break
    return fields


def _remote_invalid_reason(fields: dict, *, refresh_attempted: bool) -> str:
    code = str(fields.get("validity_error_code") or "").strip().lower()
    if code in {"token_invalidated", "token_revoked"}:
        suffix = "，session 刷新复验仍被拒绝，需要重新登录" if refresh_attempted else ""
        return f"远端认证 token 已撤销{suffix}"
    if code in {"account_deactivated", "account_disabled", "account_deleted"}:
        return "远端账号已停用"
    status = fields.get("validity_http_status")
    if status == 401:
        suffix = "，session 刷新复验仍失败" if refresh_attempted else ""
        return f"远端认证失败 (HTTP 401){suffix}"
    if status == 403:
        return "远端拒绝账号访问 (HTTP 403)"
    return "远端认证被拒绝，账号凭证当前不可用"


def _result_text(result, key: str) -> str:
    if isinstance(result, dict):
        return str(result.get(key, "") or "")
    return str(getattr(result, key, "") or "")


def _assert_complete_oauth_callback(result) -> None:
    # NextAuth 流程只返回 account_id + access_token (+ session_token)
    # 传统 Codex CLI 流程返回全部 4 个字段
    required = ("account_id", "access_token")
    missing = [key for key in required if not _result_text(result, key)]
    if missing:
        raise RuntimeError(
            "ChatGPT 注册未完成完整 OAuth callback，缺少: " + ", ".join(missing)
        )


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _int_param(params: dict, key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _optional_int_param(params: dict, key: str) -> int | None:
    value = params.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mask_proxy(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value or "@" not in value:
        return value
    prefix, _, host = value.rpartition("@")
    scheme, sep, _credentials = prefix.partition("://")
    return f"{scheme}{sep}***@{host}" if sep else f"***@{host}"


def _build_checkout_har_path(email: str) -> str:
    """为 Camoufox checkout 生成 HAR 文件路径：tools/captures/checkout-<ts>-<email-slug>.har"""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    capture_dir = os.path.join(project_root, "tools", "captures")
    os.makedirs(capture_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(email or "anon")).strip("_") or "anon"
    return os.path.join(capture_dir, f"checkout-{timestamp}-{slug}.har")


def _build_get_rt_har_path(email: str) -> str:
    """Build a HAR output path for get_rt Camoufox OAuth captures."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    capture_dir = os.path.join(project_root, "tools", "captures")
    os.makedirs(capture_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(email or "anon")).strip("_") or "anon"
    return os.path.join(capture_dir, f"get-rt-{timestamp}-{slug}.har")


def _run_sync_checkout_isolated(
    checkout_fn,
    *,
    thread_name: str = "chatgpt-paypal-checkout",
    _log_fn=None,
    timeout_sec: int | None = None,
    **kwargs,
):
    """把同步浏览器函数丢进独立线程跑，避免阻塞外层 asyncio loop / 任务线程。

    **subtask 标签透传**：外层 ``logger.log`` 用 thread-local 标签把日志
    分组到对应的 worker（前端按这个折叠）。子线程是新线程，thread-local
    天然是空的，所以这里在父线程从 ``log_fn`` 上抠出当前绑定的
    subtask（如果是 ``TaskLogger.log``），子线程进去再 set 一遍，最后
    finally 清掉。
    """
    result_box = {}
    error_box = {}

    # 尝试从 log_fn 上抠出 TaskLogger 实例和当前 subtask（best-effort）
    log_fn = _log_fn if _log_fn is not None else kwargs.get("log_fn")
    parent_logger = getattr(log_fn, "__self__", None)
    parent_subtask: tuple[str, str] | None = None
    if parent_logger is not None and hasattr(parent_logger, "_current_subtask"):
        try:
            parent_subtask = parent_logger._current_subtask()
        except Exception:
            parent_subtask = None

    def _target():
        # 把父线程的 subtask 标签复制到子线程的 thread-local，确保子线程里
        # 调 ``logger.log`` 也能正确分组。
        if parent_logger is not None and parent_subtask and parent_subtask[0]:
            try:
                parent_logger.set_subtask(parent_subtask[0], parent_subtask[1])
            except Exception:
                pass
        try:
            result_box["result"] = checkout_fn(**kwargs)
        except BaseException as exc:
            error_box["error"] = exc
        finally:
            if parent_logger is not None and parent_subtask and parent_subtask[0]:
                try:
                    parent_logger.clear_subtask()
                except Exception:
                    pass

    thread = threading.Thread(target=_target, name=thread_name)
    if timeout_sec and timeout_sec > 0:
        thread.daemon = True
    thread.start()
    thread.join(timeout=timeout_sec if timeout_sec and timeout_sec > 0 else None)
    if thread.is_alive():
        if callable(log_fn):
            try:
                log_fn(f"{thread_name} 超过 {timeout_sec}s 仍未返回，判定为浏览器启动/执行卡住")
            except Exception:
                pass
        raise TimeoutError(f"{thread_name} timeout after {timeout_sec}s")
    if error_box:
        raise error_box["error"]
    return result_box.get("result")


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """生成更稳定通过 OpenAI 注册页校验的密码。

    旧协议流已经验证过：至少带小写、数字、符号时，成功率明显更稳。
    这里再补一个大写字符，避免浏览器流随机生成出“看起来够长但组合不够强”的密码。
    """
    specials = ",._!@#"
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    required = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + specials
    required.extend(secrets.choice(pool) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "phone", "oauth_browser"]
    supported_oauth_providers = ["google", "microsoft"]
    protocol_captcha_order = ("yescaptcha_api", "twocaptcha_api", "local_solver")

    # Declarative capabilities
    capabilities = [
        "query_state",      # Query account state/quota
        "refresh_token",    # Refresh auth token
        "generate_link",    # Generate payment link
        "switch_desktop",   # Switch to Codex desktop
        "upload_cpa",       # Upload to CPA system
        "upload_tm",        # Upload to Team Manager
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
        self._last_check_credential_updates = {}
        check_errors: list[Exception] = []
        terminal_errors: list[Exception] = []
        transient_errors: list[Exception] = []
        refresh_attempted = False
        refresh_succeeded = False
        refresh_error = ""
        refresh_error_code = ""
        try:
            from platforms.chatgpt.payment import fetch_subscription_status_details
            from core.proxy_pool import proxy_pool
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.id_token = extra.get("id_token", "")
            a.cookies = extra.get("cookies", "")
            a.extra = extra

            # Keep the ChatGPT Web access token distinct from the Codex PKCE
            # access token.  Older rows may only have one of them, so probe all
            # unique candidates before deciding that authentication is dead.
            token_candidates: list[tuple[str, str]] = []
            seen_tokens: set[str] = set()
            for source, value in (
                ("web_access_token", extra.get("web_access_token")),
                ("access_token", extra.get("access_token")),
                ("primary_token", account.token),
            ):
                token = str(value or "").strip()
                if token and token not in seen_tokens:
                    seen_tokens.add(token)
                    token_candidates.append((source, token))

            region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
            auth_proxy = str(extra.get("auth_proxy_url") or "").strip()
            configured_proxy = self.config.proxy if self.config else None
            proxy_candidates: list[tuple[str | None, bool]] = []
            if auth_proxy:
                proxy_candidates.append((auth_proxy, False))
            elif configured_proxy:
                proxy_candidates.append((configured_proxy, False))
            else:
                pooled_proxy = proxy_pool.get_next(region=region)
                if pooled_proxy:
                    proxy_candidates.append((pooled_proxy, True))
            if not auth_proxy:
                proxy_candidates.append((None, False))

            def _finish_success(
                details: dict,
                *,
                token: str,
                token_source: str,
                recovered_by_session: bool,
            ) -> bool:
                status = details.get("status")
                # 把订阅状态同步映射成前端能用的 plan_state / chips
                # 来源（避免老 chips 还带 "Plus" 但实际已 free）。
                if status == "plus":
                    plan_state = "subscribed"
                    chips = ["Plus"]
                elif status == "team":
                    plan_state = "subscribed"
                    chips = ["Team"]
                elif status == "free":
                    plan_state = "free"
                    chips = ["Free"]
                elif status in ("expired", "invalid", "banned"):
                    plan_state = "expired"
                    chips = []
                else:
                    plan_state = "unknown"
                    chips = []
                remote_valid = status not in ("expired", "invalid", "banned", None)
                validity_status = (
                    "valid" if remote_valid
                    else "invalid" if status in ("expired", "invalid", "banned")
                    else "unknown"
                )
                check_source = str(details.get("source") or "")
                if recovered_by_session:
                    check_source = f"{check_source}+session_refresh" if check_source else "session_refresh"
                overview = {
                    "plan": status,
                    "plan_name": status,
                    "plan_state": plan_state,
                    "chips": chips,
                    "check_source": check_source,
                    "check_token_source": token_source,
                    "validity_status": validity_status,
                    "validity_reason": (
                        "远端账号接口返回不可用状态"
                        if status in ("expired", "invalid", "banned")
                        else "远端账号接口验证通过" if remote_valid
                        else "远端账号接口未返回可识别状态"
                    ),
                    "validity_http_status": 200,
                    "validity_error_code": "",
                    "validity_error_type": "",
                    "validity_error_message": "",
                    "validity_request_id": "",
                    "validity_cf_ray": "",
                    "auth_recovery_attempted": recovered_by_session,
                    "auth_recovery_succeeded": recovered_by_session,
                    "auth_recovery_error": "",
                    "check_error": "",
                }
                if isinstance(details.get("usage"), dict):
                    overview["chatgpt_usage"] = details["usage"]
                self._last_check_overview = overview

                # Any token accepted by the ChatGPT Web liveness endpoints is
                # safe to retain as the dedicated web token.  Never overwrite
                # the separate Codex OAuth access token here.
                self._last_check_credential_updates["web_access_token"] = token
                updated_extra = dict(extra)
                updated_extra["web_access_token"] = token
                account.extra = updated_extra
                return remote_valid

            for proxy, should_report in proxy_candidates:
                proxy_errors: list[Exception] = []
                route_classified = False
                route_had_transient = False
                for token_source, token in token_candidates:
                    a.access_token = token
                    try:
                        details = fetch_subscription_status_details(a, proxy=proxy)
                        if should_report and proxy:
                            proxy_pool.report_success(proxy)
                        return _finish_success(
                            details,
                            token=token,
                            token_source=token_source,
                            recovered_by_session=False,
                        )
                    except Exception as exc:
                        check_errors.append(exc)
                        proxy_errors.append(exc)

                explicit_proxy_failure = any(
                    _is_explicit_remote_invalid(exc)[0]
                    for exc in proxy_errors
                )
                session_token = str(extra.get("session_token") or "").strip()
                if session_token and (explicit_proxy_failure or not token_candidates):
                    refresh_attempted = True
                    try:
                        from platforms.chatgpt.token_refresh import TokenRefreshManager

                        refresh = TokenRefreshManager(proxy_url=proxy).refresh_by_session_token(
                            session_token,
                            existing_refresh_token=str(extra.get("refresh_token") or ""),
                        )
                        if refresh.success and refresh.access_token:
                            refresh_succeeded = True
                            a.access_token = refresh.access_token
                            try:
                                details = fetch_subscription_status_details(a, proxy=proxy)
                                if should_report and proxy:
                                    proxy_pool.report_success(proxy)
                                return _finish_success(
                                    details,
                                    token=refresh.access_token,
                                    token_source="session_refresh",
                                    recovered_by_session=True,
                                )
                            except Exception as exc:
                                check_errors.append(exc)
                                proxy_errors.append(exc)
                                route_classified = True
                                if _is_explicit_remote_invalid(exc)[0]:
                                    terminal_errors.append(exc)
                                else:
                                    transient_errors.append(exc)
                                    route_had_transient = True
                        else:
                            refresh_error = str(refresh.error_message or "session refresh failed")[:500]
                            refresh_error_code = str(getattr(refresh, "error_code", "") or "")[:200]
                            refresh_exc = RuntimeError(refresh_error)
                            check_errors.append(refresh_exc)
                            route_classified = True
                            missing_access = "未找到 access" in refresh_error
                            # A session refresh can be rejected by Cloudflare or
                            # the current proxy route (commonly an HTML 403).  A
                            # bare refresh HTTP status does not prove the account
                            # itself is invalid.  Only a successful session
                            # response without an access token is terminal here;
                            # otherwise preserve ``unknown`` and retry later.
                            if missing_access:
                                terminal_errors.append(
                                    proxy_errors[-1]
                                    if proxy_errors
                                    and all(_is_explicit_remote_invalid(exc)[0] for exc in proxy_errors)
                                    else refresh_exc
                                )
                            else:
                                transient_errors.append(refresh_exc)
                                route_had_transient = True
                    except Exception as exc:
                        refresh_error = _compact_check_error(exc)
                        check_errors.append(exc)
                        transient_errors.append(exc)
                        route_classified = True
                        route_had_transient = True

                if not route_classified and proxy_errors:
                    if all(_is_explicit_remote_invalid(exc)[0] for exc in proxy_errors):
                        terminal_errors.append(proxy_errors[-1])
                    else:
                        transient_errors.append(proxy_errors[-1])
                        route_had_transient = True

                if should_report and proxy and proxy_errors:
                    if route_had_transient:
                        proxy_pool.report_fail(proxy)

        except Exception as exc:
            check_errors.append(exc)
            transient_errors.append(exc)

        explicit_invalid = [
            (exc, _check_error_http_status(exc))
            for exc in terminal_errors
        ]
        if explicit_invalid and not transient_errors:
            exc, status = explicit_invalid[-1]
            error_fields = _remote_check_error_fields(exc)
            if status is not None:
                error_fields.setdefault("validity_http_status", status)
            self._last_check_overview = {
                "validity_status": "invalid",
                "validity_reason": _remote_invalid_reason(
                    error_fields,
                    refresh_attempted=refresh_attempted,
                ),
                "auth_recovery_attempted": refresh_attempted,
                "auth_recovery_succeeded": False,
                "auth_recovery_error": refresh_error,
                "check_error": _compact_check_error(exc),
                **error_fields,
            }
            return False

        if check_errors:
            exc = transient_errors[-1] if transient_errors else check_errors[-1]
            error_fields = _remote_check_error_fields(exc)
            if refresh_error_code:
                error_fields.setdefault("validity_error_code", refresh_error_code)
            self._last_check_overview = {
                "validity_status": "unknown",
                "validity_reason": "检测请求未完成，请检查代理或网络后重试",
                "validity_http_status": _check_error_http_status(exc),
                "auth_recovery_attempted": refresh_attempted,
                "auth_recovery_succeeded": refresh_succeeded,
                "auth_recovery_error": refresh_error,
                "check_error": _compact_check_error(exc),
                **error_fields,
            }
            return False

        self._last_check_overview = {
            "validity_status": "unknown",
            "validity_reason": "账号检测未返回结果",
            "validity_http_status": None,
            "check_error": "",
        }
        return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def get_last_check_credential_updates(self) -> dict:
        return dict(getattr(self, "_last_check_credential_updates", {}) or {})

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return _generate_chatgpt_registration_password()

    def _map_chatgpt_result(
        self,
        result: dict,
        *,
        password: str = "",
        user_id: str = "",
        require_oauth: bool = False,
    ) -> RegistrationResult:
        if require_oauth:
            _assert_complete_oauth_callback(result)
        if str(result.get("register_mode") or "").strip().lower() == "phone":
            if not str(result.get("account_id") or "").strip() or not str(result.get("access_token") or "").strip():
                raise RuntimeError("手机号注册结果缺少 account_id 或 access_token，账号未入库")
            if not str(result.get("phone_number") or "").strip():
                raise RuntimeError("手机号注册结果缺少 phone_number，账号未入库")
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": result.get("account_id", ""),
                "chatgpt_account_id": result.get("chatgpt_account_id", ""),
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "refresh_token_status": result.get(
                    "refresh_token_status",
                    "available" if result.get("refresh_token") else "missing_from_session",
                ),
                "refresh_token_source": result.get("refresh_token_source", ""),
                "id_token": result.get("id_token", ""),
                "session_token": result.get("session_token", ""),
                "session_token_present": bool(result.get("session_token")),
                "workspace_id": result.get("workspace_id", ""),
                "cookies": result.get("cookies", ""),
                "profile": result.get("profile", {}),
                "expires_at": result.get("expires_at", ""),
                "register_mode": result.get("register_mode", "email"),
                "phone_number": result.get("phone_number", ""),
                "registration_state": result.get("registration_state", {}),
                # 短链物理复用：浏览器内 PayPal checkout 结果透传给上层任务判定。
                "_shortlink_checkout": result.get("_shortlink_checkout", None),
                "workspace_join": result.get("workspace_join", None),
                "workspace_statuses": result.get("workspace_statuses", {}),
            },
        )

    def _run_protocol_oauth(self, ctx) -> dict:
        from platforms.chatgpt.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=ctx.identity.oauth_provider,
            email_hint=ctx.identity.email,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
        )

    def _build_post_register_in_browser_callback(self, ctx):
        extra = dict(ctx.extra or {})
        downstream = extra.get("_post_register_in_browser")

        from platforms.chatgpt.workspace_join import (
            run_workspace_join_flow,
            workspace_join_config,
            workspace_join_enabled,
        )

        if not workspace_join_enabled(extra):
            return downstream

        mailbox = getattr(self, "mailbox", None)
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        cfg = workspace_join_config(extra)
        log_fn = ctx.log

        def _post_register(page, session_info: dict) -> dict:
            merged: dict = {}
            try:
                log_fn("注册完成，开始在当前 ChatGPT 页面执行 Workspace Join Request")
                session_payload = dict(session_info or {})
                if ctx.proxy:
                    session_payload.setdefault("proxy", ctx.proxy)
                workspace_result = run_workspace_join_flow(
                    page,
                    session_payload,
                    mailbox=mailbox,
                    mailbox_account=mailbox_account,
                    config=cfg,
                    log=log_fn,
                )
                merged.update(workspace_result)
            except Exception as exc:
                log_fn(f"Workspace Join 后续流程异常（不影响账号注册结果）: {exc}")
                merged["workspace_join"] = {"ok": False, "error": str(exc)}

            if callable(downstream):
                try:
                    downstream_result = downstream(page, session_info)
                    if isinstance(downstream_result, dict):
                        merged.update(downstream_result)
                except Exception as exc:
                    log_fn(f"浏览器内后续流程异常（不影响账号注册结果）: {exc}")
            return merged

        return _post_register

    def build_browser_registration_adapter(self):
        def _build_browser_worker(ctx, artifacts):
            from platforms.chatgpt.browser_register import ChatGPTBrowserRegister

            ctx.log(
                "ChatGPT 浏览器注册 worker 已创建: "
                f"executor={ctx.executor_type}, backend="
                f"{'reuse' if (ctx.extra or {}).get('_reuse_backend_config') else 'default'}"
            )
            return ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                phone_callback=artifacts.phone_callback,
                log_fn=ctx.log,
                backend_config=(ctx.extra or {}).get("_reuse_backend_config"),
                post_register_in_browser=self._build_post_register_in_browser_callback(ctx),
                startup_timeout=resolve_timeout(
                    ctx.extra,
                    ("chatgpt_browser_startup_timeout", "browser_startup_timeout"),
                    45,
                ),
            )

        def _run_browser_register(worker, ctx, artifacts):
            timeout_sec = resolve_timeout(
                ctx.extra,
                ("chatgpt_browser_register_timeout", "browser_register_timeout"),
                300,
            )
            ctx.log(f"开始调用 ChatGPT 浏览器注册 runner: timeout={timeout_sec}s")
            run_kwargs = {
                "email": ctx.identity.email or "",
                "password": ctx.password or "",
            }
            register_mode = str(
                (ctx.extra or {}).get("register_mode")
                or getattr(ctx.identity, "identity_provider", "email")
            ).strip().lower()
            if register_mode == "phone":
                run_kwargs["register_mode"] = "phone"
            # The callback may acquire a thread-owned RLock while renting a
            # number. Transfer cleanup ownership to the isolated browser thread
            # so cancellation and lock release always happen on that same thread.
            phone_cleanup = artifacts.phone_cleanup
            artifacts.phone_cleanup = None

            def _run_and_cleanup_phone():
                try:
                    return worker.run(**run_kwargs)
                finally:
                    if callable(phone_cleanup):
                        phone_cleanup()

            result = _run_sync_checkout_isolated(
                _run_and_cleanup_phone,
                thread_name="chatgpt-browser-register",
                _log_fn=ctx.log,
                timeout_sec=timeout_sec,
            )
            ctx.log("ChatGPT 浏览器注册 runner 已返回")
            return result

        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                require_oauth=getattr(ctx.identity, "identity_provider", "") == "oauth_browser",
            ),
            browser_worker_builder=_build_browser_worker,
            browser_register_runner=_run_browser_register,
            oauth_runner=self._run_protocol_oauth,
            capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
            otp_spec=OtpSpec(wait_message="等待验证码...", timeout=600),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_protocol_oauth,
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                user_id=result.get("account_id", ""),
                require_oauth=True,
            ),
        )

    def build_protocol_mailbox_adapter(self):
        def _build_worker(ctx, artifacts):
            from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

            worker = ChatGPTProtocolMailboxWorker(
                mailbox=self.mailbox,
                mailbox_account=ctx.identity.mailbox_account,
                provider=(self.config.extra or {}).get("mail_provider", ""),
                proxy_url=ctx.proxy,
                proxy_country=str((ctx.extra or {}).get("proxy_route_country") or ""),
                log_fn=ctx.log,
                cancel_check=ctx.platform.is_cancel_requested,
            )
            self._last_protocol_mailbox_worker = worker
            return worker

        def _map_result(ctx, result):
            _assert_complete_oauth_callback(result)
            access_token = result.access_token or ""
            refresh_token = result.refresh_token or ""
            session_token = result.session_token or ""
            metadata = getattr(result, "metadata", None) or {}
            oauth_credential_type = "codex_oauth" if refresh_token else "chatgpt_web"

            return RegistrationResult(
                email=result.email,
                password=result.password or (ctx.password or ""),
                user_id=result.account_id,
                token=access_token,
                status=AccountStatus.REGISTERED,
                extra={
                    "access_token": access_token,
                    "web_access_token": (
                        metadata.get("web_access_token", "")
                        or (access_token if oauth_credential_type == "chatgpt_web" else "")
                    ),
                    "refresh_token": refresh_token,
                    "refresh_token_status": metadata.get(
                        "refresh_token_status",
                        "available" if refresh_token else "missing_from_session",
                    ),
                    "refresh_token_source": metadata.get("refresh_token_source", ""),
                    "id_token": result.id_token,
                    "oauth_credential_type": oauth_credential_type,
                    "session_token": session_token,
                    "session_token_present": bool(session_token),
                    "workspace_id": result.workspace_id,
                    "cookies": metadata.get("cookies", ""),
                    "profile": metadata.get("profile", {}),
                    "expires_at": metadata.get("expires_at", ""),
                    "session": metadata.get("session", {}),
                },
            )

        return ProtocolMailboxAdapter(
            result_mapper=_map_result,
            worker_builder=_build_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
            ),
        )

    def complete_protocol_codex_credentials(self, account: Account) -> dict:
        """Upgrade a protocol-created Web session to genuine Codex PKCE credentials."""
        mailbox_worker = getattr(self, "_last_protocol_mailbox_worker", None)
        if (
            mailbox_worker is None
            or getattr(mailbox_worker, "email_service", None) is None
            or getattr(mailbox_worker, "engine", None) is None
        ):
            return {
                "ok": False,
                "error_code": "protocol_session_missing",
                "error": "Protocol mailbox session is unavailable",
            }

        from platforms.chatgpt.protocol_phone import (
            ChatGPTProtocolEmailThenPhoneWorker,
            _export_session_cookies,
        )

        worker = ChatGPTProtocolEmailThenPhoneWorker(
            email_service=mailbox_worker.email_service,
            phone_callback=lambda: "",
            proxy_url=self.config.proxy,
            log_fn=self.log,
            cancel_check=self.is_cancel_requested,
            max_phone_attempts=1,
            require_codex_refresh_token=True,
            existing_device_id=str(
                getattr(mailbox_worker.engine, "_device_id", "")
                or (getattr(account, "extra", {}) or {}).get("oai_device_id")
                or ""
            ),
            existing_auth_cookies=(
                _export_session_cookies(mailbox_worker.engine.session)
                or (getattr(account, "extra", {}) or {}).get("auth_cookies")
                or (getattr(account, "extra", {}) or {}).get("cookies")
                or ""
            ),
            proxy_country=str(
                getattr(mailbox_worker.engine, "preflight_location", "")
                or getattr(account, "region", "")
                or ""
            ),
        )
        result = worker.acquire_codex_credentials(
            email=str(account.email or ""),
            password=str(account.password or ""),
        )
        if not result.get("ok"):
            return result

        tokens = dict(result.get("data") or {})
        existing_account_id = str(account.user_id or "").strip()
        codex_account_id = str(tokens.get("account_id") or "").strip()
        if existing_account_id and codex_account_id and existing_account_id != codex_account_id:
            return {
                "ok": False,
                "error_code": "codex_account_mismatch",
                "error": "Codex OAuth returned credentials for a different account",
            }
        extra = dict(account.extra or {})
        current_access_token = str(extra.get("access_token") or account.token or "").strip()
        if current_access_token and not str(extra.get("web_access_token") or "").strip():
            extra["web_access_token"] = current_access_token
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "session_token",
            "cookies",
            "oai_device_id",
        ):
            value = str(tokens.get(key) or "").strip()
            if value:
                extra[key] = value
        if tokens.get("auth_cookies"):
            extra["auth_cookies"] = json.dumps(
                tokens["auth_cookies"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        extra["oauth_credential_type"] = "codex_oauth"
        account.extra = extra
        account.token = str(extra.get("access_token") or account.token or "")
        if codex_account_id:
            account.user_id = codex_account_id
        return {"ok": True, "data": tokens}

    def complete_protocol_phone_verification(
        self,
        account: Account,
        *,
        phone_callback,
        max_phone_attempts: int = 3,
    ) -> dict:
        mailbox_worker = getattr(self, "_last_protocol_mailbox_worker", None)
        if mailbox_worker is None or getattr(mailbox_worker, "engine", None) is None:
            raise RuntimeError("邮箱协议注册会话不存在，手机号验证必须紧接本次邮箱注册执行")

        from platforms.chatgpt.protocol_phone import (
            ChatGPTProtocolEmailThenPhoneWorker,
            _export_session_cookies,
        )

        require_rt = True
        try:
            extra = dict(getattr(self.config, "extra", None) or getattr(account, "extra", None) or {})
            # Prefer task-level flags from plugin config if present.
            cfg_extra = dict(getattr(self, "_task_extra", None) or {})
            if cfg_extra:
                extra = {**extra, **cfg_extra}
            raw = extra.get("require_codex_refresh_token")
            if raw is not None and str(raw).strip() != "":
                require_rt = str(raw).strip().lower() not in {"0", "false", "no", "off"}
        except Exception:
            require_rt = True
        worker = ChatGPTProtocolEmailThenPhoneWorker(
            email_service=mailbox_worker.email_service,
            phone_callback=phone_callback,
            proxy_url=self.config.proxy,
            log_fn=self.log,
            cancel_check=self.is_cancel_requested,
            max_phone_attempts=min(max(int(max_phone_attempts or 3), 1), 20),
            require_codex_refresh_token=require_rt,
            existing_device_id=str(
                getattr(mailbox_worker.engine, "_device_id", "")
                or (getattr(account, "extra", {}) or {}).get("oai_device_id")
                or ""
            ),
            existing_auth_cookies=(
                _export_session_cookies(mailbox_worker.engine.session)
                or (getattr(account, "extra", {}) or {}).get("auth_cookies")
                or (getattr(account, "extra", {}) or {}).get("cookies")
                or ""
            ),
            proxy_country=str(
                getattr(mailbox_worker.engine, "preflight_location", "")
                or getattr(account, "region", "")
                or ""
            ),
        )
        result = worker.run_for_account(
            email=str(account.email or ""),
            password=str(account.password or ""),
        )
        # Persist Codex tokens onto the account immediately so later steps/Sub2 see them.
        if isinstance(result, dict):
            extra = dict(getattr(account, "extra", {}) or {})
            prior_access = str(extra.get("access_token") or getattr(account, "token", "") or "").strip()
            for key in (
                "access_token",
                "refresh_token",
                "id_token",
                "session_token",
                "cookies",
                "oai_device_id",
            ):
                value = str(result.get(key) or "").strip()
                if value:
                    extra[key] = value
            if result.get("auth_cookies"):
                extra["auth_cookies"] = json.dumps(
                    result["auth_cookies"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            if str(result.get("refresh_token") or "").strip():
                if prior_access and not str(extra.get("web_access_token") or "").strip():
                    extra["web_access_token"] = prior_access
                extra["oauth_credential_type"] = "codex_oauth"
                account.token = str(extra.get("access_token") or account.token or "")
            account.extra = extra
        return {"ok": True, "data": result}

    def build_protocol_phone_adapter(self):
        def _build_worker(ctx, artifacts):
            from core.base_mailbox import create_mailbox
            from infrastructure.provider_settings_repository import ProviderSettingsRepository
            from platforms.chatgpt.protocol_phone import ChatGPTProtocolPhoneWorker

            bind_email = _bool_param(
                ctx.extra,
                "phone_bind_email_after_registration",
                True,
            )
            mailbox_factory = None
            if bind_email:
                settings_repo = ProviderSettingsRepository()
                provider_key = str(ctx.extra.get("mail_provider") or "").strip()
                if not provider_key:
                    provider_key = settings_repo.get_default_provider_key("mailbox")
                if not provider_key:
                    raise RuntimeError("双接码 Free 需要先在设置页启用一个邮箱 Provider")
                selected_setting = settings_repo.get_by_key("mailbox", provider_key)
                if not selected_setting or not selected_setting.enabled:
                    raise RuntimeError(f"邮箱 Provider 未启用: {provider_key}")
                runtime_overrides = dict(ctx.extra)
                runtime_overrides["mail_provider_strict"] = True
                runtime_extra = settings_repo.resolve_runtime_settings(
                    "mailbox",
                    provider_key,
                    runtime_overrides,
                )
                mailbox_proxy = str(runtime_extra.get("mailbox_proxy") or "").strip()

                def mailbox_factory(proxy_url):
                    return create_mailbox(
                        provider_key,
                        extra=runtime_extra,
                        proxy=mailbox_proxy or None,
                    )

            return ChatGPTProtocolPhoneWorker(
                phone_callback=artifacts.phone_callback,
                proxy_url=ctx.proxy,
                log_fn=ctx.log,
                cancel_check=ctx.platform.is_cancel_requested,
                max_phone_attempts=min(max(int((ctx.extra or {}).get("sms_phone_max_attempts") or 8), 1), 20),
                proxy_country=str((ctx.extra or {}).get("proxy_route_country") or ""),
                mailbox_factory=mailbox_factory,
                bind_email_after_registration=bind_email,
                email_otp_timeout_seconds=min(
                    max(int(ctx.extra.get("email_otp_timeout_seconds") or 300), 60),
                    600,
                ),
            )

        def _map_result(ctx, result):
            if not result or not result.success:
                raise RuntimeError(
                    str(getattr(result, "error_message", "") or "手机号协议注册失败")
                )
            metadata = dict(getattr(result, "metadata", None) or {})
            phone_number = str(metadata.get("phone_number") or "").strip()
            if not result.account_id or not result.access_token or not phone_number:
                raise RuntimeError("手机号协议注册结果缺少 account_id、access_token 或 phone_number")
            binding_status = str(metadata.get("email_binding_status") or "").strip()
            register_mode = str(metadata.get("register_mode") or "phone")
            account_email = (
                str(result.email or "").strip()
                if register_mode == "phone_with_email" and "@" in str(result.email or "")
                else f"phone:{phone_number}"
            )
            return RegistrationResult(
                email=account_email,
                password=result.password or (ctx.password or ""),
                user_id=result.account_id,
                token=result.access_token,
                status=(
                    AccountStatus.PENDING_VERIFICATION
                    if binding_status == "failed"
                    else AccountStatus.REGISTERED
                ),
                extra={
                    "access_token": result.access_token,
                    "refresh_token": result.refresh_token,
                    "refresh_token_status": metadata.get(
                        "refresh_token_status",
                        "available" if result.refresh_token else "missing_from_session",
                    ),
                    "refresh_token_source": metadata.get("refresh_token_source", ""),
                    "id_token": result.id_token,
                    "session_token": result.session_token,
                    "session_token_present": bool(result.session_token),
                    "phone_number": phone_number,
                    "register_mode": register_mode,
                    "session": metadata.get("session", {}),
                    "cookies": metadata.get("cookies", ""),
                    "auth_cookies": json.dumps(
                        metadata.get("auth_cookies", []),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "oai_device_id": metadata.get("oai_device_id", ""),
                    "auth_proxy_url": metadata.get("auth_proxy_url", ""),
                    "auth_proxy_country": metadata.get("auth_proxy_country", ""),
                    "auth_proxy_session": metadata.get("auth_proxy_session", ""),
                    "email_binding_status": binding_status or (
                        "success" if register_mode == "phone_with_email" else "skipped"
                    ),
                    "email_binding_error": metadata.get("email_binding_error", ""),
                    "verification_mailbox": metadata.get("verification_mailbox", {}),
                    "provider_accounts": metadata.get("provider_accounts", []),
                    "provider_resources": metadata.get("provider_resources", []),
                },
            )

        return ProtocolMailboxAdapter(
            result_mapper=_map_result,
            worker_builder=_build_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                password=ctx.password or "",
            ),
        )

    def get_platform_actions(self) -> list:
        mailbox_options = []
        try:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            settings = ProviderSettingsRepository().list_enabled("mailbox")
            mailbox_options = [
                {
                    "value": str(item.provider_key or ""),
                    "label": f"{item.display_name or item.provider_key}{'（默认）' if item.is_default else ''}",
                }
                for item in settings
                if str(item.provider_key or "").strip()
            ]
        except Exception:
            mailbox_options = []
        if not mailbox_options:
            mailbox_options = [{"value": "default", "label": "设置页默认邮箱 Provider"}]
        return [
            {
                "id": "bind_email",
                "label": "添加邮箱",
                "params": [
                    {
                        "key": "mailbox_provider",
                        "label": "邮箱来源",
                        "type": "select",
                        "options": mailbox_options,
                    },
                    {
                        "key": "otp_timeout_seconds",
                        "label": "邮箱验证码等待时间",
                        "type": "select",
                        "options": [
                            {"value": "300", "label": "5 分钟"},
                            {"value": "120", "label": "2 分钟"},
                            {"value": "180", "label": "3 分钟"},
                            {"value": "480", "label": "8 分钟"},
                        ],
                    },
                    {"key": "proxy", "label": "代理（留空使用代理池）", "type": "text"},
                ],
            },
            {"id": "receive_email_code", "label": "读取邮箱验证码", "params": []},
            {"id": "switch_account", "label": "切换到 Codex 桌面端", "params": []},
            {"id": "get_account_state", "label": "查询账号状态/订阅", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "get_rt", "label": "获取rt",
             "params": [
                 {"key": "browser_mode", "label": "浏览器模式", "type": "select",
                  "options": ["camoufox_headed", "camoufox_headless"]},
             ]},
            {"id": "get_rt_bypass", "label": "获取rt(绕过手机号)",
             "params": [
                 {"key": "browser_mode", "label": "浏览器模式", "type": "select",
                  "options": ["camoufox_headed", "camoufox_headless"]},
             ]},
            {"id": "payment_link", "label": "打开支付链接",
             "params": [
                 {"key": "country", "label": "地区", "type": "select",
                  "options": ["ID","US","SG","TR","HK","JP","GB","AU","CA","IN","BR","MX","EU"]},
                 {"key": "currency", "label": "币种", "type": "select",
                  "options": ["IDR","USD","SGD","TRY","HKD","JPY","GBP","AUD","CAD","INR","BRL","MXN","EUR"]},
                 {"key": "plan", "label": "套餐", "type": "select",
                  "options": ["plus", "team"]},
                 {"key": "auto_checkout", "label": "自动提交 PayPal", "type": "select",
                  "options": ["true", "false"]},
                 {"key": "use_stripe_init", "label": "Stripe协议长链(accessToken直生成)", "type": "select",
                  "options": ["false", "true"]},
                 {"key": "use_short_link", "label": "短链(checkout_ui_mode=custom)", "type": "select",
                  "options": ["false", "true"]},
                 {"key": "payment_method", "label": "支付方式", "type": "select",
                  "options": ["paypal"]},
                 {"key": "headless", "label": "后台模式", "type": "select",
                  "options": ["false", "true"]},
                 # checkout_mode 决定 PayPal checkout 浏览器后端：
                 #   - protocol: 走 Stripe API 协议链，无浏览器
                 #   - camoufox_headed / camoufox_headless: 老 Camoufox 路径
                 #   - bitbrowser_headed / bitbrowser_hidden / bitbrowser_headless:
                 #     新 BitBrowser 路径，profile ID 通过 bit_profile_id 字段传入
                 {"key": "checkout_mode", "label": "Checkout 后端模式", "type": "select",
                  "options": [
                      "",
                      "protocol",
                      "camoufox_headed",
                      "camoufox_headless",
                      "bitbrowser_headed",
                      "bitbrowser_hidden",
                      "bitbrowser_headless",
                  ]},
                 # bitbrowser_* 模式下必填：BitBrowser 客户端里手工创建好的 profile ID
                 # （比特浏览器 → 浏览器列表 → 编辑那一栏看到的 ID 字符串）。
                 # 留空时回退到 BIT_PROFILE_ID 环境变量。
                 {"key": "bit_profile_id", "label": "BitBrowser Profile ID", "type": "text",
                  "placeholder": "比特浏览器 profile ID（仅 bitbrowser_* 模式下生效）"},
                 {"key": "checkout_timeout", "label": "结账超时秒数", "type": "number"},
                 {"key": "checkout_hold_seconds", "label": "前台保留秒数", "type": "number"},
                 # SMS 号码池：批量手机号 + 短信中转 URL，PayPal OTP 用
                 # 每行 `+phone----relay_url`，多行批量。空行 / # 注释行自动忽略。
                 {"key": "sms_pool", "label": "SMS 号码池 (+phone----relay_url 每行一条)",
                  "type": "textarea", "placeholder": "+12025550101----https://relay.example.com/api/text-relay/RELAY_KEY"},
             ]},
            {"id": "extract_payment_link", "label": "支付提链 (UPL)",
             "params": [
                 {"key": "payment_method", "label": "提链方式", "type": "select",
                  "options": [
                      {"value": "ideal", "label": "iDEAL · NL/VN/NL"},
                      {"value": "pix", "label": "PIX · BR/VN/BR"},
                      {"value": "kakao_pay", "label": "Kakao Pay · KR/VN/KR"},
                      {"value": "blik", "label": "BLIK · PL/PL/PL"},
                      {"value": "twint", "label": "TWINT · CH/VN/CH"},
                      {"value": "upi", "label": "UPI · IN/VN/IN"},
                  ]},
                 {"key": "proxy_seeds", "label": "代理 Seed（每行一条；留空使用当前代理/代理池）",
                  "type": "textarea"},
                 {"key": "checkout_proxies", "label": "UPI Checkout 代理池（可选）", "type": "textarea"},
                 {"key": "promotion_proxies", "label": "UPI Promotion 代理池（可选）", "type": "textarea"},
                 {"key": "provider_proxies", "label": "UPI Provider/Approve 代理池（可选）", "type": "textarea"},
                 {"key": "promo_mode", "label": "优惠模式", "type": "select",
                  "options": ["campaign", "off", "query", "coupon", "trial"]},
                 {"key": "promo_id", "label": "优惠 ID", "type": "text"},
                 {"key": "batch_size", "label": "每轮代理数", "type": "number"},
                 {"key": "max_batches", "label": "最大轮数", "type": "number"},
                 {"key": "poll_timeout", "label": "支付结果轮询秒数", "type": "number"},
                 {"key": "timeout_seconds", "label": "任务总超时秒数", "type": "number"},
                 {"key": "blik_code", "label": "BLIK Code（选择 BLIK 时填写 6 位）", "type": "text"},
             ]},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
            {"id": "upload_tm", "label": "上传 Team Manager",
             "params": [
                 {"key": "api_url", "label": "TM API URL", "type": "text"},
                 {"key": "api_key", "label": "TM API Key", "type": "text"},
             ]},
        ]

    def get_desktop_state(self) -> dict:
        from platforms.chatgpt.switch import get_codex_desktop_state

        return get_codex_desktop_state()

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id in {"get_account_state", "query_state"}:
            return self._handle_query_state(account, params)
        if action_id in {"switch_account", "switch_desktop"}:
            return self._execute_platform_action("switch_desktop", account, params)
        if action_id == "refresh_token":
            return self._handle_refresh_token(account, params)
        if action_id == "bind_email":
            return self._handle_bind_email(account, params)
        if action_id == "receive_email_code":
            return self._handle_receive_email_code(account, params)
        if action_id == "payment_link":
            return self._handle_generate_link(account, params)
        if action_id == "extract_payment_link":
            return self._handle_extract_payment_link(account, params)
        if action_id == "get_rt":
            return self._handle_get_rt(account, params)
        if action_id == "get_rt_bypass":
            return self._handle_get_rt_bypass(account, params)
        return super().execute_action(action_id, account, params)

    def _handle_extract_payment_link(self, account: Account, params: dict) -> dict:
        from platforms.chatgpt.upl_adapter import extract_payment_link

        method = str(params.get("payment_method") or "upi").strip().lower()
        region = {
            "ideal": "NL",
            "pix": "BR",
            "kakao_pay": "KR",
            "blik": "PL",
            "twint": "CH",
            "upi": "IN",
        }.get(method, "IN")
        fallback_proxy = str(params.get("proxy") or "").strip()
        if not fallback_proxy and self.config:
            fallback_proxy = str(self.config.proxy or "").strip()
        has_explicit_pool = any(
            str(params.get(key) or "").strip()
            for key in (
                "proxy_seeds",
                "checkout_proxies",
                "promotion_proxies",
                "provider_proxies",
            )
        )
        if not fallback_proxy and not has_explicit_pool:
            fallback_proxy = str(proxy_pool.get_next(region=region) or "").strip()
        if fallback_proxy:
            self.log(f"UPL 使用代理 Seed: {_mask_proxy(fallback_proxy)}")

        data = extract_payment_link(
            account,
            payment_method=method,
            params=params,
            fallback_proxy=fallback_proxy,
            log_fn=self.log,
            cancel_check=self.is_cancel_requested,
        )
        account_extra = dict(account.extra or {})
        overview = dict(account_extra.get("account_overview") or {})
        payment_links = dict(overview.get("payment_links") or {})
        payment_links[method] = {
            "url": data["url"],
            "label": data["payment_label"],
            "flow": data["payment_flow"],
            "upstream_commit": data["upstream_commit"],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return {
            "ok": True,
            "data": data,
            "_persist": {
                "summary_updates": {
                    "payment_links": payment_links,
                    "last_payment_link": data["url"],
                    "last_payment_method": method,
                }
            },
        }

    def _handle_receive_email_code(self, account: Account, params: dict) -> dict:
        timeout_seconds = min(max(_int_param(params, "timeout_seconds", 120), 30), 300)
        callback, error = self._build_get_rt_mailbox_otp_callback(
            account,
            self.log,
            proxy=None,
            include_existing=True,
            timeout_seconds=10,
        )
        if callback is None:
            return {"ok": False, "error": error or "账号没有可用的验证码邮箱"}

        self.log(f"开始读取账号邮箱验证码，最长等待 {timeout_seconds} 秒")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            try:
                code = str(callback() or "").strip()
            except TimeoutError:
                continue
            if code:
                mailbox_email = str(account.email or "").strip()
                for resource in list((account.extra or {}).get("provider_resources") or []):
                    if not isinstance(resource, dict):
                        continue
                    if str(resource.get("resource_type") or "mailbox").lower() != "mailbox":
                        continue
                    mailbox_email = str(
                        resource.get("handle")
                        or resource.get("display_name")
                        or mailbox_email
                    ).strip()
                    break
                self.log("邮箱验证码读取成功")
                return {
                    "ok": True,
                    "data": {
                        "message": "邮箱验证码已读取并复制",
                        "code": code,
                        "email": mailbox_email,
                    },
                }
        return {"ok": False, "error": f"等待邮箱验证码超时 ({timeout_seconds}s)"}

    def _handle_bind_email(self, account: Account, params: dict) -> dict:
        from core.base_mailbox import create_mailbox
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        from platforms.chatgpt.bind_email import ChatGPTProtocolBindEmailWorker

        extra = dict(account.extra or {})
        phone_number = str(extra.get("phone_number") or "").strip()
        if not phone_number and str(account.email or "").startswith("phone:"):
            phone_number = str(account.email).split(":", 1)[1].strip()
        if not phone_number:
            return {"ok": False, "error": "该操作只适用于手机号注册账号"}
        if "@" in str(account.email or "") and not str(account.email or "").startswith("phone:"):
            return {"ok": False, "error": "该账号已经有邮箱"}
        settings_repo = ProviderSettingsRepository()
        provider_key = str(params.get("mailbox_provider") or "").strip()
        if not provider_key or provider_key == "default":
            provider_key = settings_repo.get_default_provider_key("mailbox")
        if not provider_key:
            return {"ok": False, "error": "设置页尚未配置可用邮箱 Provider"}
        selected_setting = settings_repo.get_by_key("mailbox", provider_key)
        if not selected_setting or not selected_setting.enabled:
            return {"ok": False, "error": f"邮箱 Provider 未启用: {provider_key}"}

        proxy = str(params.get("proxy") or "").strip()
        if not proxy:
            proxy = str(extra.get("auth_proxy_url") or "").strip()
        if not proxy and self.config:
            proxy = str(self.config.proxy or "").strip()
        if not proxy:
            proxy = str(proxy_pool.get_next(region=str(account.region or "")) or "").strip()
        if proxy:
            self.log(f"添加邮箱使用代理: {_mask_proxy(proxy)}")
        else:
            self.log("添加邮箱未选择代理，使用直连")

        runtime_extra = settings_repo.resolve_runtime_settings(
            "mailbox",
            provider_key,
            {"mail_provider_strict": True},
        )
        mailbox_proxy = str(runtime_extra.get("mailbox_proxy") or "").strip()
        mailbox = create_mailbox(
            provider_key,
            extra=runtime_extra,
            proxy=mailbox_proxy or None,
        )

        timeout_seconds = min(max(_int_param(params, "otp_timeout_seconds", 300), 60), 600)
        worker = ChatGPTProtocolBindEmailWorker(
            phone_number=phone_number,
            password=account.password,
            mailbox=mailbox,
            proxy_url=proxy or None,
            otp_timeout_seconds=timeout_seconds,
            log_fn=self.log,
            cancel_check=self.is_cancel_requested,
            existing_access_token=str(extra.get("access_token") or account.token or ""),
            existing_session_token=str(extra.get("session_token") or ""),
            existing_auth_cookies=extra.get("auth_cookies") or extra.get("cookies") or "",
            existing_device_id=str(extra.get("oai_device_id") or ""),
        )
        result = worker.run()
        mailbox_account = worker.mailbox_account
        if mailbox_account is None:
            return {"ok": False, "error": "邮箱绑定完成，但缺少邮箱资源记录"}
        mailbox_account.extra = dict(mailbox_account.extra or {})
        mailbox_account.extra.setdefault("mailbox_provider_key", provider_key)

        if hasattr(mailbox, "mark_registration_success"):
            try:
                mailbox.mark_registration_success(mailbox_account)
            except Exception as exc:
                self.log(f"邮箱已绑定，但邮箱池成功标签写入失败: {exc}")

        mailbox_extra = dict(mailbox_account.extra or {})
        credentials = {
            key: result.get(key)
            for key in (
                "access_token",
                "refresh_token",
                "session_token",
                "id_token",
                "cookies",
                "oai_device_id",
            )
            if result.get(key)
        }
        if result.get("auth_cookies"):
            credentials["auth_cookies"] = json.dumps(
                result["auth_cookies"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        persisted_proxy = str(result.get("auth_proxy_url") or proxy or "").strip()
        if persisted_proxy:
            credentials["auth_proxy_url"] = persisted_proxy
        persist = {
            "account_email": str(result.get("email") or mailbox_account.email),
            "summary_updates": {
                "phone_number": phone_number,
                "remote_email": str(result.get("email") or mailbox_account.email),
                "register_mode": "phone_with_email",
            },
            "credentials": credentials,
            "provider_accounts": [mailbox_extra["provider_account"]]
            if isinstance(mailbox_extra.get("provider_account"), dict)
            else [],
            "provider_resources": [mailbox_extra["provider_resource"]]
            if isinstance(mailbox_extra.get("provider_resource"), dict)
            else [],
        }
        return {
            "ok": True,
            "data": {
                "message": "邮箱添加并验证成功",
                "email": persist["account_email"],
                "phone_number": phone_number,
                "mailbox_provider": provider_key,
                "session_refreshed": bool(result.get("session_refreshed")),
            },
            "_persist": persist,
        }

    def _execute_platform_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Handle ChatGPT-specific actions."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        from .constants import OAUTH_CLIENT_ID
        a.client_id = extra.get("client_id", OAUTH_CLIENT_ID)
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id or extra.get("user_id", "") or ""
        a.account_id = extra.get("account_id") or account.user_id or ""
        a.chatgpt_account_id = (
            extra.get("chatgpt_account_id")
            or extra.get("chatgptAccountId")
            or extra.get("account_id")
            or account.user_id
            or ""
        )

        if action_id == "switch_desktop":
            from platforms.chatgpt.switch import (
                close_codex_app,
                extract_session_token,
                fetch_chatgpt_account_state,
                get_codex_desktop_state,
                read_current_codex_account,
                restart_codex_app,
                switch_codex_account,
            )

            session_token = extract_session_token(a.session_token, a.cookies)
            if not session_token:
                return {"ok": False, "error": "Switch to Codex desktop requires session_token"}

            close_ok, close_msg = close_codex_app()
            switch_ok, switch_data = switch_codex_account(session_token=session_token, cookies=a.cookies)
            if not switch_ok:
                return {"ok": False, "error": switch_data.get("error", "Switch failed")}

            remote_state = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=session_token,
                id_token=a.id_token,
                chatgpt_account_id=a.chatgpt_account_id,
                cookies=a.cookies,
                proxy=proxy,
            )
            local_state = read_current_codex_account()
            restart_ok, restart_msg = restart_codex_app()
            message_parts = [switch_data.get("message", "Codex credentials written")]
            if close_msg:
                message_parts.append(close_msg)
            if restart_msg:
                message_parts.append(restart_msg)
            data = {
                "message": ".".join(part for part in message_parts if part),
                "close": {"ok": close_ok, "message": close_msg},
                "restart": {"ok": restart_ok, "message": restart_msg},
                "local_app_account": local_state,
                "desktop_app_state": get_codex_desktop_state(),
                "remote_state": remote_state,
                "switch_details": switch_data,
            }
            if remote_state.get("access_token"):
                data["access_token"] = remote_state["access_token"]
            if remote_state.get("refresh_token"):
                data["refresh_token"] = remote_state["refresh_token"]
            return {"ok": True, "data": data}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(token_data, api_url=params.get("api_url"),
                                    api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager
            ok, msg = upload_to_team_manager(a, api_url=params.get("api_url"),
                                             api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        if action_id == "payment_link":
            return self._handle_generate_link(account, params)

        raise NotImplementedError(f"Unknown action: {action_id}")

    # Override specific capability handlers
    def _handle_query_state(self, account: Account, params: dict) -> dict:
        """Handle query_state capability for ChatGPT."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")
        a.chatgpt_account_id = (
            extra.get("chatgpt_account_id")
            or extra.get("chatgptAccountId")
            or extra.get("account_id")
            or account.user_id
            or ""
        )

        from platforms.chatgpt.switch import fetch_chatgpt_account_state, get_codex_desktop_state, read_current_codex_account

        data = fetch_chatgpt_account_state(
            access_token=a.access_token,
            session_token=a.session_token,
            id_token=a.id_token,
            chatgpt_account_id=a.chatgpt_account_id,
            cookies=a.cookies,
            proxy=proxy,
        )
        data["local_app_account"] = read_current_codex_account()
        data["desktop_app_state"] = get_codex_desktop_state()
        return {"ok": True, "data": data}

    def _handle_refresh_token(self, account: Account, params: dict) -> dict:
        """Handle refresh_token capability for ChatGPT."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.session_token = extra.get("session_token", "")
        a.id_token = extra.get("id_token", "")
        a.cookies = extra.get("cookies", "")

        from platforms.chatgpt.token_refresh import TokenRefreshManager
        manager = TokenRefreshManager(proxy_url=proxy)
        result = manager.refresh_account(a)
        if result.success:
            data = {
                "access_token": result.access_token,
                # Session refresh normally omits RT; preserve the current
                # Codex RT so action persistence never clears it.
                "refresh_token": result.refresh_token or a.refresh_token,
            }
            try:
                from platforms.chatgpt.switch import fetch_chatgpt_account_state
                data["account_state"] = fetch_chatgpt_account_state(
                    access_token=result.access_token,
                    session_token=a.session_token,
                    id_token=a.id_token,
                    chatgpt_account_id=extra.get("chatgpt_account_id") or extra.get("account_id") or account.user_id or "",
                    cookies=a.cookies,
                    proxy=proxy,
                )
            except Exception:
                pass
            return {"ok": True, "data": data}
        return {"ok": False, "error": result.error_message}

    def _build_get_rt_mailbox_otp_callback(
        self,
        account: Account,
        log_fn,
        proxy: str | None,
        *,
        include_existing: bool = False,
        timeout_seconds: int = 600,
    ):
        """Build an OTP callback from the mailbox resource attached to account."""
        from core.base_mailbox import MailboxAccount, create_mailbox

        def _text(value) -> str:
            return str(value or "").strip()

        def _safe_dict(value) -> dict:
            return dict(value) if isinstance(value, dict) else {}

        def _safe_list(value) -> list:
            return list(value) if isinstance(value, (list, tuple)) else []

        def _mailbox_provider_key(value: str, metadata: dict | None = None) -> str:
            raw = _text(value)
            api_mode = _text((metadata or {}).get("api_mode")).lower()
            if raw in {"cloud_mail", "cfworker"} or api_mode in {"cloud_mail", "cfworker"}:
                return "cfworker_admin_api"
            return raw

        def _apply_provider_compat_settings(provider_key: str, runtime_extra: dict, metadata: dict) -> None:
            if provider_key == "cfworker_admin_api":
                if metadata.get("api_url") and not runtime_extra.get("cfworker_api_url"):
                    runtime_extra["cfworker_api_url"] = metadata.get("api_url")
                if metadata.get("domain") and not runtime_extra.get("cfworker_domain"):
                    runtime_extra["cfworker_domain"] = metadata.get("domain")
                token = (
                    metadata.get("admin_token")
                    or metadata.get("public_token")
                    or metadata.get("api_token")
                    or metadata.get("token")
                )
                if token and not runtime_extra.get("cfworker_admin_token"):
                    runtime_extra["cfworker_admin_token"] = token

        extra = _safe_dict(account.extra)
        resources = [dict(item) for item in _safe_list(extra.get("provider_resources")) if isinstance(item, dict)]
        mailbox_resources = []
        for item in resources:
            if _text(item.get("resource_type") or "mailbox").lower() == "mailbox":
                mailbox_resources.append(item)

        if not mailbox_resources:
            mailbox = _safe_dict(extra.get("verification_mailbox"))
            if not mailbox:
                mailbox = _safe_dict(_safe_dict(extra.get("identity")).get("mailbox"))
            if mailbox:
                mailbox_resources.append({
                    "provider_type": "mailbox",
                    "provider_name": mailbox.get("provider"),
                    "resource_type": "mailbox",
                    "resource_identifier": mailbox.get("account_id"),
                    "handle": mailbox.get("email"),
                    "display_name": mailbox.get("email"),
                    "metadata": {
                        "account_id": mailbox.get("account_id"),
                        "email": mailbox.get("email"),
                    },
                })

        if not mailbox_resources:
            return None, "账号没有绑定邮箱 provider 资源，无法自动读取真实邮箱 OTP"

        provider_accounts = [
            dict(item) for item in _safe_list(extra.get("provider_accounts")) if isinstance(item, dict)
        ]
        last_error = ""
        selected_provider_name = ""
        selected_mailbox_email = ""
        mailbox = None
        mailbox_account = None

        for mailbox_resource in mailbox_resources:
            metadata = _safe_dict(mailbox_resource.get("metadata"))
            raw_provider_name = _text(mailbox_resource.get("provider_name") or mailbox_resource.get("provider"))
            provider_name = _mailbox_provider_key(raw_provider_name, metadata)
            mailbox_email = _text(
                mailbox_resource.get("handle")
                or mailbox_resource.get("display_name")
                or metadata.get("email")
                or account.email
            )
            account_id = _text(
                mailbox_resource.get("resource_identifier")
                or metadata.get("account_id")
                or metadata.get("id")
                or mailbox_email
            )

            if not provider_name:
                last_error = "账号邮箱资源缺少 provider_name"
                continue
            if not mailbox_email:
                last_error = "账号邮箱资源缺少 email"
                continue

            accepted_providers = {provider_name, raw_provider_name}
            if provider_name == "cfworker_admin_api":
                accepted_providers.update({"cloud_mail", "cfworker"})
            accepted_providers = {item for item in accepted_providers if item}

            same_provider_account = None
            matched_provider_account = None
            email_lc = mailbox_email.lower()
            account_id_lc = account_id.lower()
            for item in provider_accounts:
                item_provider = _mailbox_provider_key(
                    _text(item.get("provider_name") or item.get("provider")),
                    _safe_dict(item.get("metadata")),
                )
                raw_item_provider = _text(item.get("provider_name") or item.get("provider"))
                if (item_provider or raw_item_provider) and not ({item_provider, raw_item_provider} & accepted_providers):
                    continue
                if same_provider_account is None:
                    same_provider_account = item
                item_metadata = _safe_dict(item.get("metadata"))
                item_credentials = _safe_dict(item.get("credentials"))
                candidates = {
                    _text(item.get("login_identifier")).lower(),
                    _text(item.get("display_name")).lower(),
                    _text(item_metadata.get("email")).lower(),
                    _text(item_metadata.get("account_id")).lower(),
                    _text(item_credentials.get("email")).lower(),
                    _text(item_credentials.get("login_account")).lower(),
                    _text(item.get("id")).lower(),
                }
                if email_lc in candidates or (account_id_lc and account_id_lc in candidates):
                    matched_provider_account = item
                    break

            provider_account = matched_provider_account or same_provider_account
            runtime_extra = dict(metadata)
            _apply_provider_compat_settings(provider_name, runtime_extra, metadata)
            runtime_extra["provider_resource"] = mailbox_resource
            if provider_account:
                runtime_extra["provider_account"] = provider_account

            mailbox_account_extra = dict(runtime_extra)
            mailbox_account_extra["mailbox_provider_key"] = provider_name
            mailbox_account = MailboxAccount(
                email=mailbox_email,
                account_id=account_id,
                extra=mailbox_account_extra,
            )
            try:
                mailbox = create_mailbox(provider_name, extra=runtime_extra, proxy=proxy)
            except Exception as exc:
                last_error = f"{raw_provider_name or provider_name} -> {provider_name}: {exc}"
                log_fn(f"  获取rt: 跳过不可用邮箱资源 {last_error}")
                mailbox = None
                mailbox_account = None
                continue
            selected_provider_name = provider_name
            selected_mailbox_email = mailbox_email
            if raw_provider_name and raw_provider_name != provider_name:
                log_fn(f"  获取rt: 邮箱 provider 兼容映射 {raw_provider_name} -> {provider_name}")
            break

        if mailbox is None or mailbox_account is None:
            return None, f"无法初始化账号邮箱 provider: {last_error or '没有可用邮箱资源'}"

        before_ids = set()
        if not include_existing:
            try:
                before_ids = set(mailbox.get_current_ids(mailbox_account) or set())
                log_fn(
                    f"  获取rt: 邮箱 OTP 基线已读取 provider={selected_provider_name} "
                    f"email={selected_mailbox_email} before_ids={len(before_ids)}"
                )
            except Exception as exc:
                log_fn(f"  获取rt: 邮箱 OTP 基线读取失败，继续等待新验证码: {exc}")

        def _otp_callback():
            log_fn(f"  获取rt: 等待真实邮箱 OTP provider={selected_provider_name} email={selected_mailbox_email}")
            return mailbox.wait_for_code(
                mailbox_account,
                keyword="",
                timeout=max(int(timeout_seconds or 600), 1),
                before_ids=before_ids or None,
            )

        return _otp_callback, ""

    def _handle_get_rt(self, account: Account, params: dict) -> dict:
        """通过浏览器 OAuth 获取 refresh_token（真实邮箱 OTP + 真实手机号 OTP）。

        参数：
          browser_mode: 浏览器模式
          sms_provider: 手机接码渠道（smspool / smsapi，空=不启用手机验证）
          smspool_api_key: SMSPool API key
          smspool_max_price: SMSPool 价格上限 USD
          smsapi_phone: smsapi 固定手机号
          smsapi_url: smsapi 查询短信 API URL
        """
        log_fn = getattr(self, "log", print)
        cancel_fn = getattr(self, "_cancel_check_fn", None)

        requested_browser_mode = str(params.get("browser_mode") or "camoufox_headed")
        browser_mode = resolve_runtime_browser_mode(requested_browser_mode)
        if browser_mode != requested_browser_mode.strip().lower():
            log_fn("  当前云端环境未检测到 DISPLAY，Camoufox 已自动切换为后台模式")
        record_har = _bool_param(params, "record_har", False)
        proxy = self.config.proxy if self.config else None

        if not account.password:
            return {"ok": False, "error": "账号缺少密码，无法进行 OAuth 登录"}

        acquired_profile_id = ""
        bit_profile_id = ""

        try:
            from platforms._browser_backend import parse_checkout_mode
            from platforms.chatgpt.browser_register import (
                ChatGPTBrowserRegister,
                _build_proxy_config,
                _do_codex_oauth,
            )
            from platforms.chatgpt.browser_get_rt import (
                setup_oauth_state_capture,
                build_get_rt_phone_callback,
            )

            # ★ BitBrowser 模式：自动从 Profile 池获取可用的 profile ID
            if str(browser_mode or "").startswith("bitbrowser_"):
                from application.bitbrowser_profiles import (
                    acquire_profile_for_browser_mode,
                )
                bit_profile_id, acquired_profile_id = acquire_profile_for_browser_mode(
                    browser_mode,
                    fallback=bit_profile_id,
                    log_fn=log_fn,
                )

            backend_config = parse_checkout_mode(browser_mode, bit_profile_id=bit_profile_id)
            record_har_path = _build_get_rt_har_path(account.email) if record_har else None
            if record_har and not backend_config.is_camoufox:
                log_fn(
                    f"  get_rt HAR capture skipped: browser_mode={browser_mode} "
                    "does not support Playwright record_har_path"
                )
                record_har_path = None
            otp_callback, otp_error = self._build_get_rt_mailbox_otp_callback(account, log_fn, proxy)
            if not otp_callback:
                return {"ok": False, "error": f"获取rt失败: {otp_error}"}

            # ★ 手机号 OTP 回调（可选）
            phone_callback = None
            sms_provider = str(params.get("sms_provider") or "").strip().lower()
            supplied_phone_callback = params.get("phone_callback")
            if callable(supplied_phone_callback):
                phone_callback = supplied_phone_callback
                log_fn(f"  获取rt: 使用任务级手机号复用 callback provider={sms_provider or '(unknown)'}")
            elif sms_provider:
                phone_callback, phone_error = build_get_rt_phone_callback(
                    sms_provider=sms_provider,
                    smspool_api_key=str(params.get("smspool_api_key") or ""),
                    smspool_max_price=str(params.get("smspool_max_price") or "0.13"),
                    smsapi_phone=str(params.get("smsapi_phone") or ""),
                    smsapi_url=str(params.get("smsapi_url") or ""),
                    log_fn=log_fn,
                )
                if not phone_callback:
                    log_fn(f"  获取rt: 手机 OTP 回调创建失败: {phone_error}，继续仅邮箱流程")
                else:
                    log_fn(f"  获取rt: 手机 OTP 已就绪 provider={sms_provider}")

            log_fn(f"获取rt: {account.email}, browser_mode={browser_mode}, sms={sms_provider or '(无)'}")

            class _CallbackEmailService:
                service_type = type("ST", (), {"value": "mailbox"})()

                @staticmethod
                def begin_new_otp_wait():
                    return None

                @staticmethod
                def get_verification_code(**_kwargs):
                    return otp_callback()

            if not record_har:
                try:
                    from platforms.chatgpt.protocol_phone import ChatGPTProtocolEmailThenPhoneWorker

                    log_fn("  获取rt: 优先使用协议 OAuth 登录")
                    protocol_result = ChatGPTProtocolEmailThenPhoneWorker(
                        email_service=_CallbackEmailService(),
                        phone_callback=phone_callback or (lambda: ""),
                        proxy_url=proxy,
                        log_fn=log_fn,
                        cancel_check=cancel_fn,
                        max_phone_attempts=1,
                    ).run_for_account(email=account.email, password=account.password)
                    protocol_refresh_token = str(protocol_result.get("refresh_token") or "")
                    if protocol_refresh_token:
                        protocol_access_token = str(protocol_result.get("access_token") or "")
                        log_fn(f"  获取rt协议模式成功: {account.email}")
                        return {
                            "ok": True,
                            "data": {
                                "access_token": protocol_access_token,
                                "refresh_token": protocol_refresh_token,
                                "id_token": str(protocol_result.get("id_token") or ""),
                                "account_id": str(account.user_id or ""),
                                "email": account.email,
                                "record_har_path": "",
                                "message": "refresh_token 获取成功",
                            },
                        }
                    log_fn("  获取rt协议模式未返回 RT，继续浏览器 OAuth")
                except Exception as protocol_exc:
                    log_fn(f"  获取rt协议模式未完成，继续浏览器 OAuth: {protocol_exc}")

            # 创建一个只用于 get_rt 的轻量 register 实例
            reg = ChatGPTBrowserRegister(
                headless=backend_config.is_headless,
                proxy=proxy,
                log_fn=log_fn,
                backend_config=backend_config,
            )

            if reg.backend_config.is_bitbrowser:
                launch_opts = {"headless": reg.backend_config.is_headless}
            else:
                cam_proxy = _build_proxy_config(reg.proxy)
                launch_opts = {"headless": reg.headless}
                if cam_proxy:
                    launch_opts["proxy"] = cam_proxy

            with reg._open_browser(launch_opts) as browser:
                har_context = None
                if record_har_path:
                    try:
                        os.makedirs(os.path.dirname(record_har_path), exist_ok=True)
                        har_context = browser.new_context(
                            record_har_path=record_har_path,
                            record_har_url_filter="**/*",
                        )
                        page = har_context.new_page()
                        log_fn(f"  get_rt HAR capture enabled: {record_har_path}")
                    except Exception as exc:
                        log_fn(f"  get_rt HAR capture init failed, continue without HAR: {exc}")
                        record_har_path = None
                        har_context = None
                        page = browser.new_page()
                else:
                    page = browser.new_page()

                try:
                    setup_oauth_state_capture(page, log=log_fn)
                    log_fn("  获取rt: 浏览器已打开，开始 OAuth...")

                    if callable(cancel_fn) and cancel_fn():
                        return {"ok": False, "error": "任务已取消"}

                    result = _do_codex_oauth(
                        page, {}, account.email, account.password,
                        otp_callback,
                        phone_callback,
                        proxy, log_fn,
                    )

                    if not isinstance(result, dict) or not result.get("access_token"):
                        error_detail = "OAuth 未返回 token"
                        if isinstance(result, dict):
                            error_detail = str(result.get("error") or result.get("detail") or error_detail)
                        return {"ok": False, "error": f"获取rt失败: {error_detail}"}

                    refresh_token = str(result.get("refresh_token") or "")
                    access_token = str(result.get("access_token") or "")
                    log_fn(
                        f"  获取rt成功: {account.email}"
                        f" access_token={access_token[:20]}..."
                        f" refresh_token={'有' if refresh_token else '无'}"
                    )

                    return {
                        "ok": True,
                        "data": {
                            "access_token": access_token,
                            "refresh_token": refresh_token,
                            "id_token": str(result.get("id_token") or ""),
                            "account_id": str(result.get("account_id") or ""),
                            "email": account.email,
                            "record_har_path": record_har_path or "",
                            "message": "refresh_token 获取成功" if refresh_token else "access_token 获取成功（无 refresh_token）",
                        },
                    }
                finally:
                    if har_context is not None:
                        try:
                            har_context.close()
                            log_fn(f"  get_rt HAR saved: {record_har_path}")
                        except Exception as exc:
                            log_fn(f"  get_rt HAR context close failed: {exc}")

        except Exception as exc:
            log_fn(f"  获取rt异常: {exc}")
            return {"ok": False, "error": f"获取rt异常: {exc}"}
        finally:
            if acquired_profile_id:
                try:
                    from application.bitbrowser_profiles import release_acquired_profile
                    release_acquired_profile(acquired_profile_id, log_fn=log_fn)
                except Exception:
                    pass

    def _handle_get_rt_bypass(self, account: Account, params: dict) -> dict:
        """通过浏览器 OAuth 获取 refresh_token（session/select 拦截绕过手机验证）。

        与 _handle_get_rt 的区别：
          - 不接真实手机号，不调 smspool/smsapi
          - 用 Playwright route 拦截 POST session/select 响应，
            把 phone_otp_* 替换为 consent 类型，让浏览器直接跳 consent
          - 邮箱 OTP 仍需真实接码

        参数：
          browser_mode: 浏览器模式
        """
        log_fn = getattr(self, "log", print)
        cancel_fn = getattr(self, "_cancel_check_fn", None)

        requested_browser_mode = str(params.get("browser_mode") or "camoufox_headed")
        browser_mode = resolve_runtime_browser_mode(requested_browser_mode)
        if browser_mode != requested_browser_mode.strip().lower():
            log_fn("  当前云端环境未检测到 DISPLAY，Camoufox 已自动切换为后台模式")
        proxy = self.config.proxy if self.config else None

        if not account.password:
            return {"ok": False, "error": "账号缺少密码，无法进行 OAuth 登录"}

        acquired_profile_id = ""
        bit_profile_id = ""

        try:
            from platforms._browser_backend import parse_checkout_mode
            from platforms.chatgpt.browser_register import (
                ChatGPTBrowserRegister,
                _build_proxy_config,
                _do_codex_oauth,
            )
            from platforms.chatgpt.browser_get_rt import setup_phone_otp_skip_interception
            from platforms.chatgpt.oauth import generate_oauth_url
            from platforms.chatgpt.constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE

            # 预生成 OAuth 参数（curl fallback 用）
            oauth_start = generate_oauth_url(
                redirect_uri=CODEX_REDIRECT_URI,
                scope=CODEX_SCOPE,
                client_id=CODEX_CLIENT_ID,
            )

            if str(browser_mode or "").startswith("bitbrowser_"):
                from application.bitbrowser_profiles import (
                    acquire_profile_for_browser_mode,
                )
                bit_profile_id, acquired_profile_id = acquire_profile_for_browser_mode(
                    browser_mode,
                    fallback=bit_profile_id,
                    log_fn=log_fn,
                )

            backend_config = parse_checkout_mode(browser_mode, bit_profile_id=bit_profile_id)
            otp_callback, otp_error = self._build_get_rt_mailbox_otp_callback(account, log_fn, proxy)
            if not otp_callback:
                return {"ok": False, "error": f"获取rt失败: {otp_error}"}

            log_fn(f"获取rt(绕过): {account.email}, browser_mode={browser_mode}")

            reg = ChatGPTBrowserRegister(
                headless=backend_config.is_headless,
                proxy=proxy,
                log_fn=log_fn,
                backend_config=backend_config,
            )

            if reg.backend_config.is_bitbrowser:
                launch_opts = {"headless": reg.backend_config.is_headless}
            else:
                cam_proxy = _build_proxy_config(reg.proxy)
                launch_opts = {"headless": reg.headless}
                if cam_proxy:
                    launch_opts["proxy"] = cam_proxy

            with reg._open_browser(launch_opts) as browser:
                page = browser.new_page()
                setup_phone_otp_skip_interception(page, log=log_fn)
                log_fn("  获取rt(绕过): session/select 拦截器已就绪（phone_otp→consent）")

                if callable(cancel_fn) and cancel_fn():
                    return {"ok": False, "error": "任务已取消"}

                result = _do_codex_oauth(
                    page, {}, account.email, account.password,
                    otp_callback,
                    None,
                    proxy, log_fn,
                    oauth_start=oauth_start,
                )

                # ★ Fallback: curl 补全会话 (workspace/select → callback)
                if not isinstance(result, dict) or not result.get("access_token"):
                    import time as _time, json as _json, re as _re
                    from platforms.chatgpt.browser_register import _get_cookies
                    cookies_dict = _get_cookies(page)
                    log_fn("  获取rt(绕过): _do_codex_oauth 退出，curl 补全...")
                    try:
                        import curl_cffi.requests as _curl_requests
                        s = _curl_requests.Session()
                        cookie_parts = [f'{k}={v}' for k, v in cookies_dict.items() if v]
                        cookie_header = '; '.join(cookie_parts)
                        headers = {
                            "accept": "application/json",
                            "origin": "https://auth.openai.com",
                            "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "cookie": cookie_header,
                        }
                        workspace_id = ""
                        # Try 1: client_auth_session_dump
                        dump_resp = s.get("https://auth.openai.com/api/accounts/client_auth_session_dump",
                            headers=headers, timeout=30, impersonate="chrome")
                        log_fn(f"  获取rt(绕过): client_auth_session_dump -> {dump_resp.status_code}")
                        if dump_resp.status_code < 400:
                            dump_data = dump_resp.json() if dump_resp.text else {}
                            workspaces = dump_data.get("workspaces") or []
                            if workspaces:
                                workspace_id = str(workspaces[0].get("id") or "")
                            log_fn(f"  获取rt(绕过): dump workspaces={len(workspaces)}")

                        # Try 2: use account's user_id as workspace_id
                        if not workspace_id and account.user_id:
                            workspace_id = account.user_id
                            log_fn(f"  获取rt(绕过): 尝试 user_id={workspace_id[:20]}...")

                        # Try 3: POST workspace/select with each candidate
                        for ws_candidate in [workspace_id] if workspace_id else []:
                            ws_resp = s.post(
                                "https://auth.openai.com/api/accounts/workspace/select",
                                data=_json.dumps({"workspace_id": ws_candidate}),
                                headers={**headers, "content-type": "application/json"},
                                allow_redirects=False, timeout=30, impersonate="chrome",
                            )
                            log_fn(f"  获取rt(绕过): workspace/select({ws_candidate[:16]}...) -> {ws_resp.status_code}")
                            if ws_resp.status_code < 400:
                                ws_data = ws_resp.json() if ws_resp.text else {}
                                cb_url = str(ws_data.get("continue_url") or "")
                                # Also check Location header
                                if not cb_url:
                                    cb_url = str(ws_resp.headers.get("Location") or "")
                                if "code=" in cb_url or "localhost:1455" in cb_url:
                                    m = _re.search(r'state=([^&\s]+)', cb_url)
                                    cb_state = m.group(1) if m else oauth_start.state
                                    from platforms.chatgpt.oauth import submit_callback_url
                                    result_json = submit_callback_url(
                                        callback_url=cb_url, expected_state=cb_state,
                                        code_verifier=oauth_start.code_verifier,
                                        redirect_uri=oauth_start.redirect_uri,
                                        client_id=oauth_start.client_id, proxy_url=proxy,
                                    )
                                    result = _json.loads(result_json)
                                    log_fn("  获取rt(绕过): curl workspace/select 补全成功!")
                                    break
                    except Exception as curl_exc:
                        log_fn(f"  获取rt(绕过): curl 补全异常: {curl_exc}")

                if not isinstance(result, dict) or not result.get("access_token"):
                    error_detail = "OAuth 未返回 token"
                    if isinstance(result, dict):
                        error_detail = str(result.get("error") or result.get("detail") or error_detail)
                    return {"ok": False, "error": f"获取rt失败: {error_detail}"}

                refresh_token = str(result.get("refresh_token") or "")
                access_token = str(result.get("access_token") or "")
                id_token = str(result.get("id_token") or "")
                result_data = dict(result)
                id_token_claims = {}
                try:
                    from platforms.chatgpt.oauth import _jwt_claims_no_verify
                    id_token_claims = _jwt_claims_no_verify(id_token)
                    if id_token_claims:
                        result_data["id_token_claims"] = id_token_claims
                except Exception:
                    id_token_claims = {}
                profile = {}
                try:
                    from platforms.chatgpt.browser_oauth import _fetch_profile
                    profile = _fetch_profile(access_token, proxy=proxy)
                    if profile:
                        result_data["profile"] = profile
                        result_data["remote_user"] = profile
                except Exception as exc:
                    log_fn(f"  获取rt: profile 拉取失败（忽略）: {exc}")
                resolved_email = str(
                    result_data.get("email")
                    or (profile.get("email") if isinstance(profile, dict) else "")
                    or account.email
                )
                result_data.update(
                    {
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "id_token": id_token,
                        "account_id": str(result.get("account_id") or ""),
                        "email": resolved_email,
                        "message": "refresh_token 获取成功" if refresh_token else "access_token 获取成功（无 refresh_token）",
                    }
                )
                log_fn(
                    f"  获取rt成功: {account.email}"
                    f" access_token={access_token[:20]}..."
                    f" refresh_token={'有' if refresh_token else '无'}"
                )

                return {
                    "ok": True,
                    "data": result_data,
                }

        except Exception as exc:
            log_fn(f"  获取rt异常: {exc}")
            return {"ok": False, "error": f"获取rt异常: {exc}"}
        finally:
            if acquired_profile_id:
                try:
                    from application.bitbrowser_profiles import release_acquired_profile
                    release_acquired_profile(acquired_profile_id, log_fn=log_fn)
                except Exception:
                    pass

    def _build_turnstile_solver_for_checkout(self):
        """构造给 Camoufox checkout 用的验证码求解回调。

        PayPal security challenge 只使用 YesCaptcha；如未配置可用 YesCaptcha，则返回
        None，让 checkout 流程退化为人工等待。
        """
        log_fn = getattr(self, "_log_fn", print)
        try:
            if not self._has_configured_captcha("yescaptcha_api"):
                log_fn("未启用验证码自动求解（YesCaptcha 未配置）")
                return None
            captcha_solver = self._make_captcha(provider_key="yescaptcha_api")
        except Exception as exc:
            log_fn(f"未启用验证码自动求解（YesCaptcha 初始化失败: {exc}）")
            return None
        log_fn("已启用验证码自动求解，provider: YesCaptcha")

        def _solver(page_url: str, site_key: str, challenge_type: str = "turnstile") -> str:
            if challenge_type == "recaptcha_v2":
                return captcha_solver.solve_recaptcha_v2(page_url, site_key)
            # 本地协议抓包显示：``paypal.com/pay/`` 风控页是 hCaptcha (iframe src 含
            # ``hcaptcha_fph.html?siteKey=...``)，必须走 ``solve_hcaptcha`` 才能拿到
            # 可注入到 ``form[name=challenge]`` 里的 ``g-recaptcha-response`` token。
            if challenge_type == "hcaptcha":
                return captcha_solver.solve_hcaptcha(page_url, site_key)
            return captcha_solver.solve_turnstile(page_url, site_key)

        return _solver

    def _handle_generate_link(self, account: Account, params: dict) -> dict:
        """Handle generate_link capability for ChatGPT.

        **行为变更**（"打开支付链接"语义）：账号 ``extra`` 里已存了
        ``cashier_url`` 时优先把它**直接返回**——前端拿到 URL 就在新标签
        页打开。这样"打开支付链接"按钮就跟字面意思一致了：注册阶段已生成
        过的链接直接复用，不再每次都重新打 ChatGPT 后端 API 创建新会话。

        ``params`` 里若显式传 ``regenerate=true`` 则跳过这条路径，强制重新
        生成（用于链接过期 / 想要换 country/currency 等场景）。
        """
        self.raise_if_cancelled()
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        regenerate = _bool_param(params, "regenerate", False)
        if not regenerate:
            existing_url = str(
                extra.get("cashier_url")
                or (extra.get("account_overview") or {}).get("cashier_url")
                or ""
            ).strip()
            if existing_url:
                getattr(self, "_log_fn", print)(
                    f"复用账号已有 cashier_url（不重新生成）: {existing_url}"
                )
                return {
                    "ok": True,
                    "data": {
                        "url": existing_url,
                        "checkout_url": existing_url,
                        "cashier_url": existing_url,
                        "plan": params.get("plan", "plus"),
                        "auto_checkout": False,
                        "message": "支付链接已存在，直接打开",
                        "reused": True,
                    },
                }

        class _A: pass
        a = _A()
        a.email = account.email
        a.password = account.password
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")

        from platforms.chatgpt import payment as payment_module
        plan = params.get("plan", "plus")
        country = params.get("country", "ID")
        currency = params.get("currency") or None
        # 用 Stripe payment_pages/init 协议生成 cashier_url（accessToken →
        # pay.openai.com 长链，纯协议、不开浏览器拿 cashier 链）。仅 plus 生效。
        use_stripe_init = _bool_param(params, "use_stripe_init", False)
        # 短链：checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc 短链。仅 plus。
        use_short_link = _bool_param(params, "use_short_link", False)
        # 账单地址来源（meiguodizhi.com 接口）："US" 走 ``/``，"JP" 走 ``/jp-address``。
        # 默认 US 保持向下兼容；其它值在 fetch_billing_address 里 fallback US。
        address_region = str(params.get("address_region") or "US").strip().upper() or "US"
        auto_checkout = _bool_param(params, "auto_checkout", True)
        payment_method = str(params.get("payment_method") or "paypal").strip().lower()
        headless = _bool_param(params, "headless", False)
        checkout_timeout = _int_param(params, "checkout_timeout", 180)
        checkout_hold_seconds = _optional_int_param(params, "checkout_hold_seconds")
        record_har = _bool_param(params, "record_har", False)
        record_har_path = _build_checkout_har_path(account.email) if record_har else None
        checkout_mode = str(params.get("checkout_mode") or "").strip().lower()
        if not checkout_mode:
            checkout_mode = "camoufox_headless" if headless else "camoufox_headed"
        # bitbrowser_* 模式下必须有 profile ID。表单输入优先于环境变量。
        bit_profile_id = str(params.get("bit_profile_id") or "").strip()
        if not bit_profile_id:
            bit_profile_id = os.environ.get("BIT_PROFILE_ID", "").strip()
        # 把 checkout_mode 翻成 BrowserBackendConfig；protocol 模式不需要 backend
        # （这里给 None，下游 _run_camoufox 不会被调用到）。
        backend_config: BrowserBackendConfig | None = None
        # acquired_profile_id 记录"是从池里 acquire 出来的"，跑完要 release。
        # 表单/环境变量传进来的不在池里，不需要 release。
        acquired_profile_id: str = ""
        if checkout_mode.startswith("bitbrowser_"):
            window_mode = checkout_mode[len("bitbrowser_"):]
            # 优先从 BitBrowser profile 池里 acquire 一个最少使用的 profile。
            # 池为空时回落到表单/环境变量提供的单一 ID（保持向后兼容）。
            from application.bitbrowser_profiles import (
                bitbrowser_profile_pool,
                BitBrowserProfilePoolEmpty,
            )
            try:
                resolved_profile_id = bitbrowser_profile_pool.acquire_or(
                    fallback=bit_profile_id
                )
                # 判断是不是真的从池里 acquire 的（影响 release）：池里有这个
                # ID 就视为"从池里出来的"，否则视为 fallback。
                pool_ids = {
                    item["profile_id"]
                    for item in bitbrowser_profile_pool.list_profiles()
                }
                if resolved_profile_id in pool_ids:
                    acquired_profile_id = resolved_profile_id
            except BitBrowserProfilePoolEmpty:
                # 池空 + 没 fallback → fail-fast，避免下到 BitBrowser API 才报错
                return {
                    "ok": False,
                    "error": (
                        "checkout_mode=bitbrowser_* 需要在「设置 → BitBrowser」"
                        "里添加 profile ID，或在表单里填写 BitBrowser Profile ID"
                        "（也可设置 BIT_PROFILE_ID 环境变量）"
                    ),
                }
            backend_config = BrowserBackendConfig.bitbrowser(
                profile_id=resolved_profile_id,
                window_mode=window_mode,
                api_url=os.environ.get("BIT_API_URL", "").strip() or None,
                api_token=os.environ.get("BIT_API_TOKEN", "").strip() or None,
            )
            getattr(self, "_log_fn", print)(
                f"BitBrowser profile 已选择: {resolved_profile_id} "
                f"(window_mode={window_mode}, "
                f"来源={'profile 池' if acquired_profile_id else '表单/环境变量'})"
            )
        elif checkout_mode in ("camoufox_headless", "camoufox_headed"):
            backend_config = BrowserBackendConfig.camoufox(
                headless=(checkout_mode == "camoufox_headless"),
            )
        # 解析 SMS 号码池：多行 +phone----relay_url。失败行会被静默忽略，
        # 这里只保留结构化后的非空列表，避免后续 stage / camoufox 反复字符串处理。
        sms_pool_raw = str(params.get("sms_pool") or "")
        try:
            sms_pool = payment_module.parse_sms_pool(sms_pool_raw)
        except Exception as exc:  # 防御性：解析失败也不应阻塞 checkout
            sms_pool = []
            getattr(self, "_log_fn", print)(f"SMS 号码池解析失败（忽略）: {exc}")
        if sms_pool_raw and not sms_pool:
            getattr(self, "_log_fn", print)(
                "警告：sms_pool 提供了内容但没解析出任何条目，请按 `+phone----relay_url` 格式排查"
            )
        elif sms_pool:
            getattr(self, "_log_fn", print)(f"SMS 号码池已加载 {len(sms_pool)} 条")
        checkout_proxy = None

        # Manually construct basic cookie in case old accounts don't have complete cookie string
        if not a.cookies and a.session_token:
            a.cookies = f"__Secure-next-auth.session-token={a.session_token}"

        getattr(self, "_log_fn", print)("生成 ChatGPT 测试支付链接不使用代理")
        if plan == "plus":
            if use_short_link:
                getattr(self, "_log_fn", print)(
                    "cashier_url 走短链模式（checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc）"
                )
            elif use_stripe_init:
                getattr(self, "_log_fn", print)(
                    "cashier_url 走 Stripe init 协议长链（accessToken → pay.openai.com，纯协议）"
                )
            generate_kwargs = {}
            if use_stripe_init or use_short_link:
                generate_kwargs["use_stripe_init"] = use_stripe_init
                generate_kwargs["use_short_link"] = use_short_link
            url = payment_module.generate_plus_link(
                a, proxy=None, country=country, currency=currency, **generate_kwargs
            )
        else:
            url = payment_module.generate_team_link(a, proxy=None, country=country, currency=currency)
        self.raise_if_cancelled()

        cashier_url = url
        paypal_authorize_url = ""
        paypal_protocol_extract = None
        checkout_automation = None
        if url and auto_checkout:
            checkout_proxy = proxy
            if not checkout_proxy:
                proxy_region = str(params.get("proxy_region") or country or getattr(account, "region", "") or "").strip().upper()
                checkout_proxy = proxy_pool.get_next(region=proxy_region)
            if checkout_proxy:
                getattr(self, "_log_fn", print)(f"Camoufox checkout 使用代理: {_mask_proxy(checkout_proxy)}")
            else:
                getattr(self, "_log_fn", print)("Camoufox checkout 未配置代理")
            getattr(self, "_log_fn", print)("支付链接已生成，开始自动 PayPal checkout")
            getattr(self, "_log_fn", print)(f"checkout 模式: {checkout_mode}")
            # 是否启用 YesCaptcha 远端求解（前端弹窗里的开关）。
            # 关闭时 turnstile_solver 强制为 None，payment 模块的 captcha
            # 路径会退化为"代码鼠标点击 + 10s 等待跳转"，避免反复在
            # YesCaptcha 不识别的 sitekey 上烧配额。
            use_captcha_service = _bool_param(params, "use_captcha_service", True)
            if use_captcha_service:
                turnstile_solver = self._build_turnstile_solver_for_checkout()
            else:
                getattr(self, "_log_fn", print)(
                    "已禁用 YesCaptcha 求解（弹窗开关），captcha 出现时仅自动点击 + 等 10s"
                )
                turnstile_solver = None
            log_fn = getattr(self, "_log_fn", print)

            protocol_extract_failed = False
            if plan == "plus" and use_stripe_init and checkout_mode != "protocol":
                gateway_url = os.environ.get("PAYPAL_PROTOCOL_GATEWAY_URL", "").strip()
                if gateway_url:
                    paypal_protocol_extract = payment_module.extract_paypal_authorize_link_go(
                        access_token=a.access_token,
                        proxy=checkout_proxy,
                        gateway_url=gateway_url,
                        timeout=checkout_timeout,
                        log_fn=log_fn,
                    )
                else:
                    gateway_error = (
                        "未配置 PAYPAL_PROTOCOL_GATEWAY_URL，协议长链模式需要先启动 Go gateway；"
                        "当前不会回落 Python Stripe direct confirm"
                    )
                    log_fn(f"协议长链模式：{gateway_error}")
                    paypal_protocol_extract = {
                        "ok": False,
                        "status": "failed",
                        "paypal_authorize_url": "",
                        "error": gateway_error,
                        "protocol_backend": "go",
                    }
                if paypal_protocol_extract.get("ok"):
                    paypal_authorize_url = str(paypal_protocol_extract.get("paypal_authorize_url") or "")
                    if paypal_authorize_url:
                        url = paypal_authorize_url
                        log_fn("协议长链模式：已生成 PayPal 授权长链接，交给浏览器自动填写流程")
                    else:
                        protocol_extract_failed = True
                        checkout_automation = {
                            "ok": False,
                            "status": "failed",
                            "error": "协议提取成功但未返回 PayPal 授权长链接",
                            "protocol_extract": paypal_protocol_extract,
                        }
                else:
                    protocol_extract_failed = True
                    checkout_automation = paypal_protocol_extract
                    log_fn(
                        "协议长链模式提取 PayPal 授权长链接失败: "
                        + str(paypal_protocol_extract.get("error", "") or "unknown error")
                    )

            def _run_camoufox(headless_flag: bool):
                # 名字保留 _run_camoufox 兼容老日志/调用方，实际后端由
                # backend_config 决定（Camoufox / BitBrowser）。
                backend_label = (
                    f"BitBrowser({backend_config.window_mode})"
                    if backend_config and backend_config.is_bitbrowser
                    else f"Camoufox(headless={headless_flag})"
                )
                log_fn(
                    f"切换到独立线程执行 checkout backend={backend_label}"
                )
                return _run_sync_checkout_isolated(
                    payment_module.complete_paypal_checkout,
                    checkout_url=url,
                    cookies_str=a.cookies,
                    proxy=checkout_proxy,
                    email=account.email,
                    payment_method=payment_method,
                    headless=headless_flag,
                    timeout=checkout_timeout,
                    hold_seconds=checkout_hold_seconds,
                    log_fn=log_fn,
                    cancel_check=self.is_cancel_requested,
                    turnstile_solver=turnstile_solver,
                    record_har_path=record_har_path,
                    sms_pool=sms_pool,
                    backend_config=backend_config,
                    phone_swap_callback=params.get("phone_swap_callback"),
                    address_region=address_region,
                )

            def _run_protocol():
                log_fn("启动协议模式 checkout")
                return _run_sync_checkout_isolated(
                    payment_module.complete_paypal_checkout_protocol,
                    checkout_url=url,
                    cookies_str=a.cookies,
                    proxy=checkout_proxy,
                    email=account.email,
                    payment_method=payment_method,
                    timeout=checkout_timeout,
                    log_fn=log_fn,
                    cancel_check=self.is_cancel_requested,
                    turnstile_solver=turnstile_solver,
                    sms_pool=sms_pool,
                    address_region=address_region,
                )

            if checkout_mode == "protocol":
                # 协议模式失败时**直接报错**，不再自动回落 camoufox。
                # 理由：camoufox 兜底会掩盖协议链的真实失败原因，让调试变难；
                # 而且每次跑都要等 camoufox 启动 + 浏览器自动化，浪费时间。
                # 真要 fallback 的话，由前端在外层切换 checkout_mode 重新发起。
                checkout_automation = _run_protocol()
                if checkout_automation and not checkout_automation.get("ok"):
                    proto_err = str(checkout_automation.get("error", "") or "").strip()
                    log_fn(
                        "协议模式 checkout 失败（stage="
                        + str(checkout_automation.get("stage", "?"))
                        + "），不再回落 camoufox（便于排查）"
                        + (f"；原因: {proto_err}" if proto_err else "")
                    )
            else:
                try:
                    if not protocol_extract_failed:
                        checkout_automation = _run_camoufox(
                            headless_flag=(checkout_mode == "camoufox_headless")
                        )
                finally:
                    # BitBrowser 池里 acquire 出来的 profile，跑完后释放计数，
                    # 让下一次并发能挑到当前没在用的 profile。表单/环境变量
                    # 传的 ID 不在池里，acquired_profile_id 是空字符串，
                    # release 是 no-op。
                    if acquired_profile_id:
                        try:
                            from application.bitbrowser_profiles import (
                                bitbrowser_profile_pool,
                            )
                            bitbrowser_profile_pool.release(acquired_profile_id)
                            log_fn(
                                f"BitBrowser profile 池已释放: {acquired_profile_id}"
                            )
                        except Exception as exc:
                            log_fn(f"BitBrowser profile 池释放失败（忽略）: {exc}")
            self.raise_if_cancelled()
            if checkout_automation.get("ok"):
                getattr(self, "_log_fn", print)("PayPal checkout 自动流程已提交")
            else:
                checkout_error = str(checkout_automation.get("error", "") or "PayPal checkout automation failed")
                getattr(self, "_log_fn", print)(f"PayPal checkout 自动流程失败: {checkout_error}")

        checkout_ok = bool(checkout_automation and checkout_automation.get("ok"))
        action_ok = bool(url) if not auto_checkout else bool(url and checkout_ok)
        action_error = ""
        if url and auto_checkout and not checkout_ok:
            action_error = str(
                (checkout_automation or {}).get("error", "")
                or "PayPal checkout automation failed"
            )

        data = {
            "url": url,
            "checkout_url": url,
            "cashier_url": cashier_url,
            "paypal_authorize_url": paypal_authorize_url,
            "plan": plan,
            "country": country,
            "currency": currency or "",
            "payment_method": payment_method,
            "auto_checkout": auto_checkout,
            "headless": headless,
            "checkout_mode": checkout_mode,
            "proxy_used": checkout_proxy or "",
            "record_har_path": record_har_path or "",
            "message": (
                "Payment link generated, PayPal checkout automation submitted."
                if checkout_ok
                else (
                    "Payment link generated, but PayPal checkout automation failed."
                    if url and auto_checkout
                    else "Payment link generated."
                )
            ),
        }
        if paypal_protocol_extract is not None:
            data["paypal_protocol_extract"] = paypal_protocol_extract
        if checkout_automation is not None:
            data["checkout_automation"] = checkout_automation

        return {
            "ok": action_ok,
            "data": data,
            "error": action_error,
        }

    
