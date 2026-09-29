"""Cursor 平台插件"""
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.cursor.core import UA, CURSOR


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


@register
class CursorPlatform(BasePlatform):
    name = "cursor"
    display_name = "Cursor"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    protocol_captcha_order = ("2captcha", "capsolver", "auto")

    capabilities = ["switch_desktop", "query_state"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password

    def _map_mailbox_result(self, result: dict) -> RegistrationResult:
        return RegistrationResult(
            email=result["email"],
            password=result.get("password", ""),
            token=result.get("token", ""),
            status=AccountStatus.REGISTERED,
        )

    def _map_oauth_result(self, result: dict) -> RegistrationResult:
        return RegistrationResult(
            email=result["email"],
            password="",
            token=result.get("token", ""),
            status=AccountStatus.REGISTERED,
            extra={"user_info": result.get("user_info", {})},
        )

    def _run_browser_oauth(self, ctx) -> dict:
        from platforms.cursor.browser_oauth import register_with_browser_oauth

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

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_oauth_result(result) if ctx.identity.identity_provider == "oauth_browser" else self._map_mailbox_result(result),
            browser_worker_builder=lambda ctx, artifacts: __import__("platforms.cursor.browser_register", fromlist=["CursorBrowserRegister"]).CursorBrowserRegister(
                captcha=artifacts.captcha_solver,
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
            ),
            oauth_runner=self._run_browser_oauth,
            capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
            otp_spec=OtpSpec(wait_message="等待 Cursor 邮箱验证码...", success_label="验证码"),
            use_captcha_for_mailbox=True,
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_browser_oauth,
            result_mapper=lambda ctx, result: self._map_oauth_result(result),
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_mailbox_result(result),
            worker_builder=lambda ctx, artifacts: __import__("platforms.cursor.protocol_mailbox", fromlist=["CursorProtocolMailboxWorker"]).CursorProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
                otp_callback=artifacts.otp_callback,
                captcha_solver=artifacts.captcha_solver,
            ),
            otp_spec=OtpSpec(wait_message="等待验证码..."),
            use_captcha=True,
        )

    def check_valid(self, account: Account) -> bool:
        from curl_cffi import requests as curl_req
        try:
            r = curl_req.get(
                f"{CURSOR}/api/auth/me",
                headers={"Cookie": f"WorkosCursorSessionToken={account.token}",
                         "user-agent": UA},
                impersonate="chrome124", timeout=15,
            )
            return r.status_code == 200
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [
            {"id": "switch_account", "label": "切换到桌面应用", "params": []},
            {"id": "get_account_state", "label": "查询账号状态/用量", "params": []},
        ]

    def get_desktop_state(self) -> dict:
        from platforms.cursor.switch import get_cursor_desktop_state

        return get_cursor_desktop_state()

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        from platforms.cursor.switch import (
            get_cursor_desktop_state,
            get_cursor_usage,
            get_cursor_user_info,
            read_current_cursor_account,
            restart_cursor_ide,
            summarize_cursor_usage,
            switch_cursor_account,
        )

        token = account.token
        if not token:
            return {"ok": False, "error": "账号缺少 token"}

        if action_id == "switch_account":
            ok, message = switch_cursor_account(token)
            if not ok:
                return {"ok": False, "error": message}
            restart_ok, restart_message = restart_cursor_ide()
        elif action_id in {"get_user_info", "get_account_state"}:
            message = "账号状态已刷新"
            restart_ok, restart_message = False, ""
        else:
            raise NotImplementedError(f"未知操作: {action_id}")

        user_info = get_cursor_user_info(token) or {}
        usage_info = get_cursor_usage(token, user_info.get("sub", "")) or {}
        current = read_current_cursor_account() or {}
        return {
            "ok": bool(user_info),
            "data": {
                "message": f"{message}。{restart_message}" if restart_ok else message,
                "valid": bool(user_info),
                "remote_user": user_info,
                "usage_info": usage_info,
                "usage_summary": summarize_cursor_usage(usage_info),
                "local_app_account": {
                    "token_preview": _mask_secret(current.get("token", "")),
                    "matches_target": current.get("token") == token if current.get("token") else False,
                },
                "desktop_app_state": get_cursor_desktop_state(),
            },
        }
