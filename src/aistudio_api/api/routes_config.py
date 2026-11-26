"""系统配置、API Key 管理与全局代理路由（自 routes_system.py / routes_operations.py 拆分）。"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping, Sequence

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.model_defaults import (
    _resolve_config_path,
    config_file_write_lock,
    invalidate_config_cache,
    update_config_yaml_document,
)
from aistudio_api.infrastructure.utils.common import atomic_write_text
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.redaction import redact_text

logger = get_logger("routes.config")

public_router = APIRouter()
protected_router = APIRouter()

# config.yaml 的所有读-改-写路径（整文件保存、API Key 增删改备注、全局代理）
# 共用 model_defaults.config_file_write_lock：锁定义与那 5 个配置写函数同源，
# 否则跨模块并发（面板保存 YAML 同时切换 WAA 节点）仍会互相覆盖。


def _mask_yaml_api_keys(yaml_content: str) -> str:
    """结构层脱敏：api_keys 的 Key 明文与 waa.servers 的自定义 headers。

    文本级掩码（逐行匹配 ``key:``）无法覆盖 flow/JSON 风格与纯字符串
    列表等合法 YAML 形态（2026-11 安全审查实测全部漏脱敏），此处
    safe_load 解析后按结构掩码再序列化；解析失败时整体隐藏，绝不
    回传原文。代价是注释丢失——仅对非管理密码会话调用，管理员仍
    拿到原文。远程签名器的 Authorization 共享密钥一并掩码。
    """
    from aistudio_api.api.dependencies import mask_api_key

    def _mask_item(item: object) -> object:
        if isinstance(item, str):
            return mask_api_key(item) if item else item
        if isinstance(item, dict):
            out = dict(item)
            key = out.get("key")
            if isinstance(key, str) and key:
                out["key"] = mask_api_key(key)
            return out
        return item

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return "(配置解析失败，已隐藏)"
    if parsed is None:
        return yaml_content
    if not isinstance(parsed, dict):
        return "(不支持的配置结构，已隐藏)"

    raw_keys = parsed.get("api_keys")
    if isinstance(raw_keys, list):
        parsed["api_keys"] = [_mask_item(item) for item in raw_keys]
    elif isinstance(raw_keys, dict):
        parsed["api_keys"] = _mask_item(raw_keys)

    waa = parsed.get("waa")
    if isinstance(waa, dict):
        servers = waa.get("servers")
        if isinstance(servers, list):
            for server in servers:
                if not isinstance(server, dict):
                    continue
                headers = server.get("headers")
                if isinstance(headers, dict) and headers:
                    server["headers"] = dict.fromkeys(headers, "***")
                if server.get("auth_token_env"):
                    server["auth_token_env"] = "***"
    # 全局出站代理可能内嵌 user:pass 凭据（update_global_proxy 按 0600
    # 私密落盘）：非管理密码会话读 /config 时必须同等级掩码，否则任意
    # API Key 持有者可读出代理账密。
    network = parsed.get("network")
    if isinstance(network, dict):
        proxy_url = network.get("proxy_url")
        if isinstance(proxy_url, str) and proxy_url.strip():
            network["proxy_url"] = redact_text(proxy_url)
    try:
        return yaml.dump(
            parsed, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
    except Exception:
        return "(配置脱敏失败，已隐藏)"


class ConfigYamlUpdateRequest(BaseModel):
    yaml_content: str


@protected_router.get("/config")
async def get_system_config(request: Request) -> dict[str, object]:
    """获取系统运行配置与 config.yaml 内容。

    脱敏（2.1）：未配置管理密码时 yaml_content 中的 API Key 明文会被
    掩码，避免任意 Key 持有者读到全部 Key。
    """
    from aistudio_api.api.dependencies import (
        is_admin_password_session,
        web_auth_required,
    )
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_local_waa_server,
    )
    from aistudio_api.infrastructure.utils.logger import (
        get_dump_dir,
        get_log_level,
        is_debug_env_active,
        is_dump_requests_enabled,
        is_dump_to_file_enabled,
    )
    from aistudio_api.infrastructure.utils.upstream_dump import (
        upstream_snapshot_keep,
    )

    config_yaml_path = _resolve_config_path(None)
    yaml_content = ""
    if config_yaml_path.exists():
        with contextlib.suppress(Exception):
            yaml_content = config_yaml_path.read_text(encoding="utf-8")
    if not is_admin_password_session(request):
        yaml_content = _mask_yaml_api_keys(yaml_content)

    active_waa = get_local_waa_server()
    return {
        "port": settings.port,
        "proof_server_url": str(active_waa.get("url") or ""),
        "active_signer_server": active_waa.get("id"),
        "proxy_configured": bool(settings.proxy_url),
        "auth_enabled": web_auth_required(),
        "yaml_content": yaml_content,
        "log_level": get_log_level(),
        "dump_requests": is_dump_requests_enabled(),
        "debug_env_active": is_debug_env_active(),
        "dump_to_file": is_dump_to_file_enabled(),
        "dump_dir": str(get_dump_dir()),
        "upstream_snapshot_keep": upstream_snapshot_keep(),
    }


@protected_router.put("/config/yaml")
async def update_config_yaml(req: ConfigYamlUpdateRequest) -> dict[str, object]:
    """更新 config.yaml 文件内容并热重载默认配置。"""

    try:
        parsed = yaml.safe_load(req.yaml_content)
        if parsed is not None and not isinstance(parsed, dict):
            raise HTTPException(400, detail="YAML 顶层必须为字典结构")
    except yaml.YAMLError as e:
        raise HTTPException(400, detail=f"YAML 语法格式错误: {e}") from e
    config_yaml_path = _resolve_config_path(None)
    try:
        # 原子替换：直接 write_text 时进程被中断会留下半截 YAML，下次启动即解析失败。
        # 注意：此处为「用户原文整写」语义（PUT /config/yaml 是面板编辑器的保存框，
        # 必须逐字保留用户提交的原文含注释），不走 update_config_yaml_document 的
        # 解析-再序列化路径，否则注释与格式会被 yaml.dump 重排抹掉。
        # 写盘（fsync + rename）是同步 I/O：与 API Key 端点同款下放线程池，
        # 避免保存期间拖停在途模型请求/SSE 流（写锁为 threading.Lock，
        # 跨线程语义不变）。
        def _persist_yaml() -> None:
            with config_file_write_lock():
                atomic_write_text(config_yaml_path, req.yaml_content)
                invalidate_config_cache()

        await asyncio.to_thread(_persist_yaml)

        if isinstance(parsed, dict) and "logging" in parsed:
            raw_logging = parsed["logging"]
            if isinstance(raw_logging, dict):
                from aistudio_api.infrastructure.utils.logger import (
                    set_dump_dir,
                    set_dump_requests,
                    set_dump_to_file,
                    set_log_level,
                )

                if "level" in raw_logging:
                    set_log_level(str(raw_logging["level"]))
                if "dump_requests" in raw_logging:
                    set_dump_requests(bool(raw_logging["dump_requests"]))
                if "dump_to_file" in raw_logging:
                    set_dump_to_file(bool(raw_logging["dump_to_file"]))
                if "dump_dir" in raw_logging:
                    set_dump_dir(str(raw_logging["dump_dir"]))
        logger.info("已更新并重载 config.yaml")
        return {"ok": True, "message": "配置已保存并重载"}
    except Exception as e:
        raise HTTPException(500, detail=f"写入配置文件失败: {e}") from e


class LoggingConfigRequest(BaseModel):
    level: str | None = None
    dump_requests: bool | None = None
    dump_to_file: bool | None = None
    dump_dir: str | None = None
    upstream_snapshot_keep: int | None = None


@protected_router.put("/config/logging")
async def update_logging_config(req: LoggingConfigRequest) -> dict[str, object]:
    """更新系统日志级别与请求转储配置，并持久化到 config.yaml。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        update_configured_logging_settings,
    )
    from aistudio_api.infrastructure.utils.logger import (
        get_dump_dir,
        get_log_level,
        is_debug_env_active,
        is_dump_requests_enabled,
        is_dump_to_file_enabled,
        set_dump_dir,
        set_dump_requests,
        set_dump_to_file,
        set_log_level,
    )
    from aistudio_api.infrastructure.utils.upstream_dump import (
        prune_upstream_snapshots,
        upstream_snapshot_keep,
    )

    if req.upstream_snapshot_keep is not None and not (
        0 <= req.upstream_snapshot_keep <= 1000
    ):
        raise HTTPException(400, "上游成功快照保留条数必须在 0-1000 之间")

    if req.level is not None:
        set_log_level(req.level)
    if req.dump_requests is not None:
        set_dump_requests(req.dump_requests)
    if req.dump_to_file is not None:
        set_dump_to_file(req.dump_to_file)
    if req.dump_dir is not None:
        set_dump_dir(req.dump_dir)

    # 同 create_api_key：config.yaml 读-解析-落盘（fsync）全套同步 I/O 下放
    # 线程池，避免面板保存期间冻结在途模型请求/SSE 流。
    await asyncio.to_thread(
        update_configured_logging_settings,
        level=req.level,
        dump_requests=req.dump_requests,
        dump_to_file=req.dump_to_file,
        dump_dir=req.dump_dir,
        upstream_snapshot_keep=req.upstream_snapshot_keep,
    )
    # 保留条数调低（或置 0）后立即收缩成功环，不等下一次成功写盘。
    if req.upstream_snapshot_keep is not None:
        await asyncio.to_thread(prune_upstream_snapshots)
    logger.info(
        "日志配置已更新: 级别=%s, 请求转储=%s, 文件转储=%s, 目录=%s, 成功快照保留=%s",
        req.level,
        req.dump_requests,
        req.dump_to_file,
        req.dump_dir,
        req.upstream_snapshot_keep,
    )

    return {
        "ok": True,
        "log_level": get_log_level(),
        "dump_requests": is_dump_requests_enabled(),
        "debug_env_active": is_debug_env_active(),
        "dump_to_file": is_dump_to_file_enabled(),
        "dump_dir": str(get_dump_dir()),
        "upstream_snapshot_keep": upstream_snapshot_keep(),
    }


