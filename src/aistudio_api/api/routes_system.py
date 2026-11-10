"""System and metadata routes."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.response_models import (
    HealthResponse,
    ModelStatsResponse,
    StatsResponse,
    StatsTotalsResponse,
)
from aistudio_api.infrastructure.gateway.model_defaults import (
    _resolve_config_path,
    config_file_write_lock,
    invalidate_config_cache,
)
from aistudio_api.infrastructure.utils.common import atomic_write_text
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.waa.daemon import is_local_signer_url

logger = get_logger("routes.system")

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState

public_router = APIRouter()
protected_router = APIRouter()

# config.yaml 的所有读-改-写路径（整文件保存、API Key 增删改备注）共用一把锁：
# 这些端点都是「read_text → 改内存 → 整文件覆盖」，并发请求会互相抹掉对方的改动。
# 锁定义在 model_defaults.config_file_write_lock，与那 5 个配置写函数共用同一把，
# 否则跨模块并发（面板保存 YAML 同时切换 WAA 节点）仍会互相覆盖。


def health_response() -> HealthResponse:
    return HealthResponse(status="ok")


def stats_response() -> StatsResponse:
    from aistudio_api.api.response_models import AccountBreakdownItem
    from aistudio_api.api.state import runtime_state
    from aistudio_api.infrastructure.gateway.model_defaults import resolve_tier_quota

    stats = dict(runtime_state.model_stats)
    rotator = runtime_state.rotator
    account_svc = runtime_state.account_service
    accounts_list = account_svc.list_accounts() if account_svc else []

    totals = StatsTotalsResponse(
        requests=sum(s.requests for s in stats.values()),
        success=sum(s.success for s in stats.values()),
        rate_limited=sum(s.rate_limited for s in stats.values()),
        errors=sum(s.errors for s in stats.values()),
        prompt_tokens=sum(s.prompt_tokens for s in stats.values()),
        completion_tokens=sum(s.completion_tokens for s in stats.values()),
        reasoning_tokens=sum(s.reasoning_tokens for s in stats.values()),
        cached_tokens=sum(s.cached_tokens for s in stats.values()),
        total_tokens=sum(s.total_tokens for s in stats.values()),
    )

    models = {}
    for name, s in stats.items():
        clean_m = name.removeprefix("models/").lower()
        breakdown: list[AccountBreakdownItem] = []

        if rotator:
            from aistudio_api.application.account_rotator import get_pacific_date_key

            current_la = get_pacific_date_key()
            for acc in accounts_list:
                st = rotator.get_account_stats(acc.id)
                if st:
                    st._check_date_reset(current_la)
                    if clean_m in st.daily_models:
                        m_daily = st.daily_models[clean_m]
                        quota = resolve_tier_quota(acc.tier, clean_m)
                        used_pct = round((m_daily.requests / max(1, quota)) * 100, 1)
                        cd_rem = (
                            max(0, int(m_daily.cooldown_until - time.time()))
                            if m_daily.cooldown_until > time.time()
                            else 0
                        )
                        breakdown.append(
                            AccountBreakdownItem(
                                account_id=acc.id,
                                name=acc.name,
                                auth_user=acc.auth_user,
                                email=acc.email,
                                tier=acc.tier,
                                group=acc.group,
                                enabled=acc.enabled,
                                today_requests=m_daily.requests,
                                today_success=m_daily.success,
                                today_rate_limited=m_daily.rate_limited,
                                today_tokens=m_daily.total_tokens,
                                today_reasoning_tokens=m_daily.reasoning_tokens,
                                configured_quota=quota,
                                quota_used_percent=used_pct,
                                is_available=acc.enabled
                                and cd_rem == 0
                                and not st.session_expired,
                                cooldown_remaining=cd_rem,
                            )
                        )

        models[name] = ModelStatsResponse(
            requests=s.requests,
            success=s.success,
            rate_limited=s.rate_limited,
            errors=s.errors,
            prompt_tokens=s.prompt_tokens,
            completion_tokens=s.completion_tokens,
            reasoning_tokens=s.reasoning_tokens,
            cached_tokens=s.cached_tokens,
            total_tokens=s.total_tokens,
            finish_reasons=dict(s.finish_reasons),
            last_used=s.last_used,
            account_breakdown=breakdown,
        )
    return StatsResponse(models=models, totals=totals)


@public_router.get("/health", response_model=HealthResponse)
async def health():
    return health_response()


# /healthz：与 /health 等价的别名端点，遵循 Kubernetes / PaaS 探活命名惯例
# （Render / Fly.io / k8s 用户习惯性配置 /healthz）。无鉴权、不触上游、
# 不依赖账号，任何启动状态下都应返回 200。
@public_router.get("/healthz", response_model=HealthResponse, include_in_schema=False)
async def healthz():
    return health_response()


@protected_router.get("/stats", response_model=StatsResponse)
async def stats():
    # O(模型数 × 账号数) 的同步聚合（P3）：50 账号 × 10 模型时达数十毫秒，
    # 下放线程池避免阻塞事件循环拖慢并发流式请求。
    return await asyncio.to_thread(stats_response)


# ========== 调度与配额管理 API ==========


class ClearCooldownRequest(BaseModel):
    account_id: str | None = None
    model: str | None = None


@protected_router.get("/rotation")
async def get_rotation_status(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """获取账号黏性调度与配额状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        return {
            "enabled": False,
            "mode": "sticky",
            "message": "调度器未初始化",
            "accounts": {},
        }

    return {
        "enabled": True,
        "mode": "sticky",
        "accounts": rotator.get_all_stats(),
    }


