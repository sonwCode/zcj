"""oreateai 浏览器自动化注册"""
import time
import re
from typing import Optional, Callable
from platforms.oreateai.core import OreateaiBrowserHelper


class OreateaiBrowserRegister:
    """
    orateai 浏览器自动化注册

    流程：
    1. 打开注册页面，填写邮箱密码
    2. 通过 OutlookEmail API 获取验证邮件中的链接
    3. 在浏览器中访问验证链接
    4. 注册成功后返回账号信息
    """

    def __init__(
        self,
        headless: bool = True,
        proxy: str = None,
        verification_link_callback: Optional[Callable] = None,
        log_fn: Callable = print,
        # 可选：直接传入已配置的 OutlookEmailMailbox 实例
        mailbox=None,
    ):
        self.headless = headless
        self.proxy = proxy
        self.callback = verification_link_callback
        self.log = log_fn
        self.mailbox = mailbox  # OutlookEmailMailbox 实例

        # Playwright 实例
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def _init_browser(self):
        """初始化浏览器"""
        if self._browser:
            return

        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_opts = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-web-security",
            ]
        }
        if self.proxy:
            launch_opts["proxy"] = {"server": self.proxy}

        self._browser = self._pw.chromium.launch(**launch_opts)
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        self._context.set_default_timeout(60000)
        self._page = self._context.new_page()

    def _find_signup_form_selectors(self) -> dict:
        """自动检测注册表单的选择器"""
        selectors = {
            "email": [
                'input[name="email"]',
                'input[type="email"]',
                'input[id*="email"]',
                'input[placeholder*="email" i]',
                'input[placeholder*="邮箱" i]',
            ],
            "password": [
                'input[name="password"]',
                'input[type="password"]',
                'input[id*="password"]',
            ],
            "submit": [
                'button[type="submit"]',
                'button:has-text("注册")',
                'button:has-text("Sign up")',
                'button:has-text("Sign Up")',
                'input[type="submit"]',
            ],
        }

        result = {}
        for field, selector_list in selectors.items():
            for sel in selector_list:
                try:
                    self._page.wait_for_selector(sel, timeout=2000)
                    result[field] = sel
                    self.log(f"   找到 {field}: {sel}")
                    break
                except:
                    continue
        return result

    def _wait_for_verification_link(self, email: str, timeout: int = 120) -> Optional[str]:
        """通过 OutlookEmailMailbox.wait_for_link 获取验证链接"""
        # 优先使用配置的 mailbox
        if self.mailbox:
            try:
                from core.base_mailbox import MailboxAccount
                account = MailboxAccount(email=email)
                # 尝试从 extra 获取 account_id
                link = self.mailbox.wait_for_link(
                    account,
                    keyword="oreateai",
                    timeout=timeout,
                )
                return link
            except Exception as e:
                self.log(f"   mailbox.wait_for_link 失败: {e}")

        # 回退：使用回调
        if self.callback:
            try:
                return self.callback(email=email, timeout=timeout)
            except Exception as e:
                self.log(f"   callback 获取链接失败: {e}")

        return None

    def run(self, email: str, password: str) -> dict:
        """
        执行完整的浏览器自动化注册

        Args:
            email: 注册邮箱
            password: 注册密码

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
            # 初始化浏览器
            self._init_browser()
            page = self._page

            # ============================================
            # 步骤 1: 打开注册页面
            # ============================================
            self.log("1. 打开注册页面...")
            signup_url = "https://www.oreateai.com/auth/signup"
            page.goto(signup_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)
            self.log(f"   当前页面: {page.url}")

            # ============================================
            # 步骤 2: 填写注册表单
            # ============================================
            self.log(f"2. 填写注册表单: {email}")
            selectors = self._find_signup_form_selectors()

            if not selectors.get("email"):
                raise RuntimeError("未找到邮箱输入框")

            # 填写邮箱
            page.fill(selectors["email"], email)
            self.log("   邮箱已填写")

            # 填写密码
            if selectors.get("password"):
                page.fill(selectors["password"], password)
                self.log("   密码已填写")

            # 点击注册按钮
            if selectors.get("submit"):
                page.click(selectors["submit"])
                self.log("   已点击注册按钮")
            else:
                raise RuntimeError("未找到注册按钮")

            time.sleep(2)
            self.log(f"   当前页面: {page.url}")

            # ============================================
            # 步骤 3: 通过 OutlookEmail API 获取验证链接
            # ============================================
            self.log("3. 等待验证邮件...")

            verify_link = self._wait_for_verification_link(email, timeout=120)

            if not verify_link:
                result["error"] = "未获取到验证链接"
                return result

            # 打码显示（隐藏敏感部分）
            masked_link = self._mask_link(verify_link)
            self.log(f"   验证链接: {masked_link}")

            # ============================================
            # 步骤 4: 访问验证链接
            # ============================================
            self.log("4. 访问验证链接...")
            page.goto(verify_link, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)
            self.log(f"   当前页面: {page.url}")

            time.sleep(3)

            # ============================================
            # 步骤 5: 获取 session 和积分
            # ============================================
            self.log("5. 获取账号信息...")

            # 获取 cookies
            cookies = self._context.cookies()
            for c in cookies:
                if c["name"] in ["session_token", "token", "auth_token", "session"]:
                    result["session_token"] = c["value"]
                    break

            # 尝试从页面获取积分
            self._extract_credits_from_page(page, result)

            # 如果页面没找到，尝试 API
            if result["credits"] == 0:
                self._query_credits_via_api(page, result)

            # 默认 80 积分
            if result["credits"] == 0:
                result["credits"] = 80

            result["success"] = True
            self.log(f"✓ 注册成功! 积分: {result['credits']}")

        except Exception as e:
            result["error"] = str(e)
            self.log(f"✗ 注册失败: {e}")

        return result

    def _mask_link(self, link: str) -> str:
        """打码显示链接，隐藏 token 部分"""
        if not link:
            return ""
        # 隐藏 token 参数值
        masked = re.sub(r'(token[id]?=[^&\s]+)', r'\1***', link, flags=re.IGNORECASE)
        masked = re.sub(r'(key=[^&\s]+)', 'key=***', masked, flags=re.IGNORECASE)
        masked = re.sub(r'(code=[^&\s]+)', 'code=***', masked, flags=re.IGNORECASE)
        # 截断过长的链接
        if len(masked) > 80:
            masked = masked[:80] + "..."
        return masked

    def _extract_credits_from_page(self, page, result: dict):
        """从页面提取积分信息"""
        try:
            credit_selectors = [
                '[class*="credit"]',
                '[class*="point"]',
                '[class*="balance"]',
                '[class*="score"]',
            ]
            for sel in credit_selectors:
                try:
                    element = page.wait_for_selector(sel, timeout=3000)
                    if element:
                        credit_text = element.text_content()
                        credit_match = re.search(r'(\d+)', credit_text or "")
                        if credit_match:
                            result["credits"] = int(credit_match.group(1))
                            break
                except:
                    continue
        except:
            pass

    def _query_credits_via_api(self, page, result: dict):
        """通过 API 查询积分"""
        try:
            resp = page.request.get(
                "https://www.oreateai.com/api/user/profile",
                headers={"Authorization": f"Bearer {result['session_token']}"} if result["session_token"] else {}
            )
            if resp.ok:
                data = resp.json()
                result["credits"] = data.get("credits", data.get("balance", 80))
                result["api_token"] = data.get("api_key", "")
        except:
            pass

    def _close_browser(self):
        """关闭浏览器"""
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._browser = None
        self._pw = None

    def __del__(self):
        self._close_browser()
