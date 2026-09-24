"""OpenAI 专用 HTTP 客户端"""
from typing import Any, Dict, Optional, Tuple

from core.http_client import HTTPClient, HTTPClientError, RequestConfig
from .constants import ERROR_MESSAGES
import logging
logger = logging.getLogger(__name__)

# 浏览器画像统一由 core.identity_profile 派生（impersonate 目标决定 OS，UA 与
# client hints 从同一目标派生）。这里原本硬编码了 Windows UA + chrome142 的
# impersonate，而 curl_cffi 的 chrome142 目标实际是 macOS Tahoe —— TLS/HTTP2
# 指纹说 macOS、请求头说 Windows，已移除以免被再次误用。

class OpenAIHTTPClient(HTTPClient):
    """
    OpenAI 专用 HTTP 客户端
    包含 OpenAI API 特定的请求方法
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        config: Optional[RequestConfig] = None,
        profile: Optional[Any] = None,
    ):
        """
        初始化 OpenAI HTTP 客户端

        Args:
            proxy_url: 代理 URL
            config: 请求配置
        """
        # 画像必须先于 config 解析：impersonate 目标决定了 OS，而 UA 与 client hints
        # 都从同一个目标派生，二者因此不可能互相矛盾。
        if profile is None:
            from core.identity_profile import resolve_profile

            profile = resolve_profile()
        self.browser_profile = profile
        if config is None:
            config = RequestConfig(impersonate=profile.impersonate)
        super().__init__(proxy_url, config)

        # 请求头统一从地理一致的浏览器画像派生（core.identity_profile），
        # 避免「代理出口在 JP、指纹却说 UTC/en-US」这种自相矛盾。
        self.default_headers = profile.headers()

    def get_chatgpt_headers(self, referer: str = "https://chatgpt.com/login") -> Dict[str, str]:
        """Headers for the chatgpt.com NextAuth API boundary."""
        profile = getattr(self, "browser_profile", None)
        if profile is None:
            from core.identity_profile import resolve_profile

            profile = resolve_profile()
        hints = profile.headers()
        return {
            "User-Agent": hints["User-Agent"],
            "sec-ch-ua": hints["sec-ch-ua"],
            "sec-ch-ua-full-version-list": hints["sec-ch-ua-full-version-list"],
            "sec-ch-ua-platform": hints["sec-ch-ua-platform"],
            "sec-ch-ua-platform-version": hints["sec-ch-ua-platform-version"],
            "sec-ch-ua-arch": hints["sec-ch-ua-arch"],
            "sec-ch-ua-bitness": hints["sec-ch-ua-bitness"],
            "sec-ch-ua-model": hints["sec-ch-ua-model"],
            "sec-ch-ua-mobile": hints["sec-ch-ua-mobile"],
            "accept": "*/*",
            "accept-language": profile.accept_language,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
        }

    def check_nextauth_access(self) -> tuple[bool, int]:
        """Confirm that the route can reach ChatGPT's NextAuth API."""
        try:
            response = self.session.get(
                "https://chatgpt.com/api/auth/providers",
                headers=self.get_chatgpt_headers(),
                timeout=15,
            )
            return response.status_code == 200, int(response.status_code)
        except Exception:
            return False, 0

    def check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """
        检查 IP 地理位置

        Returns:
            Tuple[是否支持, 位置信息]
        """
        try:
            response = self.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            trace_text = response.text

            # 解析位置信息
            import re
            loc_match = re.search(r"loc=([A-Z]+)", trace_text)
            loc = loc_match.group(1) if loc_match else None

            # 检查是否支持
            if loc in ["CN", "HK", "MO", "TW"]:
                return False, loc
            return True, loc

        except Exception as e:
            logger.error(f"检查 IP 地理位置失败: {e}")
            return False, None

    def send_openai_request(
        self,
        endpoint: str,
        method: str = "POST",
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        发送 OpenAI API 请求

        Args:
            endpoint: API 端点
            method: HTTP 方法
            data: 表单数据
            json_data: JSON 数据
            headers: 请求头
            **kwargs: 其他参数

        Returns:
            响应 JSON 数据

        Raises:
            HTTPClientError: 请求失败
        """
        # 合并请求头
        request_headers = self.default_headers.copy()
        if headers:
            request_headers.update(headers)

        # 设置 Content-Type
        if json_data is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        elif data is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            response = self.request(
                method,
                endpoint,
                data=data,
                json=json_data,
                headers=request_headers,
                **kwargs
            )

            # 检查响应状态码
            response.raise_for_status()

            # 尝试解析 JSON
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}

        except cffi_requests.RequestsError as e:
            raise HTTPClientError(f"OpenAI 请求失败: {endpoint} - {e}")

    def check_sentinel(self, did: str, proxies: Optional[Dict] = None) -> Optional[str]:
        """
        检查 Sentinel 拦截

        Args:
            did: Device ID
            proxies: 代理配置

        Returns:
            Sentinel token 或 None
        """
        from .constants import OPENAI_API_ENDPOINTS

        try:
            sen_req_body = f'{{"p":"","id":"{did}","flow":"authorize_continue"}}'

            response = self.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sen_req_body,
            )

            if response.status_code == 200:
                return response.json().get("token")
            else:
                logger.warning(f"Sentinel 检查失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Sentinel 检查异常: {e}")
            return None


def create_http_client(
    proxy_url: Optional[str] = None,
    config: Optional[RequestConfig] = None
) -> HTTPClient:
    """
    创建 HTTP 客户端工厂函数

    Args:
        proxy_url: 代理 URL
        config: 请求配置

    Returns:
        HTTPClient 实例
    """
    return HTTPClient(proxy_url, config)


def create_openai_client(
    proxy_url: Optional[str] = None,
    config: Optional[RequestConfig] = None,
    profile: Optional[Any] = None,
) -> OpenAIHTTPClient:
    """
    创建 OpenAI HTTP 客户端工厂函数

    Args:
        proxy_url: 代理 URL
        config: 请求配置

    Returns:
        OpenAIHTTPClient 实例
    """
    return OpenAIHTTPClient(proxy_url, config, profile)