@protected_router.post("/rotation/clear-cooldown")
async def clear_cooldown(
    req: ClearCooldownRequest,
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """清除指定账号或全局的 429 锁定状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    if req.account_id:
        rotator.clear_cooldown(req.account_id, model=req.model)
    else:
        rotator.clear_all_cooldowns()
    logger.info(
        "已清除冷却状态: 账号=%s, 模型=%s",
        req.account_id or "全部",
        req.model or "全部",
    )

    return {"ok": True, "accounts": rotator.get_all_stats()}


@protected_router.post("/rotation/cleanup-ghosts")
async def cleanup_ghosts(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """主动对齐并彻底清除已删除账号在调度器中残留的幽灵状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    cleaned_ids = rotator.cleanup_ghost_accounts()
    return {
        "ok": True,
        "cleaned_count": len(cleaned_ids),
        "cleaned_ids": cleaned_ids,
        "accounts": rotator.get_all_stats(),
    }


@protected_router.get("/rotation/accounts")
async def get_rotation_accounts(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, dict[str, object]]:
    """获取所有账号的调度与配额统计。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    return rotator.get_all_stats()


@protected_router.post("/rotation/next")
async def force_next_account(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """强制切换到下一个可用账号。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    next_account = await rotator.get_next_account()
    if next_account is None:
        raise HTTPException(404, detail="没有可用的账号")

    account_service = runtime_state.account_service
    client = runtime_state.client

    if account_service is None or client is None or client.session is None:
        raise HTTPException(503, detail="服务未就绪")

    result = await account_service.activate_account(
        next_account.id,
        client.session,
    )

    if result is None:
        raise HTTPException(500, detail="切换失败")

    logger.info("手动切换至下一账号: id=%s, name=%s", result.id, result.name)
    return {
        "ok": True,
        "account": {
            "id": result.id,
            "name": result.name,
            "email": result.email,
        },
    }


# ========== 系统与模型配置 API ==========


class ConfigYamlUpdateRequest(BaseModel):
    yaml_content: str


def _mask_yaml_api_keys(yaml_content: str) -> str:
    """对 YAML 文本中 api_keys 块的 key 字段值脱敏（仅展示用）。

    按缩进识别顶层 ``api_keys:`` 块的边界，只替换其中 ``key: <value>``
    行的值，不影响其余内容与注释，保证面板编辑器中的往返安全。
    """
    import re

    lines = yaml_content.splitlines(keepends=True)
    out: list[str] = []
    in_api_keys_block = False
    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped.startswith((" ", "\t", "#")) and stripped.strip():
            # 顶层键：进入/退出 api_keys 块
            in_api_keys_block = stripped.rstrip().endswith(":") and (
                stripped.split(":", 1)[0].strip() == "api_keys"
            )
        elif in_api_keys_block and stripped.strip().startswith("key:"):
            m = re.match(r"^(\s*key:\s*).+$", stripped)
            if m:
                from aistudio_api.api.dependencies import mask_api_key

                raw_value = stripped[m.end(1) :].strip().strip("'\"")
                if raw_value:
                    line = m.group(1) + mask_api_key(raw_value) + "\n"
        out.append(line)
    return "".join(out)


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
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.utils.logger import (
        get_dump_dir,
        get_log_level,
        is_debug_env_active,
        is_dump_requests_enabled,
        is_dump_to_file_enabled,
    )

    config_yaml_path = _resolve_config_path(None)
    yaml_content = ""
    if config_yaml_path.exists():
        with contextlib.suppress(Exception):
            yaml_content = config_yaml_path.read_text(encoding="utf-8")
    if not is_admin_password_session(request):
        yaml_content = _mask_yaml_api_keys(yaml_content)
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_active_waa_server,
    )

    active_waa = get_active_waa_server()
    return {
        "port": settings.port,
        "proof_server_url": str(active_waa.get("url") or settings.proof_server_url),
        "active_signer_server": active_waa.get("id"),
        "proxy_configured": bool(settings.proxy_url),
        "auth_enabled": web_auth_required(),
        "yaml_content": yaml_content,
        "log_level": get_log_level(),
        "dump_requests": is_dump_requests_enabled(),
        "debug_env_active": is_debug_env_active(),
        "dump_to_file": is_dump_to_file_enabled(),
        "dump_dir": str(get_dump_dir()),
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
        with config_file_write_lock():
            atomic_write_text(config_yaml_path, req.yaml_content)
            invalidate_config_cache()

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


# ========== WAA 签名服务器管理与纯手动切换 API ==========


class SwitchSignerServerRequest(BaseModel):
    server_id: str


class UpsertSignerServerRequest(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    url: str
    user_agent: str = ""
    headers: dict[str, str] = {}


class TestSignerServerRequest(BaseModel):
    url: str


@protected_router.get("/config/signer-servers")
async def get_signer_servers() -> dict[str, object]:
    """获取所有配置的 WAA 签名服务器列表及当前激活项。

    激活判定与运行时完全一致，优先级从高到低：
    1. 运行时覆盖（``runtime_override``，面板手动切换写入，进程内即时生效）；
    2. 环境变量 ``AISTUDIO_WAA_SERVER``（启动默认，重启后恢复）；
    3. config.yaml 的 ``waa.active_server``。

    响应字段说明：
    - ``env_override``：环境变量指定的启动默认节点 id（未设置时为 null）；
    - ``runtime_override``：面板切换写入的运行时覆盖 id（未设置时为 null）；
    - 每个 server 的 ``is_env_default``：该节点是否为环境变量指定的启动默认。
    """
    import os

    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_active_waa_server,
        get_configured_waa_settings,
        get_runtime_waa_override,
    )

    waa = get_configured_waa_settings()
    # 运行时实际生效节点（含运行时覆盖/环境变量），与 client/session 的取值逻辑对齐
    active_id = str(get_active_waa_server().get("id") or "local_spidermonkey").strip()
    env_override = os.getenv("AISTUDIO_WAA_SERVER", "").strip()
    runtime_override = get_runtime_waa_override()
    raw_servers = waa.get("servers")
    servers = raw_servers if isinstance(raw_servers, list) else []

    result_servers = []
    for s in servers:
        if isinstance(s, dict):
            s_id = str(s.get("id") or "").strip()
            item = dict(s)
            item["is_active"] = s_id == active_id
            item["is_env_default"] = bool(env_override) and s_id == env_override
            # 兼容旧字段：面板不再将其渲染为“锁定”，仅表示环境变量默认节点
            item["env_locked"] = item["is_env_default"]
            result_servers.append(item)

    return {
        "active_server": active_id,
        "env_override": env_override or None,
        "runtime_override": runtime_override or None,
        "servers": result_servers,
    }


@protected_router.post("/config/signer-servers/switch")
async def switch_signer_server(req: SwitchSignerServerRequest) -> dict[str, object]:
    """纯手动切换当前激活的 WAA 签名服务器（即时生效并持久化）。

    - 写入运行时覆盖层：即便 ``AISTUDIO_WAA_SERVER`` 环境变量指定了
      默认节点（本地或远程），切换也对当前进程立即生效，无需重启；
    - 同步持久化到 config.yaml（有盘部署重启后仍保持）；
    - 切到本地节点时懒启动本地签名守护进程（二进制缺失时自动回退远程）；
    - 切走本地节点时停掉本进程自动拉起的守护实例，避免空转；
    - 环境变量存在且与目标不一致时返回 ``note`` 说明重启后的回退行为
      （仅信息提示，当前进程切换已完全生效）。
    """
    import os

    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_active_waa_server,
        get_configured_waa_settings,
        get_runtime_waa_override,
        switch_active_waa_server,
    )

    target_id = req.server_id.strip()
    waa = get_configured_waa_settings()
    raw_servers = waa.get("servers")
    known_ids = {
        str(s.get("id") or "").strip()
        for s in (raw_servers if isinstance(raw_servers, list) else [])
        if isinstance(s, dict)
    }
    if target_id not in known_ids:
        raise HTTPException(404, detail=f"未找到签名服务器: {target_id}")

    switch_active_waa_server(target_id)

    # 切到本地节点：懒启动本地守护进程（缺失时自动回退远程并写回配置）。
    # ensure_signer_ready 内含阻塞 urlopen/端口探测/子进程等待（最长约 12 秒），
    # 必须下放线程池，否则会卡住整个事件循环、冻结所有并发流式请求。
    active = get_active_waa_server()
    active_url = str(active.get("url") or "")
    if is_local_signer_url(active_url):
        from aistudio_api.infrastructure.waa.daemon import ensure_signer_ready

        await asyncio.to_thread(ensure_signer_ready)
        # 兼容二进制缺失自动回退远程的情况，重新读取实际生效节点
        active = get_active_waa_server()
    else:
        from aistudio_api.infrastructure.waa.daemon import stop_signer_daemon

        await asyncio.to_thread(stop_signer_daemon)
    env_override = os.getenv("AISTUDIO_WAA_SERVER", "").strip()
    runtime_override = get_runtime_waa_override()
    note: str | None = None
    if env_override and env_override != target_id:
        note = (
            f"已切换至 [{target_id}] 并对当前进程立即生效；"
            f"重启后将恢复环境变量 AISTUDIO_WAA_SERVER={env_override} 指定的默认节点。"
            f"（无盘部署建议保持环境变量为长期默认）"
        )
    logger.info(
        "切换 WAA 签名服务: id=%s, url=%s%s",
        active.get("id"),
        active.get("url"),
        f"（启动默认为环境变量 {env_override}，运行时已覆盖）"
        if env_override and env_override != active.get("id")
        else "",
    )
    return {
        "ok": True,
        "active_server": active,
        "runtime_override": runtime_override or None,
        "note": note,
    }