# ========== 抗截断传输面板配置 ==========


class UpdateAntiTruncationConfigRequest(BaseModel):
    enabled: bool | None = None
    tool_name: str | None = None
    variants_enabled: bool | None = None
    models: list[str] | None = None


def _anti_truncation_payload(
    models: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """组装面板所需的抗截断配置 + 可选模型列表（过滤生图/视频/音频）。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_anti_truncation_settings,
    )

    settings = get_anti_truncation_settings()
    raw_variants = settings.get("model_variants")
    variants = raw_variants if isinstance(raw_variants, dict) else {}
    available: list[dict[str, str]] = []
    for item in models:
        if (
            item.get("is_image_model")
            or item.get("is_video_model")
            or item.get("is_audio_model")
        ):
            continue
        model_id = str(item.get("id") or item.get("name") or "")
        if not model_id:
            continue
        available.append(
            {
                "id": model_id.removeprefix("models/"),
                "displayName": str(item.get("displayName") or model_id),
            }
        )
    return {
        "enabled": bool(settings.get("enabled")),
        "tool_name": str(settings.get("tool_name") or "router_emit"),
        "variants": {
            "enabled": bool(variants.get("enabled", False)),
            "models": [str(m) for m in (variants.get("models") or [])],
        },
        "available_models": available,
    }


@protected_router.get("/config/anti-truncation")
async def get_anti_truncation_config() -> dict[str, object]:
    """获取抗截断传输配置与可选模型列表（面板"抗截断传输"卡片数据源）。"""
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    models = await model_discovery.get_models()
    return _anti_truncation_payload(models)


@protected_router.put("/config/anti-truncation")
async def update_anti_truncation_config(
    req: UpdateAntiTruncationConfigRequest,
) -> dict[str, object]:
    """更新抗截断传输配置（全局开关 / 变体开关 / 选中模型）并持久化。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        update_anti_truncation_settings,
    )
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    # 同 create_api_key：config.yaml 持久化全套同步 I/O 下放线程池。
    await asyncio.to_thread(
        update_anti_truncation_settings,
        enabled=req.enabled,
        tool_name=req.tool_name,
        variants_enabled=req.variants_enabled,
        models=req.models,
    )
    logger.info(
        "抗截断配置已更新: 全局=%s, 变体=%s, 模型数=%d",
        req.enabled,
        req.variants_enabled,
        len(req.models or []),
    )

    models = await model_discovery.get_models()
    payload = _anti_truncation_payload(models)
    payload["ok"] = True
    return payload


