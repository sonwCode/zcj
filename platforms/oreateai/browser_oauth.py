"""oreateai 浏览器 OAuth 登录"""
from typing import Optional, Callable


class OreateaiBrowserOAuth:
    """oreateai OAuth 登录（预留）"""

    def __init__(
        self,
        oauth_provider: str = "google",
        headless: bool = True,
        proxy: str = None,
        log_fn: Callable = print,
    ):
        self.oauth_provider = oauth_provider
        self.headless = headless
        self.proxy = proxy
        self.log = log_fn

    def run(self, email_hint: str = "") -> dict:
        """
        执行 OAuth 登录流程

        TODO: 如果 orateai 支持 Google/GitHub 登录，实现此方法
        """
        self.log(f"OAuth 登录 ({self.oauth_provider}) - 待实现")
        return {
            "success": False,
            "error": "OAuth 登录待实现",
        }


def register_with_browser_oauth(
    oauth_provider: str = "google",
    email_hint: str = "",
    proxy: str = None,
    timeout: int = 300,
    log_fn: Callable = print,
    **kwargs
) -> dict:
    """浏览器 OAuth 注册入口"""
    oauth = OreateaiBrowserOAuth(
        oauth_provider=oauth_provider,
        proxy=proxy,
        log_fn=log_fn,
    )
    return oauth.run(email_hint=email_hint)