@protected_router.post("/config/signer-servers/reset")
async def reset_signer_server() -> dict[str, object]:
    """清除运行时覆盖，恢复“环境变量 > 配置文件”的默认优先级。

    面板切换写入的运行时覆盖消失后：
    - 设置了 ``AISTUDIO_WAA_SERVER`` 时恢复到环境变量指定的默认节点；
    - 未设置时恢复到 config.yaml 的 ``waa.active_server``。

    适用场景：临时切换排查问题后，一键回到部署时的默认签名节点。
    """
    import os

    from aistudio_api.infrastructure.gateway.model_defaults import (
        clear_runtime_waa_override,
        get_active_waa_server,
    )

    previous = clear_runtime_waa_override()
    if previous is None:
        return {
            "ok": True,
            "active_server": get_active_waa_server(),
            "runtime_override": None,
            "changed": False,
        }

    # 覆盖清除后回到默认节点：远端则停掉本地守护，本地则懒启动。
    # 同 switch 接口：阻塞操作下放线程池，避免卡住事件循环。
    active = get_active_waa_server()
    active_url = str(active.get("url") or "")
    if is_local_signer_url(active_url):
        from aistudio_api.infrastructure.waa.daemon import ensure_signer_ready

        await asyncio.to_thread(ensure_signer_ready)
        active = get_active_waa_server()
    else:
        from aistudio_api.infrastructure.waa.daemon import stop_signer_daemon

        await asyncio.to_thread(stop_signer_daemon)

    env_override = os.getenv("AISTUDIO_WAA_SERVER", "").strip()
    logger.info(
        "恢复 WAA 签名服务默认节点: id=%s%s",
        active.get("id"),
        f"（环境变量默认 {env_override}）" if env_override else "（配置文件默认）",
    )
    return {
        "ok": True,
        "active_server": active,
        "runtime_override": None,
        "changed": True,
    }


