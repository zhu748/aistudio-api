"""Request history, proxy diagnostics and readiness for operators."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.model_defaults import get_local_waa_server
from aistudio_api.infrastructure.request_context import active_requests
from aistudio_api.infrastructure.request_history import request_history
from aistudio_api.infrastructure.utils.log_files import read_log_history
from aistudio_api.infrastructure.utils.logger import _mask_json_value, get_dump_dir
from aistudio_api.infrastructure.utils.redaction import redact_text
from aistudio_api.infrastructure.utils.upstream_dump import (
    clear_upstream_snapshots,
    upstream_snapshot_stats,
)

router = APIRouter(prefix="/api")


@router.get("/requests")
async def list_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    model: str = "",
    account_id: str = "",
    key_id: str = "",
    result: str = "",
    search: str = Query("", max_length=256),
    since: float = Query(0, ge=0),
    until: float = Query(0, ge=0),
    protocol: str = "",
    retried: bool = False,
    min_elapsed_ms: float = Query(0, ge=0),
) -> dict[str, object]:
    return await asyncio.to_thread(
        request_history().query,
        page=page,
        page_size=page_size,
        model=model,
        account_id=account_id,
        key_id=key_id,
        result=result,
        search=search,
        since=since,
        until=until,
        protocol=protocol,
        retried=retried,
        min_elapsed_ms=min_elapsed_ms,
    )


@router.get("/requests/export")
async def export_requests(
    since: float = Query(0, ge=0),
    until: float = Query(0, ge=0),
    model: str = "",
    result: str = "",
    account_id: str = "",
    key_id: str = "",
    search: str = Query("", max_length=256),
    protocol: str = "",
    retried: bool = False,
    min_elapsed_ms: float = Query(0, ge=0),
) -> dict[str, object]:
    return await asyncio.to_thread(
        request_history().query,
        # 导出上限：10 万行历史理论响应 GB 级且同步 JSON 构造长期占用
        # 线程池；5000 行覆盖常规审计，更大范围请用时间窗分批导出
        page_size=5000,
        since=since,
        until=until,
        model=model,
        result=result,
        account_id=account_id,
        key_id=key_id,
        search=search,
        protocol=protocol,
        retried=retried,
        min_elapsed_ms=min_elapsed_ms,
    )


@router.get("/requests/upstream-snapshots")
async def upstream_snapshot_inventory() -> dict[str, object]:
    """上游交换快照库存（成功/错误条数、总字节、保留条数配置）。"""
    return await asyncio.to_thread(upstream_snapshot_stats)


@router.delete("/requests/upstream-snapshots")
async def clear_upstream_snapshot_files(
    scope: str = Query("all", pattern="^(all|success)$"),
) -> dict[str, object]:
    """手动清理上游交换快照（scope=all 全部 / success 仅成功快照，错误现场保留）。"""
    removed = await asyncio.to_thread(clear_upstream_snapshots, scope)
    return {"ok": True, "scope": scope, "removed": removed}


@router.get("/requests/active")
async def list_active_requests() -> dict[str, object]:
    def _snapshot() -> list[dict[str, object]]:
        masked = _mask_json_value(active_requests())
        return masked if isinstance(masked, list) else []

    # asdict 全量快照 + 递归脱敏是毫秒级纯 CPU：并发流式请求越多（events/
    # attempts 满额），面板轮询期间的事件循环停顿越长，必须下放线程池。
    records = await asyncio.to_thread(_snapshot)
    return {"items": records, "total": len(records)}


@router.get("/requests/stats")
async def request_statistics(
    since: float = Query(0, ge=0),
    until: float = Query(0, ge=0),
    model: str = "",
    account_id: str = "",
) -> dict[str, object]:
    now = time.time()
    start = max(since or now - 86400, now - settings.history_days * 86400)
    end = min(until or now, now)
    if start > end:
        raise HTTPException(400, "开始时间不能晚于结束时间")
    result = await asyncio.to_thread(
        request_history().metrics,
        since=start,
        until=end,
        model=model,
        account_id=account_id,
    )
    result.update(
        history_days=settings.history_days,
        history_enabled=settings.request_history,
        active=len(active_requests()),
    )
    return result


@router.get("/requests/options")
async def request_options() -> dict[str, object]:
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_key_items,
    )

    result = await asyncio.to_thread(request_history().options)
    service = runtime_state.account_service
    result["accounts"] = (
        [
            {"id": account.id, "name": account.email or account.name or account.id}
            for account in service.list_accounts()
        ]
        if service
        else []
    )
    result["key_names"] = {
        hashlib.sha256(str(item.get("key") or "").encode()).hexdigest()[:12]: str(
            item.get("name") or "API Key"
        )
        for item in get_configured_api_key_items()
    } | {"admin": "管理员"}
    return result


@router.get("/requests/{request_id}/payload")
async def request_payload(request_id: str) -> dict[str, object]:
    if not re.fullmatch(r"req_[A-Za-z0-9_-]{1,100}", request_id):
        raise HTTPException(400, "请求 ID 格式无效")
    record = await asyncio.to_thread(request_history().get, request_id)
    if record is None:
        raise HTTPException(404, "请求记录不存在或已过保留期")

    def read() -> dict[str, object]:
        target = get_dump_dir() / f"{request_id}.json"
        # stat 与 read 一并放入 try：_prune_dumps（每 60s）会在
        # is_file 通过后、读取前清掉文件，锁外的 stat 会抛 500。
        try:
            if not target.is_file():
                return {
                    "available": False,
                    "note": "此请求未采集正文，或转储已过保留期。可在日志与调试中为后续请求开启文件转储。",
                }
            # JSON escaping can expand body bytes sixfold, and legacy views
            # coexist with the verbatim client capture in the same file.
            if target.stat().st_size > settings.dump_max_bytes * 32 + 65536:
                return {
                    "available": False,
                    "note": "转储超过当前读取上限，请在服务器端查看。",
                }
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"available": False, "note": "转储文件暂时不可读取。"}
        client_request = (
            payload.pop("client_request", None) if isinstance(payload, dict) else None
        )
        safe_payload = _mask_json_value(payload)
        if (
            isinstance(safe_payload, dict)
            and isinstance(client_request, dict)
            and client_request.get("source") == "client"
        ):
            # Body is explicitly captured verbatim. Recursive redaction here
            # would corrupt tool schemas and user messages a second time.
            body = client_request.pop("body", None)
            safe_client = _mask_json_value(client_request)
            if isinstance(safe_client, dict):
                safe_client["body"] = body
                safe_payload["client_request"] = safe_client
        return {
            "available": True,
            "payload": safe_payload,
            "note": (
                "客户端请求正文按接收原文保存，未经协议转换或正文脱敏；请求头及查询串中的凭据已打码。流式响应不保存正文。"
                if client_request is not None
                else "旧版正文已脱敏并重新序列化，不是逐字原文；后续请求开启文件转储后可保留原文。"
            ),
        }

    return await asyncio.to_thread(read)


@router.get("/requests/{request_id}/upstream")
async def request_upstream_exchange(
    request_id: str, download: bool = Query(False)
) -> Response:
    """上游交换快照（URL/脱敏头/wire 体/响应样本与响应头）。

    成功与失败请求均可能存在快照。重试场景下同一 request_id 会同时
    留下失败现场（``<id>.upstream.json``）与最终成功快照
    （``<id>.upstream.ok.json``）：按请求记录自身的成败选择优先级，
    展示「最后一次尝试」的形态。download=1 时以 attachment 形式
    返回 JSON 文件（文件名 ``<request_id>.upstream.json``），用于
    离线排查与共享。
    """
    if not re.fullmatch(r"req_[A-Za-z0-9_-]{1,100}", request_id):
        raise HTTPException(400, "请求 ID 格式无效")
    record = await asyncio.to_thread(request_history().get, request_id)
    if record is None:
        raise HTTPException(404, "请求记录不存在或已过保留期")

    # 最终成功的请求优先展示成功快照；最终失败的优先错误现场。
    final_status = record.get("status_code")
    record_success = isinstance(final_status, int) and 200 <= final_status < 300
    order = (
        (".upstream.ok.json", ".upstream.json")
        if record_success
        else (".upstream.json", ".upstream.ok.json")
    )

    def read() -> dict[str, object]:
        dump_dir = get_dump_dir()
        for suffix in order:
            target = dump_dir / f"{request_id}{suffix}"
            try:
                if not target.is_file():
                    continue
                if target.stat().st_size > settings.dump_max_bytes * 4 + 65536:
                    return {
                        "available": False,
                        "note": "快照超过当前读取上限，请在服务器端查看。",
                    }
                return json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        return {
            "available": False,
            "note": "此请求无上游交换快照（成功快照超保留条数被淘汰，或辅助 RPC 成功形态不采集）。",
        }

    snapshot = await asyncio.to_thread(read)
    content = json.dumps(snapshot, ensure_ascii=False, indent=1)
    if download:
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{request_id}.upstream.json"'
                )
            },
        )
    return Response(content=content, media_type="application/json")


@router.get("/requests/{request_id}")
async def get_request(request_id: str) -> dict[str, object]:
    for record in active_requests():
        if record["request_id"] == request_id:
            safe = _mask_json_value(record)
            return safe if isinstance(safe, dict) else {}
    record = await asyncio.to_thread(request_history().get, request_id)
    if record is None:
        raise HTTPException(404, "请求记录不存在或已过保留期")
    return record


@router.delete("/requests")
async def clear_requests() -> dict[str, object]:
    return {"removed": await asyncio.to_thread(request_history().clear)}


@router.get("/logs/history")
async def historical_logs(
    level: str = "INFO",
    search: str = Query("", max_length=256),
    request_id: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    category: str = "model",
    model: str = "",
    account_id: str = "",
    order: str = "desc",
) -> dict[str, object]:
    return await asyncio.to_thread(
        read_log_history,
        level=level.upper(),
        search=search,
        request_id=request_id,
        page=page,
        page_size=page_size,
        category=category,
        model=model,
        account_id=account_id,
        order=order,
    )


@router.get("/proxies")
async def list_proxies() -> dict[str, object]:
    from aistudio_api.infrastructure.proxy_bridge import bridge_status

    service = runtime_state.account_service
    accounts = service.list_accounts() if service else []
    status = bridge_status()
    return {
        "global_proxy": redact_text(settings.proxy_url or ""),
        "signer": redact_text(str(get_local_waa_server().get("url") or "")),
        "bridge": {
            "available": status["available"],
            "binary": status["binary"],
            "idle_minutes": status["idle_minutes"],
            "instances": status["instances"],
        },
        "accounts": [
            {
                "account_id": a.id,
                "name": a.name,
                "enabled": a.enabled,
                "source": "account"
                if a.proxy_url
                else "global"
                if settings.proxy_url
                else "direct",
                "proxy": redact_text(a.proxy_url or settings.proxy_url or ""),
            }
            for a in accounts
        ],
    }


class ProxyTestRequest(BaseModel):
    account_id: str | None = None
    proxy_url: str | None = None


@router.post("/proxies/test")
async def test_proxy(req: ProxyTestRequest) -> dict[str, object]:
    from aistudio_api.infrastructure.account.cookie_parser import normalize_proxy_url
    from aistudio_api.infrastructure.proxy_bridge import (
        ProxyBridgeError,
        resolve_proxy_url_async,
    )

    proxy = settings.proxy_url
    if req.account_id:
        service = runtime_state.account_service
        account = service.get_account(req.account_id) if service else None
        if account is None:
            raise HTTPException(404, "账号不存在")
        proxy = account.proxy_url or settings.proxy_url
    elif req.proxy_url is not None:
        proxy = normalize_proxy_url(req.proxy_url) if req.proxy_url.strip() else None
        if req.proxy_url.strip() and not proxy:
            raise HTTPException(400, "代理地址无效")

    # turn/turns/sstp 需先拉起桥接子进程转换为本地 SOCKS5；冷启动失败
    # 直接把可操作的中文指引返回给面板（fail-closed，不静默改直连测试）。
    try:
        resolved_proxy = await resolve_proxy_url_async(proxy)
    except ProxyBridgeError as exc:
        raise HTTPException(400, str(exc)) from exc

    async def check(name: str, url: str) -> dict[str, object]:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                proxy=resolved_proxy, trust_env=False, timeout=8, follow_redirects=False
            ) as client:
                response = await client.get(url)
                result: dict[str, object] = {
                    "name": name,
                    "ok": response.status_code < 400,
                    "status_code": response.status_code,
                }
                if name == "出口 IP" and response.is_success:
                    value = response.json().get("ip", "")
                    result["ip"] = str(value)[:64]
        except Exception as exc:
            result = {"name": name, "ok": False, "error": redact_text(str(exc))[:300]}
        result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
        return result

    checks = await asyncio.gather(
        check("Google 网络", "https://www.google.com/generate_204"),
        check("AI Studio", "https://aistudio.google.com/"),
        check("出口 IP", "https://api.ipify.org?format=json"),
    )
    return {
        "proxy": redact_text(proxy or ""),
        "effective_proxy": redact_text(resolved_proxy or ""),
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


@router.get("/ready")
async def readiness() -> JSONResponse:
    service, rotator = runtime_state.account_service, runtime_state.rotator
    accounts = service.list_accounts() if service else []
    available = sum(
        1
        for a in accounts
        if a.enabled and rotator and rotator.is_account_available(a.id)
    )
    # 本地签名引擎（127.0.0.1:8081）是唯一签名方式：探活直连本机
    # 回环，不走出站代理（代理只会把回环请求发往代理侧的 localhost）。
    url = str(get_local_waa_server().get("url") or "")
    signer_ok = False
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get(url.rstrip("/") + "/health")
            signer_ok = response.is_success
    except httpx.HTTPError:
        pass
    ready = bool(runtime_state.client and available and signer_ok)
    return JSONResponse(
        {
            "ready": ready,
            "available_accounts": available,
            "signer_ok": signer_ok,
            "history_enabled": settings.request_history,
            "history_days": settings.history_days,
            "max_concurrent_requests": settings.max_concurrent_requests,
            "request_timeout": settings.request_timeout,
        },
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )
