"""Request history, proxy diagnostics and readiness for operators."""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.model_defaults import get_active_waa_server
from aistudio_api.infrastructure.request_history import request_history
from aistudio_api.infrastructure.utils.log_files import read_log_history
from aistudio_api.infrastructure.utils.redaction import redact_text

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
) -> dict[str, object]:
    return await asyncio.to_thread(
        request_history().query,
        page_size=10000,
        since=since,
        until=until,
        model=model,
        result=result,
        account_id=account_id,
        key_id=key_id,
        search=search,
    )


@router.get("/requests/{request_id}")
async def get_request(request_id: str) -> dict[str, object]:
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
) -> dict[str, object]:
    return await asyncio.to_thread(
        read_log_history,
        level=level.upper(),
        search=search,
        request_id=request_id,
        page=page,
        page_size=page_size,
    )


@router.get("/proxies")
async def list_proxies() -> dict[str, object]:
    service = runtime_state.account_service
    accounts = service.list_accounts() if service else []
    return {
        "global_proxy": redact_text(settings.proxy_url or ""),
        "signer": redact_text(str(get_active_waa_server().get("url") or "")),
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


class GlobalProxyRequest(BaseModel):
    proxy_url: str = ""


@router.put("/proxies/global")
async def update_global_proxy(req: GlobalProxyRequest) -> dict[str, object]:
    import yaml

    from aistudio_api.infrastructure.account.cookie_parser import normalize_proxy_url
    from aistudio_api.infrastructure.gateway.model_defaults import (
        _resolve_config_path,
        config_file_write_lock,
        invalidate_config_cache,
    )
    from aistudio_api.infrastructure.utils.common import atomic_write_text

    proxy = normalize_proxy_url(req.proxy_url) if req.proxy_url.strip() else None
    if req.proxy_url.strip() and proxy is None:
        raise HTTPException(400, "代理地址无效")

    def save() -> None:
        with config_file_write_lock():
            path = _resolve_config_path(None)
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            network = config.get("network")
            if not isinstance(network, dict):
                network = {}
            config["network"] = {**network, "proxy_url": proxy or ""}
            atomic_write_text(
                path,
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                private=True,
            )
            invalidate_config_cache()

    await asyncio.to_thread(save)
    settings.proxy_url = proxy
    if runtime_state.client:
        await runtime_state.client.session.set_default_proxy(proxy)
    return {
        "ok": True,
        "global_proxy": redact_text(proxy or ""),
        "note": "当前进程立即生效；重启时环境变量代理配置优先。",
    }


@router.post("/proxies/test")
async def test_proxy(req: ProxyTestRequest) -> dict[str, object]:
    from aistudio_api.infrastructure.account.cookie_parser import normalize_proxy_url

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

    async def check(name: str, url: str) -> dict[str, object]:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                proxy=proxy, trust_env=False, timeout=8, follow_redirects=False
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
    url = str(get_active_waa_server().get("url") or settings.proof_server_url)
    signer_ok = False
    try:
        from aistudio_api.infrastructure.waa.daemon import is_local_signer_url

        async with httpx.AsyncClient(
            timeout=3,
            proxy=None if is_local_signer_url(url) else settings.proxy_url,
            trust_env=False,
        ) as client:
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