@protected_router.put("/config/signer-servers")
async def upsert_signer_server(req: UpsertSignerServerRequest) -> dict[str, object]:
    """添加或修改 WAA 签名服务器配置。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        upsert_waa_server,
    )

    server_data = req.model_dump(exclude_none=True)
    saved = upsert_waa_server(server_data)
    logger.info(
        "保存 WAA 签名服务器配置: id=%s, name=%s",
        saved.get("id"),
        saved.get("name"),
    )
    return {"ok": True, "server": saved}


@protected_router.delete("/config/signer-servers/{server_id}")
async def delete_signer_server(server_id: str) -> dict[str, bool]:
    """删除指定的 WAA 签名服务器。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        delete_waa_server,
    )

    success = delete_waa_server(server_id)
    if not success:
        raise HTTPException(404, detail="未找到该签名服务器")
    logger.info("删除 WAA 签名服务器: id=%s", server_id)
    return {"ok": True}


@protected_router.post("/config/signer-servers/test")
async def test_signer_server(req: TestSignerServerRequest) -> dict[str, object]:
    """测试指定签名服务的连通性与健康状态。

    兼容两代远程签名服务探活协议：
    - 旧版（aistudio-pg-bridge 早期 / 自建 Go signer）：GET /health 无参返回 200。
    - 新版（spinsnow 2026-10 起）：/health 必须携带 Cookie（X-Google-Cookies 头
      或 cookies 查询参数），缺参返回 400 + "Missing cookies" 提示。该 400
      恰恰证明服务在线，仅探活方式过时；有活跃账号时携带 Cookie 复测即可
      得到完整的池化状态（account/poolState）。
    """
    import json as _json
    import time
    from urllib.parse import urlparse

    import httpx

    from aistudio_api.config import settings
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_waa_settings,
    )

    target_url = req.url.rstrip("/")
    parsed = urlparse(target_url)
    host = (parsed.hostname or "").lower()

    # 安全：仅允许探测已在 config.yaml 登记的 WAA 节点，且只放行
    # http(s) 协议。此前用子串判断 "127.0.0.1"/"localhost" 既挡不住
    # 127.0.0.1.evil.com 这类绕过，也把任意用户 URL 当成了合法目标（SSRF）。
    waa_cfg = get_configured_waa_settings()
    raw_servers = waa_cfg.get("servers")
    allowed_urls: set[str] = set()
    for s in raw_servers if isinstance(raw_servers, list) else []:
        if isinstance(s, dict):
            allowed_urls.add(str(s.get("url") or "").rstrip("/"))
    # 兜底：无配置时至少放行内置默认节点
    allowed_urls.add("http://127.0.0.1:8081")
    allowed_urls.add("https://waa.spinsnow.fun")

    if parsed.scheme not in ("http", "https") or target_url not in allowed_urls:
        return {
            "ok": False,
            "error": "目标地址未在签名服务器配置中登记，拒绝探测",
            "elapsed_ms": 0.0,
        }

    is_local = host in ("127.0.0.1", "localhost", "::1")
    proxy = None if is_local else settings.proxy_url

    def _extract_active_cookies() -> str:
        """从当前活跃账号取核心 Cookie，仅用于本机节点的新版协议深度探活。"""
        try:
            from aistudio_api.infrastructure.account.account_store import (
                AccountStore,
            )
            from aistudio_api.infrastructure.account.cookie_parser import (
                filter_minimal_cookies,
                load_cookies_from_auth_file,
            )

            store = AccountStore()
            active = store.get_active_account()
            if not active:
                return ""
            auth_path = store.get_auth_path_optional(active.id, require_exists=True)
            if not auth_path:
                return ""
            cookies = filter_minimal_cookies(load_cookies_from_auth_file(auth_path))
            return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
        except Exception:
            return ""

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=8.0, proxy=proxy) as client:
            resp = await client.get(f"{target_url}/health")
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            is_ok = resp.status_code == 200
            data: dict[str, object] = {}
            note = ""

            if resp.status_code == 400:
                # 新版协议：缺 Cookie 探活被拒，但服务本身在线
                body_text = resp.text[:300]
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": body_text}
                err_text = str(data.get("error") or body_text).lower()
                if "cookies" in err_text:
                    is_ok = True
                    if is_local:
                        note = (
                            "服务在线（新版协议要求探活携带 Cookie，"
                            "携带活跃账号 Cookie 复测中）"
                        )
                        # 仅对本机节点携带活跃账号 Cookie 复测，获取完整池化状态
                        cookie_header = _extract_active_cookies()
                        if cookie_header:
                            try:
                                resp2 = await client.get(
                                    f"{target_url}/health",
                                    headers={"X-Google-Cookies": cookie_header},
                                )
                                if resp2.status_code == 200:
                                    data = resp2.json()
                            except Exception:
                                pass
                    else:
                        # 安全：绝不向远程节点外发活跃账号的核心 Cookie。
                        # 配置里登记的节点仍可能被面板/被篡改的配置注入，
                        # 因此把凭据外发面收敛到本机回环地址。
                        note = (
                            "服务在线（新版协议要求探活携带 Cookie；"
                            "安全策略禁止向远程节点发送账号 Cookie）"
                        )
                else:
                    note = str(data.get("error") or body_text)
            elif is_ok:
                try:
                    data = resp.json()
                except Exception:
                    data = {}

            result: dict[str, object] = {
                "ok": is_ok,
                "status_code": resp.status_code,
                "elapsed_ms": elapsed_ms,
                "data": data,
            }
            if note:
                result["note"] = note
            # 显式 dump 一遍保证 JSON 可序列化（data 可能含非 dict 项）
            _json.dumps(result)
            return result
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "ok": False,
            "error": str(e),
            "elapsed_ms": elapsed_ms,
        }


