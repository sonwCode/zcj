"""平台插件注册表 - 自动扫描 platforms/ 目录加载插件"""
import importlib
import pkgutil
from datetime import datetime, timezone
from typing import Dict, Type
from sqlmodel import Session, select
from .base_platform import BasePlatform
from .db import PlatformCapabilityOverrideModel, engine

_registry: Dict[str, Type[BasePlatform]] = {}

_CAPABILITY_KEYS = ("supported_executors", "supported_identity_modes", "supported_oauth_providers", "capabilities")

# 线上工作台把"注册能力"作为一个独立入口暴露；这份清单决定了哪些平台会出现在
# 该入口里，以及它们各自走什么验证方式。chatgpt_free 不是目录，它由
# platforms/chatgpt/free_plugin.py 注册，所以不参与下面的目录扫描。
LEGACY_REGISTRATION_PLATFORMS = (
    "chatgpt_free",
    "anything",
    "blink",
    "cerebras",
    "cursor",
    "grok",
    "kiro",
    "openblocklabs",
    "oreateai",
    "tavily",
    "trae",
    "windsurf",
)

LEGACY_REGISTRATION_METADATA = {
    "chatgpt_free": {
        "description": "ChatGPT Free 邮箱注册 + 手机接码，并完成 Codex AT/RT。",
        "verification": "邮箱验证码 + 手机短信验证码 + Codex OAuth",
    },
    "anything": {"description": "邮箱魔法链接注册，纯协议执行。", "verification": "魔法链接"},
    "blink": {"description": "邮箱魔法链接注册，并初始化工作区。", "verification": "魔法链接"},
    "cerebras": {"description": "邮箱验证码注册，成功后生成并保存 API Key。", "verification": "邮箱验证码"},
    "cursor": {"description": "邮箱验证码注册，支持协议和浏览器执行。", "verification": "邮箱验证码 + 人机验证"},
    "grok": {"description": "支持邮箱注册或 Google OAuth 可视浏览器注册。", "verification": "邮箱验证码 / Google OAuth"},
    "kiro": {"description": "邮箱验证码注册，支持协议和浏览器执行。", "verification": "邮箱验证码"},
    "openblocklabs": {"description": "支持邮箱注册或 Google OAuth 可视浏览器注册。", "verification": "邮箱验证码 / Google OAuth"},
    "oreateai": {"description": "邮箱验证码注册，支持协议和浏览器执行。", "verification": "邮箱验证码"},
    "tavily": {"description": "支持邮箱注册及多种 OAuth；OAuth 需要可复用 Chrome 会话。", "verification": "邮箱验证码 / OAuth"},
    "trae": {"description": "支持邮箱注册或 Google OAuth 可视浏览器注册。", "verification": "邮箱验证码 / Google OAuth"},
    "windsurf": {"description": "邮箱验证码注册，支持协议和浏览器执行。", "verification": "邮箱验证码 + 人机验证"},
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def register(cls: Type[BasePlatform]):
    """装饰器：注册平台插件"""
    _registry[cls.name] = cls
    return cls


def load_all():
    """自动扫描并加载 platforms/ 下所有插件"""
    import platforms
    # chatgpt_free 的平台类挂在 free_plugin 里而不是 plugin 里，扫描不到。
    try:
        importlib.import_module("platforms.chatgpt.free_plugin")
    except ModuleNotFoundError:
        pass
    for finder, name, _ in pkgutil.iter_modules(platforms.__path__, platforms.__name__ + "."):
        try:
            importlib.import_module(f"{name}.plugin")
        except ModuleNotFoundError:
            pass


def get(name: str) -> Type[BasePlatform]:
    if name not in _registry:
        raise KeyError(f"平台 '{name}' 未注册，已注册: {list(_registry.keys())}")
    return _registry[name]


def _class_defaults(cls: Type[BasePlatform]) -> dict[str, list[str]]:
    """从类属性获取 fallback 默认值（仅在 DB 无数据时使用）。"""
    return {
        "supported_executors": list(getattr(cls, "supported_executors", [])),
        "supported_identity_modes": list(getattr(cls, "supported_identity_modes", [])),
        "supported_oauth_providers": list(getattr(cls, "supported_oauth_providers", [])),
        "capabilities": list(getattr(cls, "capabilities", [])),
    }


def _normalize_platform_capabilities(data: dict | None, cls: Type[BasePlatform]) -> dict[str, list[str]]:
    defaults = _class_defaults(cls)
    payload = data if isinstance(data, dict) else {}
    normalized: dict[str, list[str]] = {}
    for key in _CAPABILITY_KEYS:
        raw = payload.get(key)
        if isinstance(raw, list):
            normalized[key] = [str(item) for item in raw if str(item or "").strip()]
        else:
            normalized[key] = list(defaults.get(key, []))
    return normalized


def _ensure_platform_capabilities_seeded(session: Session) -> dict[str, PlatformCapabilityOverrideModel]:
    items = session.exec(select(PlatformCapabilityOverrideModel)).all()
    by_name = {item.platform_name: item for item in items}
    changed = False
    for cls in _registry.values():
        if cls.name in by_name:
            item = by_name[cls.name]
            current = _normalize_platform_capabilities(item.get_capabilities(), cls)
            defaults = _class_defaults(cls)
            merged = {key: list(current.get(key, [])) for key in _CAPABILITY_KEYS}
            did_merge = False
            for key in _CAPABILITY_KEYS:
                for value in defaults.get(key, []):
                    if value not in merged[key]:
                        merged[key].append(value)
                        did_merge = True
            if did_merge:
                item.set_capabilities(merged)
                item.updated_at = _utcnow()
                session.add(item)
                changed = True
            continue
        item = PlatformCapabilityOverrideModel(platform_name=cls.name)
        item.created_at = _utcnow()
        item.updated_at = _utcnow()
        item.set_capabilities(_class_defaults(cls))
        session.add(item)
        by_name[cls.name] = item
        changed = True
    if changed:
        session.commit()
        items = session.exec(select(PlatformCapabilityOverrideModel)).all()
        by_name = {item.platform_name: item for item in items}
    return by_name


def get_platform_capabilities(name: str) -> dict[str, list[str]]:
    cls = get(name)
    with Session(engine) as session:
        by_name = _ensure_platform_capabilities_seeded(session)
        item = by_name.get(name)
        if item:
            return _normalize_platform_capabilities(item.get_capabilities(), cls)
    return _class_defaults(cls)


def list_platforms() -> list:
    with Session(engine) as session:
        persisted = _ensure_platform_capabilities_seeded(session)
        result = []
        for cls in _registry.values():
            item = persisted.get(cls.name)
            caps = _normalize_platform_capabilities(item.get_capabilities() if item else None, cls)
            result.append({
                "name": cls.name,
                "display_name": cls.display_name,
                "version": cls.version,
                **caps,
            })
        return result


def list_legacy_registration_platforms() -> list:
    """注册工作台入口的平台清单：只列白名单内的，并附上验证方式说明。"""
    allowed = set(LEGACY_REGISTRATION_PLATFORMS)
    result = []
    for item in list_platforms():
        if item["name"] not in allowed:
            continue
        result.append({**item, **LEGACY_REGISTRATION_METADATA.get(item["name"], {})})
    return result
