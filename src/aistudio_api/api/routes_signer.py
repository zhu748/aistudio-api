"""WAA 签名服务器管理与纯手动切换路由（自 routes_system.py 拆分）。"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.waa.daemon import is_local_signer_url

logger = get_logger("routes.signer")

public_router = APIRouter()
protected_router = APIRouter()


class SwitchSignerServerRequest(BaseModel):
    server_id: str


class UpsertSignerServerRequest(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    url: str
    user_agent: str = ""
    auth_token_env: str = ""
    headers: dict[str, str] = {}


class TestSignerServerRequest(BaseModel):
    url: str


@protected_router.get("/config/signer-servers")
async def get_signer_servers(request: Request) -> dict[str, object]:
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

    from aistudio_api.api.dependencies import is_admin_password_session

    # 自定义 headers 可含远程签名器的 Authorization 共享密钥、
    # auth_token_env 指向的环境变量——非管理密码会话一律掩码
    mask_sensitive = not is_admin_password_session(request)
    result_servers = []
    for s in servers:
        if isinstance(s, dict):
            s_id = str(s.get("id") or "").strip()
            item = dict(s)
            if mask_sensitive:
                headers = item.get("headers")
                if isinstance(headers, dict) and headers:
                    item["headers"] = dict.fromkeys(headers, "***")
                if item.get("auth_token_env"):
                    item["auth_token_env"] = "***"
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


@protected_router.get("/config/signer-health")
async def get_signer_health_state() -> dict[str, object]:
    """WAA 签名器健康熔断状态（语义与判据见 waa/health.py）。

    面板/运维排障用途：当 GenerateContent 集中 403 而 CountTokens 等
    轻量端点正常时，优先查看本端点 —— ``degraded=true`` 意味着
    「新铸 proof 被上游拒绝」已跨账号关联，根因大概率在签名服务
    （如远程 VM 挑战过期，2026-11-12 spinsnow 实测案例），而非账号。
    """
    from aistudio_api.infrastructure.waa.health import get_signer_health

    return get_signer_health().state_summary()


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
    startup_error: str | None = None
    if is_local_signer_url(active_url):
        from aistudio_api.infrastructure.waa.daemon import ensure_signer_ready

        startup_error = await asyncio.to_thread(ensure_signer_ready)
        # 兼容二进制缺失自动回退远程的情况，重新读取实际生效节点
        active = get_active_waa_server()
    else:
        from aistudio_api.infrastructure.waa.daemon import stop_signer_daemon

        await asyncio.to_thread(stop_signer_daemon)
    env_override = os.getenv("AISTUDIO_WAA_SERVER", "").strip()
    runtime_override = get_runtime_waa_override()
    switched = active.get("id") == target_id and startup_error is None
    error = None
    if not switched:
        error = (
            f"切换至 [{target_id}] 失败：{startup_error or '目标节点未生效'}。"
            f"当前实际激活节点为 [{active.get('id')}]。"
        )
    note: str | None = None
    if switched and env_override and env_override != target_id:
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
        "ok": switched,
        "active_server": active,
        "runtime_override": runtime_override or None,
        "note": note,
        "error": error,
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
        get_runtime_waa_override,
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
    startup_error: str | None = None
    if is_local_signer_url(active_url):
        from aistudio_api.infrastructure.waa.daemon import ensure_signer_ready

        startup_error = await asyncio.to_thread(ensure_signer_ready)
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
        "ok": startup_error is None,
        "active_server": active,
        "runtime_override": get_runtime_waa_override(),
        "changed": True,
        "error": startup_error,
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
    try:
        from aistudio_api.infrastructure.proxy_bridge import resolve_proxy_url_async

        proxy = None if is_local else await resolve_proxy_url_async(settings.proxy_url)
    except Exception as exc:
        # turn/sstp 桥接冷启动失败：远程签名节点探活不可达，返回可操作
        # 指引（fail-closed，不静默改直连探测）。
        return {
            "ok": False,
            "error": f"出站代理解析失败：{exc}",
            "elapsed_ms": 0.0,
        }

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
        if is_local_signer_url(target_url, port=8081):
            from aistudio_api.infrastructure.waa.daemon import ensure_signer_ready

            startup_error = await asyncio.to_thread(
                ensure_signer_ready, server_url=target_url
            )
            if startup_error:
                return {
                    "ok": False,
                    "error": startup_error,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                }
        async with httpx.AsyncClient(
            timeout=8.0, proxy=proxy, trust_env=False
        ) as client:
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
