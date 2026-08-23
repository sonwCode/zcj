"""oreateai 辅助模块"""
import re
from typing import Optional


def mask_sensitive_data(text: str) -> str:
    """
    打码敏感数据，只显示部分

    用于日志输出，避免泄露完整 token/key/password
    """
    if not text:
        return ""

    result = text

    # 隐藏 token 相关参数
    patterns = [
        (r'(token[id]?=[^&\s"\'<>]+)', r'\1***'),
        (r'(key=[^&\s"\'<>]+)', 'key=***'),
        (r'(code=[^&\s"\'<>]+)', 'code=***'),
        (r'(password=[^&\s"\'<>]+)', 'password=***'),
        (r'(secret=[^&\s"\'<>]+)', 'secret=***'),
        (r'(authorization:\s*)(bearer\s+)[^&\s]+', r'\1\2***'),
        (r'(x-api-key:\s*)[^&\s]+', r'\1***'),
    ]

    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    # 截断过长的文本
    if len(result) > 100:
        result = result[:100] + "..."

    return result


def extract_verification_link(text: str, keyword: str = "oreateai") -> Optional[str]:
    """
    从文本中提取验证链接

    Args:
        text: 邮件正文或其他文本
        keyword: 关键词过滤

    Returns:
        验证链接，未找到返回 None
    """
    # 提取所有 URL
    urls = re.findall(r'https?://[^\s<>"\'\)]+', text, re.IGNORECASE)

    for url in urls:
        url_lower = url.lower()
        # 过滤验证相关链接
        hints = ["verify", "confirm", "activation", "email", "register", "signup"]
        if any(h in url_lower for h in hints):
            # 清理末尾特殊字符
            url = re.sub(r'[^\w\-\./\?=&%:]+$', '', url)
            return url

    # 如果指定了关键词，也匹配包含关键词的链接
    if keyword:
        for url in urls:
            if keyword.lower() in url.lower():
                url = re.sub(r'[^\w\-\./\?=&%:]+$', '', url)
                return url

    return None


def build_verification_link_callback(mailbox, account_email: str, timeout: int = 120):
    """
    构建验证链接回调函数

    复用 OutlookEmailMailbox.wait_for_link

    Args:
        mailbox: OutlookEmailMailbox 实例
        account_email: 邮箱地址
        timeout: 超时时间（秒）

    Returns:
        回调函数，返回验证链接
    """
    from core.base_mailbox import MailboxAccount

    def callback(**kwargs):
        email = kwargs.get("email", account_email)
        wait_timeout = kwargs.get("timeout", timeout)

        # 创建 account 对象
        account = MailboxAccount(email=email)

        # 调用 wait_for_link
        link = mailbox.wait_for_link(
            account,
            keyword="oreateai",  # orateai 邮件关键词
            timeout=wait_timeout,
        )

        # 打码输出
        masked = mask_sensitive_data(link)
        print(f"[Oreateai] 获取到验证链接: {masked}")

        return link

    return callback
