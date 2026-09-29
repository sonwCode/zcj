from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.mihomo_config import MihomoConfigError, mihomo_config_manager
from core.mihomo_client import MihomoError, MihomoUnavailableError, mihomo_client


router = APIRouter(prefix="/proxy-nodes", tags=["proxy-nodes"])


class ProxySourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=4096)
    interval: int = Field(default=3600, ge=60, le=86400)


class ProxyNodeStateRequest(BaseModel):
    enabled: bool


def _reload_after_save() -> tuple[bool, str]:
    try:
        mihomo_client.reload_config()
        return True, ""
    except MihomoError as exc:
        return False, str(exc)


@router.get("")
def list_proxy_nodes(refresh: bool = False):
    try:
        result = mihomo_client.list_nodes(refresh=refresh)
        provider_by_node: dict[str, str] = {}
        try:
            for provider_name, provider in mihomo_client.list_proxy_providers().items():
                if not isinstance(provider, dict):
                    continue
                for proxy in provider.get("proxies") or []:
                    if isinstance(proxy, dict) and proxy.get("name"):
                        provider_by_node[str(proxy["name"])] = str(provider_name)
        except MihomoError:
            pass
        for node in result.get("nodes", []):
            name = str(node.get("name") or "")
            node["enabled"] = mihomo_client.is_node_enabled(name)
            node["provider"] = provider_by_node.get(name, "")
        return result
    except MihomoError as exc:
        return {
            "available": False,
            "group": mihomo_client.group,
            "selected": "",
            "nodes": [],
            "error": str(exc),
        }


@router.get("/sources")
def list_proxy_sources():
    try:
        sources = mihomo_config_manager.list_sources()
    except MihomoConfigError as exc:
        raise HTTPException(400, str(exc))
    runtime: dict = {}
    runtime_error = ""
    try:
        runtime = mihomo_client.list_proxy_providers()
    except MihomoError as exc:
        runtime_error = str(exc)
    for source in sources:
        info = runtime.get(source["name"]) or {}
        proxies = info.get("proxies") or [] if isinstance(info, dict) else []
        source["node_count"] = len(proxies) if isinstance(proxies, list) else 0
        source["updated_at"] = str(
            (info.get("updatedAt") or info.get("updated_at") or "")
            if isinstance(info, dict)
            else ""
        )
        source["runtime_available"] = bool(info)
    return {
        "configured": mihomo_config_manager.config_path.is_file(),
        "sources": sources,
        "runtime_error": runtime_error,
    }


@router.post("/sources")
def create_proxy_source(body: ProxySourceRequest):
    try:
        source = mihomo_config_manager.create_source(**body.model_dump())
    except MihomoConfigError as exc:
        raise HTTPException(400, str(exc))
    reloaded, reload_error = _reload_after_save()
    return {"ok": True, "source": source, "reloaded": reloaded, "reload_error": reload_error}


@router.put("/sources/{source_name}")
def update_proxy_source(source_name: str, body: ProxySourceRequest):
    try:
        source = mihomo_config_manager.update_source(source_name, **body.model_dump())
    except MihomoConfigError as exc:
        raise HTTPException(400, str(exc))
    reloaded, reload_error = _reload_after_save()
    return {"ok": True, "source": source, "reloaded": reloaded, "reload_error": reload_error}


@router.delete("/sources/{source_name}")
def delete_proxy_source(source_name: str):
    try:
        mihomo_config_manager.delete_source(source_name)
    except MihomoConfigError as exc:
        raise HTTPException(400, str(exc))
    reloaded, reload_error = _reload_after_save()
    return {"ok": True, "reloaded": reloaded, "reload_error": reload_error}


@router.post("/sources/{source_name}/refresh")
def refresh_proxy_source(source_name: str):
    try:
        mihomo_client.refresh_proxy_provider(source_name)
    except MihomoUnavailableError as exc:
        raise HTTPException(503, str(exc))
    except MihomoError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.put("/nodes/{node_name}")
def update_proxy_node(node_name: str, body: ProxyNodeStateRequest):
    try:
        mihomo_client.set_node_enabled(node_name, body.enabled)
    except MihomoUnavailableError as exc:
        raise HTTPException(503, str(exc))
    except MihomoError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "name": node_name, "enabled": body.enabled}


@router.post("/nodes/{node_name}/activate")
def activate_proxy_node(node_name: str):
    try:
        mihomo_client.activate_node(node_name)
    except MihomoUnavailableError as exc:
        raise HTTPException(503, str(exc))
    except MihomoError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "selected": node_name}
