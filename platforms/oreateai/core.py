"""oreateai 浏览器注册 - 简化版，使用 HTTP API"""
import time
import re
from typing import Optional, Callable, Dict, Any


class OreateaiBrowserHelper:
    """
    orateai 注册辅助类

    使用 HTTP 请求完成注册流程，配合 OutlookEmailMailbox 获取验证链接
    """

    def __init__(
        self,
        log_fn: Callable = print,
        verification_link_callback: Optional[Callable] = None,
    ):
        self.log = log_fn
        self.callback = verification_link_callback
        self.session = None

    def _get_session(self):
        """获取 HTTP 会话"""
        if self.session is None:
            from curl_cffi import requests as cffi_requests
            self.session = cffi_requests.Session()
            self.session.impersonate = "chrome124"
            self.session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
        return self.session

    def _mask_link(self, link: str) -> str:
        """打码显示链接"""
        if not link:
            return ""
        masked = re.sub(r'(token[id]?=[^&\s]+)', r'\1***', link, flags=re.IGNORECASE)
        masked = re.sub(r'(key=[^&\s]+)', 'key=***', masked, flags=re.IGNORECASE)
        if len(masked) > 80:
            masked = masked[:80] + "..."
        return masked

    def step1_get_signup_page(self) -> Dict[str, Any]:
        """访问注册页面，获取初始 cookies 和可能的 CSRF"""
        self.log("1. 访问注册页面...")
        s = self._get_session()
        base_url = "https://www.oreateai.com"

        r = s.get(f"{base_url}/auth/signup", timeout=15)

        csrf = ""
        # 尝试从 HTML 提取 CSRF
        match = re.search(r'name=["\']csrf_token["\'].*?value=["\']([^"\']+)["\']', r.text, re.IGNORECASE)
        if match:
            csrf = match.group(1)

        self.log(f"   页面状态: {r.status_code}")
        self.log(f"   CSRF: {csrf[:20] if csrf else 'N/A'}...")

        return {"csrf": csrf, "cookies": dict(s.cookies)}

    def step2_submit_signup(self, email: str, password: str, csrf: str = "") -> Dict[str, Any]:
        """提交注册表单"""
        self.log(f"2. 提交注册: {email}")
        s = self._get_session()
        base_url = "https://www.oreateai.com"

        # TODO: 抓包确认实际请求格式
        # 尝试常见的注册 API
        data = {"email": email, "password": password}
        if csrf:
            data["csrf_token"] = csrf

        # 尝试 JSON 格式
        r = s.post(
            f"{base_url}/api/auth/register",
            json=data,
            timeout=15
        )

        self.log(f"   API 响应: {r.status_code}")

        if r.status_code >= 400:
            # 尝试表单格式
            r = s.post(
                f"{base_url}/api/auth/register",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15
            )
            self.log(f"   表单格式响应: {r.status_code}")

        return {
            "status_code": r.status_code,
            "response": r.text[:500],
        }

    def step3_wait_for_verification_link(self, email: str, timeout: int = 120) -> Optional[str]:
        """等待并获取验证链接"""
        self.log("3. 等待验证邮件...")

        # 使用回调获取链接
        if self.callback:
            try:
                link = self.callback(email=email, timeout=timeout)
                if link:
                    self.log(f"   链接: {self._mask_link(link)}")
                    return link
            except Exception as e:
                self.log(f"   回调获取失败: {e}")

        # 如果没有回调，抛出异常
        raise TimeoutError("未配置验证链接回调，请传入 verification_link_callback")

    def step4_confirm_email(self, verify_link: str) -> Dict[str, Any]:
        """访问验证链接"""
        self.log(f"4. 确认邮箱: {self._mask_link(verify_link)}")
        s = self._get_session()

        r = s.get(verify_link, timeout=15)
        self.log(f"   响应: {r.status_code}")

        return {
            "status_code": r.status_code,
            "url": r.url,
        }

    def step5_get_account_info(self) -> Dict[str, Any]:
        """获取账号信息"""
        self.log("5. 获取账号信息...")
        s = self._get_session()
        base_url = "https://www.oreateai.com"

        result = {"credits": 80, "api_token": "", "session_token": ""}

        # 获取 cookies
        cookies = dict(s.cookies)
        for name in ["session_token", "token", "auth_token", "session"]:
            if name in cookies:
                result["session_token"] = cookies[name]
                break

        # 尝试 API 获取信息
        try:
            r = s.get(
                f"{base_url}/api/user/profile",
                headers={"Authorization": f"Bearer {result['session_token']}"} if result["session_token"] else {},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                result["credits"] = data.get("credits", data.get("balance", 80))
                result["api_token"] = data.get("api_token", "")
        except:
            pass

        return result

    def run(self, email: str, password: str) -> Dict[str, Any]:
        """
        执行注册流程

        Args:
            email: 邮箱
            password: 密码

        Returns:
            dict: {
                email, password, success,
                credits, api_token, session_token,
                error
            }
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
            page_data = self.step1_get_signup_page()
            csrf = page_data.get("csrf", "")

            # 2. 提交注册
            signup_data = self.step2_submit_signup(email, password, csrf)
            if signup_data.get("status_code", 0) >= 400:
                result["error"] = f"注册失败: {signup_data.get('response')}"
                return result

            # 3. 等待验证链接
            verify_link = self.step3_wait_for_verification_link(email)

            if not verify_link:
                result["error"] = "未获取到验证链接"
                return result

            # 4. 确认邮箱
            confirm_data = self.step4_confirm_email(verify_link)

            # 5. 获取账号信息
            account_data = self.step5_get_account_info()
            result["credits"] = account_data.get("credits", 80)
            result["api_token"] = account_data.get("api_token", "")
            result["session_token"] = account_data.get("session_token", "")

            result["success"] = True
            self.log(f"✓ 注册成功! 积分: {result['credits']}")

        except Exception as e:
            result["error"] = str(e)
            self.log(f"✗ 注册失败: {e}")

        return result


def mask_link(link: str) -> str:
    """打码显示链接"""
    if not link:
        return ""
    masked = re.sub(r'(token[id]?=[^&\s]+)', r'\1***', link, flags=re.IGNORECASE)
    masked = re.sub(r'(key=[^&\s]+)', 'key=***', masked, flags=re.IGNORECASE)
    if len(masked) > 80:
        masked = masked[:80] + "..."
    return masked
