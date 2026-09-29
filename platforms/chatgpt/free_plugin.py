"""独立的 ChatGPT Free 注册入口。

该插件复用已审查的 ChatGPT 邮箱注册核心，但由 ``legacy_register`` 任务
单独驱动手机接码和 Codex AT/RT 完成，不进入 aBai ChatGPT 注册任务链。
"""

from core.registry import register

from .plugin import ChatGPTPlatform


@register
class ChatGPTFreePlatform(ChatGPTPlatform):
    name = "chatgpt_free"
    display_name = "ChatGPT Free"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []
    # Free registration is intentionally isolated from aBai's ChatGPT
    # management actions (CPA, Team Manager, desktop switching, etc.).
    capabilities = []