# ========== 运行日志查看（内存环形缓冲） ==========

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@protected_router.get("/api/logs")
async def get_runtime_logs(
    level: str = "INFO",
    limit: int = 300,
    search: str | None = None,
    order: str = "desc",
) -> dict[str, object]:
    """查询最近运行日志（内存环形缓冲），供面板展示运行日志与报错。

    - ``level``: 最低日志级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL）
    - ``limit``: 返回条数上限（1~1000）
    - ``search``: 关键字过滤（匹配消息、日志器名、级别）
    - ``order``: ``desc`` 最新在前（默认），``asc`` 按时间正序
    """
    from aistudio_api.infrastructure.utils.logger import (
        get_log_buffer_stats,
        get_recent_logs,
    )

    clean_level = str(level or "INFO").strip().upper()
    if clean_level not in _LOG_LEVELS:
        clean_level = "INFO"
    try:
        clean_limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        clean_limit = 300
    clean_order = "asc" if str(order or "").strip().lower() == "asc" else "desc"

    entries = get_recent_logs(
        min_level=clean_level,
        limit=clean_limit,
        search=search,
        order=clean_order,
    )
    stats = get_log_buffer_stats()
    return {
        "ok": True,
        "count": len(entries),
        "total": stats["size"],
        "capacity": stats["capacity"],
        "logs": entries,
    }