# ========== API Key 备注与密钥管理 ==========


class ApiKeyItemModel(BaseModel):
    name: str = "API Key"
    key: str = ""
    created_at: str | None = None


class CreateApiKeyRequest(BaseModel):
    name: str = "API Key"
    key: str | None = None


class UpdateApiKeyRequest(BaseModel):
    name: str


# ── 出站保活设置（面板「出站保活」卡片数据源） ─────────────────────────


class UpdateKeepaliveConfigRequest(BaseModel):
    """PUT /config/keepalive 请求体（三字段全量提交）。"""

    url: str = ""
    interval: int = Field(default=60, ge=5, le=86400)
    allow_private: bool = False


def _keepalive_env_override() -> dict[str, str]:
    """环境变量注入展示（AISTUDIO_KEEPALIVE_* 非空时面板标注「环境变量注入」）。"""
    overrides: dict[str, str] = {}
    if os.getenv("AISTUDIO_KEEPALIVE_URL", "").strip():
        overrides["url"] = "AISTUDIO_KEEPALIVE_URL"
    if os.getenv("AISTUDIO_KEEPALIVE_INTERVAL", "").strip():
        overrides["interval"] = "AISTUDIO_KEEPALIVE_INTERVAL"
    if os.getenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", "").strip():
        overrides["allow_private"] = "AISTUDIO_KEEPALIVE_ALLOW_PRIVATE"
    return overrides


