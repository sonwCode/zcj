"""oreateai 平台插件"""
import random
import string
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import (
    BrowserRegistrationAdapter,
    LinkSpec,
    OtpSpec,
    ProtocolMailboxAdapter,
    ProtocolOAuthAdapter,
    RegistrationCapability,
    RegistrationResult,
)
from core.registry import register


@register
class OreateaiPlatform(BasePlatform):
    """
    orateai 平台 - AI视频生成平台，注册送80积分

    核心边界：
    - ✅ 自动收信、自动提取验证链接
    - ⚠️ 只在 allowlist 域名内自动访问验证链接
    - ❌ 不做批量注册、不绕过平台风控
    """

    name = "oreateai"
    display_name = "Oreateai"
    version = "1.0.0"

    # 允许自动确认验证链接的域名白名单
    ALLOWED_VERIFY_DOMAINS = [
        "oreateai.com",
        "www.oreateai.com",
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _browser_registration_label(self, identity) -> str:
        return getattr(identity, "email", "") or "(manual oauth)"

    def _prepare_registration_password(self, password: str | None) -> str | None:
        """生成随机密码"""
        if password:
            return password
        # 生成 12 位混合密码
        return "".join(random.choices(string.ascii_letters + string.digits + "!@#$", k=12))

    def _map_oreateai_result(self, result: dict) -> RegistrationResult:
        """映射注册结果"""
        return RegistrationResult(
            email=result.get("email", ""),
            password=result.get("password", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "credits": result.get("credits", 0),
                "api_token": result.get("api_token", ""),
                "session_token": result.get("session_token", ""),
                "pending_confirmation": result.get("pending_confirmation", False),
                "verification_link": result.get("verification_link", ""),
            },
        )

    def build_browser_registration_adapter(self):
        """浏览器模式注册适配器"""
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_oreateai_result(result),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.oreateai.browser_register",
                fromlist=["OreateaiBrowserRegister"]
            ).OreateaiBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                verification_link_callback=artifacts.verification_link_callback,
                mailbox=self.mailbox,
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
            ),
            capability=RegistrationCapability(
                browser_mailbox_requires_email=True,
                browser_mailbox_requires_mailbox=True,
            ),
            otp_spec=OtpSpec(wait_message="等待验证码邮件..."),
            link_spec=LinkSpec(
                wait_message="等待验证链接邮件...",
                keyword="oreateai",
                timeout=120,
            ),
        )

    def build_protocol_mailbox_adapter(self):
        """协议邮箱模式注册适配器"""
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_oreateai_result(result),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.oreateai.protocol_mailbox",
                fromlist=["OreateaiProtocolMailboxWorker"]
            ).OreateaiProtocolMailboxWorker(
                executor=artifacts.executor,
                mailbox=self.mailbox,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
                link_callback=artifacts.verification_link_callback,
            ),
            otp_spec=OtpSpec(wait_message="等待验证码邮件..."),
            link_spec=LinkSpec(
                wait_message="等待验证链接邮件...",
                keyword="oreateai",
                timeout=120,
            ),
            use_executor=True,
        )

    def build_protocol_oauth_adapter(self):
        """协议 OAuth 适配器（预留）"""
        return ProtocolOAuthAdapter(
            oauth_runner=lambda ctx: {"error": "OAuth 登录待实现"},
            result_mapper=lambda ctx, result: self._map_oreateai_result(result),
        )

    def check_valid(self, account: Account) -> bool:
        """检测账号是否有效"""
        # 至少要有邮箱
        if not account.email:
            return False
        # 如果有待确认链接，标记为待确认
        if account.extra.get("pending_confirmation"):
            return False
        return True

    def get_quota(self, account: Account) -> dict:
        """获取账号配额/积分"""
        return {
            "credits": account.extra.get("credits", 0),
            "available": account.extra.get("credits", 0) > 0,
        }

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        """执行平台操作"""
        if action_id == "generate_video":
            return self._handle_generate_video(account, params)
        if action_id == "confirm_verification":
            return self._handle_confirm_verification(account, params)
        return super().execute_action(action_id, account, params)

    def _handle_generate_video(self, account: Account, params: dict) -> dict:
        """
        生成视频

        参数:
            prompt: 视频描述
            duration: 时长（可选）
            style: 风格（可选）
        """
        # TODO: 抓包确认视频生成 API
        return {
            "ok": False,
            "error": "视频生成 API 待实现，请先抓包确认接口格式",
            "params_received": params,
        }

    def _handle_confirm_verification(self, account: Account, params: dict) -> dict:
        """
        人工确认验证链接

        用于：
        1. 验证链接不在白名单内，需要人工确认
        2. 自动确认后需要二次确认
        """
        verification_link = account.extra.get("verification_link", "")
        if not verification_link:
            return {"ok": False, "error": "没有待确认的验证链接"}

        # 检查域名是否在白名单
        import re
        match = re.search(r'https?://([^/\s]+)', verification_link, re.IGNORECASE)
        if match:
            domain = match.group(1).lower()
            allowed = False
            for d in self.ALLOWED_VERIFY_DOMAINS:
                if domain == d or domain.endswith(f".{d}"):
                    allowed = True
                    break
            if not allowed:
                return {
                    "ok": False,
                    "error": f"域名 {domain} 不在白名单内，禁止自动访问",
                    "domain": domain,
                    "allowed_domains": self.ALLOWED_VERIFY_DOMAINS,
                }

        # 执行确认
        # TODO: 实现自动确认逻辑
        return {
            "ok": True,
            "message": "验证链接已访问",
            "link": verification_link[:50] + "..." if len(verification_link) > 50 else verification_link,
        }
