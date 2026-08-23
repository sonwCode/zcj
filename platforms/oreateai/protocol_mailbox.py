"""oreateai 协议邮箱注册 worker"""
from __future__ import annotations

from typing import Callable, Optional
from platforms.oreateai.core import OreateaiBrowserHelper, mask_link


class OreateaiProtocolMailboxWorker:
    """
    orateai 协议注册 Worker

    使用 HTTP 请求 + OutlookEmailMailbox 获取验证链接完成注册

    核心边界：
    - ✅ 自动收信、自动提取验证链接
    - ⚠️ 只在 allowlist 域名内自动访问验证链接
    - ❌ 不做批量注册、不绕过平台风控
    """

    # 允许自动访问验证链接的域名白名单
    ALLOWED_VERIFY_DOMAINS = [
        "oreateai.com",
        "www.oreateai.com",
    ]

    def __init__(
        self,
        *,
        executor,
        log_fn: Callable[[str], None] = print,
        # 可选：传入 OutlookEmailMailbox 实例
        mailbox=None,
    ):
        self.executor = executor
        self.log = log_fn
        self.mailbox = mailbox  # OutlookEmailMailbox 实例

        # 创建辅助类
        self._helper = OreateaiBrowserHelper(log_fn=log_fn)

    def _get_verification_link_from_mailbox(self, email: str, timeout: int = 120) -> Optional[str]:
        """
        通过 OutlookEmailMailbox.wait_for_link 获取验证链接

        复用的是：
        - GET /api/external/emails
        - _extract_verification_link
        """
        if not self.mailbox:
            return None

        try:
            from core.base_mailbox import MailboxAccount

            # 创建 account 对象
            account = MailboxAccount(email=email)

            # 调用 wait_for_link
            link = self.mailbox.wait_for_link(
                account,
                keyword="oreateai",
                timeout=timeout,
            )

            # 打码输出（安全日志）
            masked = mask_link(link)
            self.log(f"   获取到验证链接: {masked}")

            return link

        except TimeoutError:
            self.log(f"   等待验证链接超时 ({timeout}s)")
            return None
        except Exception as e:
            self.log(f"   获取验证链接失败: {e}")
            return None

    def _is_verify_link_allowed(self, link: str) -> bool:
        """
        检查验证链接是否在白名单内

        只有白名单域名才自动访问，非白名单需要人工确认
        """
        import re

        if not link:
            return False

        # 提取域名
        match = re.search(r'https?://([^/\s]+)', link, re.IGNORECASE)
        if not match:
            return False

        domain = match.group(1).lower()

        # 检查是否在白名单
        for allowed in self.ALLOWED_VERIFY_DOMAINS:
            if domain == allowed or domain.endswith(f".{allowed}"):
                return True

        return False

    def run(
        self,
        *,
        email: str,
        password: str,
        link_callback: Optional[Callable[[], str]] = None,
        auto_confirm: bool = True,
    ) -> dict:
        """
        执行注册流程

        Args:
            email: 注册邮箱
            password: 注册密码
            link_callback: 可选的链接回调
            auto_confirm: 是否自动确认链接（仅限白名单域名）

        Returns:
            dict: 注册结果
        """
        result = {
            "email": email,
            "password": password,
            "success": False,
            "credits": 0,
            "api_token": "",
            "session_token": "",
            "error": "",
        }

        try:
            # 1. 访问注册页面
            page_data = self._helper.step1_get_signup_page()
            csrf = page_data.get("csrf", "")

            # 2. 提交注册
            signup_data = self._helper.step2_submit_signup(email, password, csrf)
            if signup_data.get("status_code", 0) >= 400:
                result["error"] = f"注册失败: {signup_data.get('response')}"
                return result

            # 3. 获取验证链接
            verify_link = None

            # 优先使用回调
            if link_callback:
                try:
                    verify_link = link_callback()
                except Exception as e:
                    self.log(f"   回调获取失败: {e}")

            # 回退：使用 mailbox
            if not verify_link and self.mailbox:
                verify_link = self._get_verification_link_from_mailbox(email)

            if not verify_link:
                result["error"] = "未获取到验证链接"
                return result

            # 4. 安全检查：确认链接
            masked = mask_link(verify_link)

            if self._is_verify_link_allowed(verify_link):
                if auto_confirm:
                    self.log(f"   白名单域名，自动确认: {masked}")
                else:
                    self.log(f"   白名单域名，待确认: {masked}")
                    result["verification_link"] = verify_link
                    result["pending_confirmation"] = True
                    return result
            else:
                # 非白名单，需要人工确认
                self.log(f"   ⚠️ 非白名单域名，需人工确认: {masked}")
                result["verification_link"] = verify_link
                result["pending_confirmation"] = True
                result["warning"] = "验证链接不在白名单内，请人工确认"
                return result

            # 5. 确认邮箱
            confirm_data = self._helper.step4_confirm_email(verify_link)

            # 6. 获取账号信息
            account_data = self._helper.step5_get_account_info()
            result["credits"] = account_data.get("credits", 80)
            result["api_token"] = account_data.get("api_token", "")
            result["session_token"] = account_data.get("session_token", "")

            result["success"] = True
            self.log(f"✓ 注册成功! 积分: {result['credits']}")

        except Exception as e:
            result["error"] = str(e)
            self.log(f"✗ 注册失败: {e}")

        return result