def _keepalive_env_prefill() -> dict[str, object]:
    """按 Settings 同口径解析环境变量当前值（表单预填用）。

    解析口径与 config.Settings 字段构造一致：URL 原样 strip、间隔
    int 失败回退 60、布尔按 not in ("0","false","False") 判真。
    """
    raw_interval = os.getenv("AISTUDIO_KEEPALIVE_INTERVAL")
    try:
        interval = (
            int(raw_interval.strip()) if raw_interval and raw_interval.strip() else 60
        )
    except ValueError:
        interval = 60
    allow_private = os.getenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE")
    return {
        "url": os.getenv("AISTUDIO_KEEPALIVE_URL", "").strip(),
        "interval": interval,
        "allow_private": allow_private is not None
        and allow_private not in ("0", "false", "False"),
    }


def _keepalive_payload() -> dict[str, object]:
    """组装保活卡片数据：表单值（env 注入优先预填）+ 注入标注 + 运行态。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_keepalive_settings,
    )
    from aistudio_api.infrastructure.outbound_keepalive import (
        keepalive_runtime_override_active,
        outbound_keepalive_service,
    )

    yaml_settings = get_keepalive_settings()
    env_overrides = _keepalive_env_override()
    env_prefill = _keepalive_env_prefill()
    # 表单初值：运行时覆盖生效中（面板保存过）→ 覆盖值；否则环境变量
    # 注入的字段预填 env 值（面板保存即覆盖）；未注入字段保持 yaml 值。
    from aistudio_api.infrastructure.outbound_keepalive import (
        keepalive_runtime_override,
    )

    override = keepalive_runtime_override()
    if override is not None:
        form = {
            "url": override[0],
            "interval": override[1],
            "allow_private": override[2],
        }
    else:
        form = {
            "url": str(env_prefill["url"])
            if "url" in env_overrides
            else str(yaml_settings["url"]),
            "interval": (
                int(env_prefill["interval"])  # type: ignore[arg-type]
                if "interval" in env_overrides
                else int(yaml_settings["interval"])  # type: ignore[arg-type]
            ),
            "allow_private": (
                bool(env_prefill["allow_private"])
                if "allow_private" in env_overrides
                else bool(yaml_settings["allow_private"])
            ),
        }
    service = outbound_keepalive_service
    return {
        **form,
        "yaml_url": str(yaml_settings["url"]),
        "yaml_interval": int(yaml_settings["interval"]),  # type: ignore[arg-type]
        "yaml_allow_private": bool(yaml_settings["allow_private"]),
        "running": service.is_running,
        "env_overrides": env_overrides,
        "runtime_override_active": keepalive_runtime_override_active(),
    }


@protected_router.get("/config/keepalive")
async def get_keepalive_config() -> dict[str, object]:
    """获取出站保活设置（URL / 间隔 / 私网许可）与服务运行态。"""
    return _keepalive_payload()


@protected_router.put("/config/keepalive")
async def update_keepalive_config(
    req: UpdateKeepaliveConfigRequest,
) -> dict[str, object]:
    """更新出站保活配置，持久化到 config.yaml 并立即重启保活任务。

    URL 非空时先行校验（http/https + 合法主机名；私网目标需
    allow_private，否则 400 拒绝——保活任务可被劫持会话用于内网探测）。
    """
    from aistudio_api.infrastructure.gateway.model_defaults import (
        update_keepalive_settings,
    )
    from aistudio_api.infrastructure.outbound_keepalive import (
        outbound_keepalive_service,
        set_keepalive_runtime_override,
        validate_keepalive_url,
    )

    url = req.url.strip()
    if url and not validate_keepalive_url(url, allow_private=req.allow_private):
        if validate_keepalive_url(url, allow_private=True):
            raise HTTPException(
                status_code=400,
                detail="保活目标是私网/环回地址：如确属自我保活或内网监控，请先开启「允许私网目标」再保存",
            )
        raise HTTPException(
            status_code=400,
            detail="保活 URL 必须是包含主机名的合法 http(s) 地址",
        )
    # 同 create_api_key：config.yaml 持久化全套同步 I/O 下放线程池。
    await asyncio.to_thread(
        update_keepalive_settings,
        url=url,
        interval=req.interval,
        allow_private=req.allow_private,
    )
    # 运行时覆盖 > 环境变量：面板保存立即生效（含覆盖环境变量注入值），
    # 重启后回退环境变量/配置文件。之后按新配置重启保活任务（URL 清空
    # 时即为停止）。
    set_keepalive_runtime_override(
        url=url,
        interval=req.interval,
        allow_private=req.allow_private,
    )
    await outbound_keepalive_service.restart()
    logger.info(
        "出站保活配置已更新: url=%s, interval=%ds, allow_private=%s",
        url or "(空，停止保活)",
        req.interval,
        req.allow_private,
    )
    return _keepalive_payload()


def _normalize_key_entries(raw: object) -> list[dict[str, str]]:
    """把 config.yaml 的 api_keys 原始项归一化为三项 dict 列表。

    兼容历史形态：dict 项（可能缺 name/created_at）与裸 str 项（含
    flow/字符串列表）都归一为 ``{"name", "key", "created_at"}``；
    空项与空字符串丢弃。create/delete/update 三个端点的读改写共用。
    """
    entries: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                entries.append(
                    {
                        "name": str(item.get("name") or "API Key"),
                        "key": str(item.get("key") or ""),
                        "created_at": str(item.get("created_at") or ""),
                    }
                )
            elif isinstance(item, str) and item.strip():
                entries.append(
                    {"name": "API Key", "key": item.strip(), "created_at": ""}
                )
    return entries


@protected_router.get("/api-keys", response_model=list[ApiKeyItemModel])
async def list_api_keys(request: Request) -> list[ApiKeyItemModel]:
    """获取已配置的 API Keys 列表（config.yaml + 环境变量注入合并展示）。

    脱敏（2.1）：未配置管理密码时，管理端以 API Key 回退鉴权，任意 Key
    持有者不应看到其他 Key 的明文，返回脱敏形式；配置了管理密码时
    （即真正的管理员会话）才返回明文。
    """
    from aistudio_api.api.dependencies import is_admin_password_session, mask_api_key
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_key_items,
    )

    show_plaintext = is_admin_password_session(request)
    items = get_configured_api_key_items()
    result = [
        ApiKeyItemModel(
            name=item.get("name") or "API Key",
            key=(item.get("key") or "")
            if show_plaintext
            else mask_api_key(str(item.get("key") or "")),
            created_at=item.get("created_at") or None,
        )
        for item in items
    ]

    # 追加环境变量注入的 Key（只读展示：来源 AISTUDIO_API_KEYS / AISTUDIO_API_KEY，
    # 无法在 UI 中删除或修改，随容器重启自动恢复）
    seen_keys = {item.get("key") for item in items}

    for key in sorted(settings.api_keys):
        if key in seen_keys:
            continue
        result.append(
            ApiKeyItemModel(
                name="环境变量注入 (ENV)",
                key=key if show_plaintext else mask_api_key(key),
                created_at=None,
            )
        )
    return result


@protected_router.post("/api-keys", response_model=ApiKeyItemModel)
async def create_api_key(req: CreateApiKeyRequest) -> ApiKeyItemModel:
    """在 config.yaml 中添加新的 API Key。"""
    import secrets
    from datetime import UTC, datetime

    new_key = (
        req.key.strip()
        if req.key and req.key.strip()
        else f"sk-aistudio-{secrets.token_hex(16)}"
    )
    created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    name = req.name.strip() if req.name.strip() else "API Key"

    def _persist_new_key(parsed: dict[str, object]) -> None:
        new_list = _normalize_key_entries(parsed.get("api_keys"))
        new_list.append({"name": name, "key": new_key, "created_at": created_at})
        parsed["api_keys"] = new_list

    # config.yaml 全套读-解析-落盘（fsync）是同步 I/O：直接在事件循环执行
    # 会拖停所有在途模型请求/SSE 流，下放线程池（写锁为 threading.Lock，
    # 跨线程语义不变）。
    await asyncio.to_thread(update_config_yaml_document, _persist_new_key, None)

    logger.info("创建 API Key: 备注=%s, 前缀=%s", name, new_key[:8] + "...")
    return ApiKeyItemModel(name=name, key=new_key, created_at=created_at)


@protected_router.delete("/api-keys/{key_value:path}")
async def delete_api_key(key_value: str) -> dict[str, bool]:
    """在 config.yaml 中删除指定 API Key。"""

    def _persist_delete_key(parsed: dict[str, object]) -> None:
        new_list: list[dict[str, str]] = []
        found = False
        for item in _normalize_key_entries(parsed.get("api_keys")):
            if item["key"] == key_value:
                found = True
                continue
            new_list.append(item)

        if not found:
            raise HTTPException(404, detail="未找到该 API Key")

        parsed["api_keys"] = new_list

    # 同 create_api_key：读-解析-落盘全套同步 I/O 下放线程池。
    await asyncio.to_thread(update_config_yaml_document, _persist_delete_key, None)

    logger.info("删除 API Key: 前缀=%s", key_value[:8] + "...")
    return {"ok": True}


@protected_router.put("/api-keys/{key_value:path}", response_model=ApiKeyItemModel)
async def update_api_key_name(
    key_value: str, req: UpdateApiKeyRequest
) -> ApiKeyItemModel:
    """在 config.yaml 中更新指定 API Key 的备注名。"""

    def _persist_rename_key(parsed: dict[str, object]) -> None:
        new_list: list[dict[str, str]] = []
        updated_item: ApiKeyItemModel | None = None
        new_name_val = req.name.strip() if req.name.strip() else "API Key"

        for item in _normalize_key_entries(parsed.get("api_keys")):
            if item["key"] == key_value:
                updated_item = ApiKeyItemModel(
                    name=new_name_val,
                    key=item["key"],
                    created_at=item["created_at"] or None,
                )
                new_list.append(
                    {
                        "name": new_name_val,
                        "key": item["key"],
                        "created_at": item["created_at"],
                    }
                )
            else:
                new_list.append(item)

        if not updated_item:
            raise HTTPException(404, detail="未找到该 API Key")

        parsed["api_keys"] = new_list
        _rename_result.append(updated_item)

    _rename_result: list[ApiKeyItemModel] = []

    # 同 create_api_key：读-解析-落盘全套同步 I/O 下放线程池。
    await asyncio.to_thread(update_config_yaml_document, _persist_rename_key, None)

    updated = _rename_result[0]
    logger.info(
        "更新 API Key 备注: 前缀=%s, 备注=%s", key_value[:8] + "...", updated.name
    )

    return updated


# ========== 全局出站代理（自 routes_operations.py 迁入） ==========
# 实际 URL 为 /api/proxies/global：迁移前挂在 routes_operations 的
# prefix="/api" 路由下；本模块的 router 无 prefix，装饰路径写全。


class GlobalProxyRequest(BaseModel):
    proxy_url: str = ""


@protected_router.put("/api/proxies/global")
async def update_global_proxy(req: GlobalProxyRequest) -> dict[str, object]:
    """更新全局出站代理（写 config.yaml + 当前进程即时生效）。"""
    from aistudio_api.infrastructure.account.cookie_parser import normalize_proxy_url

    proxy = normalize_proxy_url(req.proxy_url) if req.proxy_url.strip() else None
    if req.proxy_url.strip() and proxy is None:
        raise HTTPException(400, "代理地址无效")

    def save(parsed: dict[str, object]) -> None:
        network = parsed.get("network")
        if not isinstance(network, dict):
            network = {}
        parsed["network"] = {**network, "proxy_url": proxy or ""}

    # 代理 URL 可能内嵌用户名/密码：与迁移前一致按私密文件（0600）落盘。
    await asyncio.to_thread(update_config_yaml_document, save, None, private=True)
    settings.proxy_url = proxy
    if runtime_state.client:
        await runtime_state.client.session.set_default_proxy(proxy)
    return {
        "ok": True,
        "global_proxy": redact_text(proxy or ""),
        "note": "当前进程立即生效；重启时环境变量代理配置优先。",
    }