@protected_router.delete("/api/logs")
async def clear_runtime_logs() -> dict[str, object]:
    """清空内存日志缓冲。"""
    from aistudio_api.infrastructure.utils.logger import (
        clear_recent_logs,
        get_log_buffer_stats,
    )

    removed = clear_recent_logs()
    logger.info("内存日志缓冲已清空: 清除条数=%d", removed)
    stats = get_log_buffer_stats()
    return {
        "ok": True,
        "removed": removed,
        "total": stats["size"],
        "capacity": stats["capacity"],
    }


@protected_router.post("/api/logs/stream-ticket")
async def create_log_stream_ticket() -> dict[str, object]:
    """签发一次性 SSE 票据，供浏览器 EventSource 通过 URL 携带鉴权。

    管理密码不应出现在 URL 中（会残留在浏览器历史、代理访问日志与 Referer 中），
    因此前端先用请求头完成鉴权，换取短时（30 秒）且仅能使用一次的票据。
    """
    from aistudio_api.api.dependencies import issue_stream_ticket

    return {"ok": True, "ticket": issue_stream_ticket()}


@protected_router.get("/api/logs/stream")
async def stream_runtime_logs(
    request: Request,
    level: str = "INFO",
    limit: int = 300,
) -> StreamingResponse:
    """SSE 实时日志流：连接后先下发历史快照，随后持续增量推送新日志。

    - ``level``: 最低日志级别过滤（连接期间固定，切换级别由前端重连实现）
    - ``limit``: 初始历史快照条数上限（1~500）
    - 鉴权复用 ``require_web_auth``：浏览器 EventSource 无法携带自定义请求头，
      因此前端先用请求头调用 ``POST /api/logs/stream-ticket`` 换取一次性票据，
      再通过 ``?ticket=`` 携带。管理密码不会出现在 URL 中。
    - 空闲期每 20 秒发送一次 ``ping`` 心跳事件（附带缓冲统计），
      既防止反向代理层超时断连，也作为前端 total/capacity 的低频刷新源

    事件协议：
    ``ready``     连接确认（附带缓冲统计与订阅级别）
    ``snapshot``  历史快照（正序日志数组，重连时前端用它整体替换本地列表）
    ``log``       单条增量日志
    ``ping``      心跳（附带缓冲统计）
    """
    import asyncio
    import json as _json
    from collections.abc import AsyncIterator

    from aistudio_api.infrastructure.utils.logger import (
        get_log_buffer_stats,
        get_recent_logs,
        passes_min_level,
        subscribe_log_entries,
        unsubscribe_log_entries,
    )

    clean_level = str(level or "INFO").strip().upper()
    if clean_level not in _LOG_LEVELS:
        clean_level = "INFO"
    try:
        clean_limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        clean_limit = 300

    def _sse(event: str, payload: object) -> str:
        data = _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event}\ndata: {data}\n\n"

    async def _event_stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()

        def _on_entry(entry: dict[str, object]) -> None:
            # 级别过滤在入队前完成，低于订阅级别的日志不占用连接带宽
            if not passes_min_level(str(entry.get("level")), clean_level):
                return

            def _put() -> None:
                try:
                    queue.put_nowait(entry)
                except asyncio.QueueFull:
                    # 慢消费者：丢弃最旧一条，优先保留最新日志
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(entry)

            with contextlib.suppress(RuntimeError):
                # 事件循环已关闭（进程关闭阶段）会抛 RuntimeError，静默丢弃
                loop.call_soon_threadsafe(_put)

        handle = subscribe_log_entries(_on_entry)
        pushed = 0
        t_start = time.perf_counter()
        client_ip = request.client.host if request.client else "unknown"
        logger.info("面板实时日志流已连接: 客户端=%s, 级别>=%s", client_ip, clean_level)
        try:
            # 1) ready：连接确认 + 缓冲统计（字段命名与 /api/logs 响应保持一致）
            stats = get_log_buffer_stats()
            yield _sse(
                "ready",
                {
                    "ok": True,
                    "level": clean_level,
                    "total": stats["size"],
                    "capacity": stats["capacity"],
                },
            )
            # 2) 历史快照：在订阅之后读取，保证快照与增量流无缝衔接
            snapshot = get_recent_logs(
                min_level=clean_level,
                limit=clean_limit,
                order="asc",
            )
            if snapshot:
                yield _sse("snapshot", {"count": len(snapshot), "logs": snapshot})
            # 3) 先排空订阅瞬间积压的增量（跳过已包含在快照中的条目，按对象身份去重）
            snapshot_ids = {id(entry) for entry in snapshot}
            backlog: list[dict[str, object]] = []
            while True:
                try:
                    pending = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if id(pending) not in snapshot_ids:
                    backlog.append(pending)
            for entry in backlog:
                yield _sse("log", entry)
                pushed += 1
            # 4) 持续增量推送 + 空闲心跳
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    stats = get_log_buffer_stats()
                    yield _sse(
                        "ping",
                        {
                            "ok": True,
                            "total": stats["size"],
                            "capacity": stats["capacity"],
                        },
                    )
                    continue
                yield _sse("log", entry)
                pushed += 1
        finally:
            unsubscribe_log_entries(handle)
            logger.info(
                "面板实时日志流已断开: 客户端=%s, 推送条数=%d, 持续=%.1fs",
                client_ip,
                pushed,
                time.perf_counter() - t_start,
            )

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class LoggingConfigRequest(BaseModel):
    level: str | None = None
    dump_requests: bool | None = None
    dump_to_file: bool | None = None
    dump_dir: str | None = None


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

    if req.level is not None:
        set_log_level(req.level)
    if req.dump_requests is not None:
        set_dump_requests(req.dump_requests)
    if req.dump_to_file is not None:
        set_dump_to_file(req.dump_to_file)
    if req.dump_dir is not None:
        set_dump_dir(req.dump_dir)

    update_configured_logging_settings(
        level=req.level,
        dump_requests=req.dump_requests,
        dump_to_file=req.dump_to_file,
        dump_dir=req.dump_dir,
    )
    logger.info(
        "日志配置已更新: 级别=%s, 请求转储=%s, 文件转储=%s, 目录=%s",
        req.level,
        req.dump_requests,
        req.dump_to_file,
        req.dump_dir,
    )

    return {
        "ok": True,
        "log_level": get_log_level(),
        "dump_requests": is_dump_requests_enabled(),
        "debug_env_active": is_debug_env_active(),
        "dump_to_file": is_dump_to_file_enabled(),
        "dump_dir": str(get_dump_dir()),
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

    update_anti_truncation_settings(
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
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

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
    from aistudio_api.config import settings

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

    config_path = _resolve_config_path(None)
    with config_file_write_lock():
        content = (
            config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        )
        parsed = yaml.safe_load(content) or {}
        if not isinstance(parsed, dict):
            parsed = {}

        raw_keys = parsed.get("api_keys")
        new_list: list[dict[str, str]] = []
        if isinstance(raw_keys, list):
            for item in raw_keys:
                if isinstance(item, dict):
                    new_list.append(
                        {
                            "name": str(item.get("name") or "API Key"),
                            "key": str(item.get("key") or ""),
                            "created_at": str(item.get("created_at") or ""),
                        }
                    )
                elif isinstance(item, str) and item.strip():
                    new_list.append(
                        {"name": "API Key", "key": item.strip(), "created_at": ""}
                    )

        new_list.append({"name": name, "key": new_key, "created_at": created_at})
        parsed["api_keys"] = new_list

        atomic_write_text(
            config_path, yaml.dump(parsed, allow_unicode=True, sort_keys=False)
        )
        invalidate_config_cache()

    logger.info("创建 API Key: 备注=%s, 前缀=%s", name, new_key[:8] + "...")
    return ApiKeyItemModel(name=name, key=new_key, created_at=created_at)


@protected_router.delete("/api-keys/{key_value:path}")
async def delete_api_key(key_value: str) -> dict[str, bool]:
    """在 config.yaml 中删除指定 API Key。"""

    config_path = _resolve_config_path(None)
    with config_file_write_lock():
        content = (
            config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        )
        parsed = yaml.safe_load(content) or {}
        if not isinstance(parsed, dict):
            raise HTTPException(404, detail="未找到 API Key")

        raw_keys = parsed.get("api_keys")
        new_list: list[dict[str, str]] = []
        found = False
        if isinstance(raw_keys, list):
            for item in raw_keys:
                item_key = str(
                    item.get("key") if isinstance(item, dict) else item
                ).strip()
                if item_key == key_value:
                    found = True
                    continue
                if isinstance(item, dict):
                    new_list.append(
                        {
                            "name": str(item.get("name") or "API Key"),
                            "key": item_key,
                            "created_at": str(item.get("created_at") or ""),
                        }
                    )
                elif item_key:
                    new_list.append(
                        {"name": "API Key", "key": item_key, "created_at": ""}
                    )

        if not found:
            raise HTTPException(404, detail="未找到该 API Key")

        parsed["api_keys"] = new_list
        atomic_write_text(
            config_path, yaml.dump(parsed, allow_unicode=True, sort_keys=False)
        )
        invalidate_config_cache()

    logger.info("删除 API Key: 前缀=%s", key_value[:8] + "...")
    return {"ok": True}


@protected_router.put("/api-keys/{key_value:path}", response_model=ApiKeyItemModel)
async def update_api_key_name(
    key_value: str, req: UpdateApiKeyRequest
) -> ApiKeyItemModel:
    """在 config.yaml 中更新指定 API Key 的备注名。"""

    config_path = _resolve_config_path(None)
    with config_file_write_lock():
        content = (
            config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        )
        parsed = yaml.safe_load(content) or {}
        if not isinstance(parsed, dict):
            raise HTTPException(404, detail="未找到 API Key")

        raw_keys = parsed.get("api_keys")
        new_list: list[dict[str, str]] = []
        updated_item: ApiKeyItemModel | None = None
        new_name = req.name.strip() if req.name.strip() else "API Key"

        if isinstance(raw_keys, list):
            for item in raw_keys:
                item_key = str(
                    item.get("key") if isinstance(item, dict) else item
                ).strip()
                created = str(item.get("created_at") if isinstance(item, dict) else "")
                if item_key == key_value:
                    updated_item = ApiKeyItemModel(
                        name=new_name, key=item_key, created_at=created or None
                    )
                    new_list.append(
                        {"name": new_name, "key": item_key, "created_at": created}
                    )
                else:
                    name_val = str(
                        item.get("name") if isinstance(item, dict) else "API Key"
                    )
                    new_list.append(
                        {"name": name_val, "key": item_key, "created_at": created}
                    )

        if not updated_item:
            raise HTTPException(404, detail="未找到该 API Key")

        parsed["api_keys"] = new_list
        atomic_write_text(
            config_path, yaml.dump(parsed, allow_unicode=True, sort_keys=False)
        )
        invalidate_config_cache()
    logger.info("更新 API Key 备注: 前缀=%s, 备注=%s", key_value[:8] + "...", new_name)

    return updated_item
