"""FastAPI application entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from aistudio_api.application.api_service_gemini import classify_gemini_error_payload
from aistudio_api.application.compat_service import build_compat_error_response
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_configured_logging_settings,
)
from aistudio_api.infrastructure.utils.logger import (
    get_logger,
    setup_logging,
)

from .dependencies import require_api_key, require_web_auth
from .request_middleware import RequestMiddleware
from .routes_accounts import router as accounts_router
from .routes_compat import router as compat_router
from .routes_gemini import router as gemini_router
from .routes_models import router as models_router
from .routes_operations import router as operations_router
from .routes_system import (
    protected_router as system_protected_router,
    public_router as system_public_router,
)
from .state import runtime_state

if TYPE_CHECKING:
    from aistudio_api.infrastructure.account.account_store import AccountMeta

_log_cfg = get_configured_logging_settings()
setup_logging(str(_log_cfg.get("level", "INFO")))
logger = get_logger("server")


async def _pooled_probe_client(
    pool: dict[str, httpx.AsyncClient], proxy_url: str | None
) -> httpx.AsyncClient:
    """按代理分池获取探活客户端（并发安全，全局代理回退含在内）。

    turn/sstp 冷启动解析是真实 await 让出点：原「检查-构造-赋值」写法
    在并发首调下会重复建池，多余实例脱离 pool 无人关闭（连接泄漏给
    GC）；改为先解析、``setdefault`` 仲裁入池，竞争中后到者立即自关。
    账号代理为空时回退全局默认（fail-closed：解析失败上抛由调用方处理）。
    """
    key = proxy_url or ""
    existing = pool.get(key)
    if existing is not None:
        return existing
    from aistudio_api.infrastructure.proxy_bridge import resolve_effective_proxy_async

    resolved = await resolve_effective_proxy_async(proxy_url)
    client = httpx.AsyncClient(timeout=8.0, proxy=resolved, follow_redirects=True)
    winner = pool.setdefault(key, client)
    if winner is not client:
        await client.aclose()  # 并发竞争中后到者自关，唯一实例入池
    return winner


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aistudio_api.infrastructure.waa.daemon import (
        ensure_signer_ready,
        stop_signer_daemon,
    )

    ensure_auth_configured()
    from aistudio_api.infrastructure.utils.log_files import (
        start_file_logging,
        stop_file_logging,
    )

    start_file_logging()
    if settings.request_history:
        from aistudio_api.infrastructure.request_history import request_history

        await asyncio.to_thread(request_history().recover_interrupted)
    # 启动时自动检查并拉起本地 WAA 签名服务（若配置为本地节点）。
    # 内含阻塞 I/O，下放线程池避免卡住启动期事件循环。
    await asyncio.to_thread(ensure_signer_ready)

    from aistudio_api.application.account_rotator import init_rotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.infrastructure.account.account_store import AccountStore

    client = AIStudioClient()
    runtime_state.client = client

    # 初始化账号管理服务
    account_store = AccountStore()
    account_service = AccountService(account_store)
    runtime_state.account_service = account_service

    # 环境变量 Cookie 注入（AISTUDIO_COOKIES / _<N> / _FILE，支持 代理+Cookie / Cookie / Cookie+代理）
    from aistudio_api.application.env_account_injector import (
        inject_env_accounts,
        probe_env_sub_accounts,
    )

    env_report = inject_env_accounts(account_store)
    if env_report.total:
        logger.info(
            "环境变量账号注入完成: 共 %d 条（新建=%d, 更新=%d, 清理=%d）",
            env_report.total,
            len(env_report.created_ids),
            len(env_report.updated_ids),
            len(env_report.pruned_ids),
        )

    # 初始化黏性账号调度器
    rotator = init_rotator(account_store)
    runtime_state.rotator = rotator

    active_auth_path = account_store.get_active_auth_path()
    if active_auth_path is not None:
        await client.switch_auth(str(active_auth_path))
    logger.info(
        "服务启动完成: port=%d, 账号数=%d",
        settings.port,
        len(account_store.list_accounts()),
    )
    logger.debug(
        "账号列表: %s",
        [
            f"{a.id}(u/{a.auth_user},tier={a.tier},enabled={a.enabled})"
            for a in account_store.list_accounts()
        ],
    )

    async def _auto_refresh_tiers() -> None:

        from aistudio_api.infrastructure.account.cookie_parser import (
            load_cookies_from_auth_file,
            probe_account_benefit_tier,
            probe_account_email,
        )

        now_ts = time.time()
        accounts_to_check = [
            a
            for a in account_store.list_accounts()
            if not a.email
            or (a.tier_checked_at is None)
            or (now_ts - (a.tier_checked_at or 0) > 86400 * 3)
        ]
        if not accounts_to_check:
            return

        logger.info("校验账号邮箱与订阅等级: 待检测=%d", len(accounts_to_check))
        upgraded_count = 0
        checked_count = 0
        sem = asyncio.Semaphore(4)

        # 按代理分池的探活客户端：绑定独立代理的账号各走各的出口。
        # turn/turns/sstp 先解析为本地回环 SOCKS5（桥接子进程冷启动放
        # 线程池），解析失败抛错 → _check_one 捕获后跳过该账号（fail-
        # closed，绝不静默改直连）。池化经 _pooled_probe_client：并发
        # 首调只建一个客户端（历史上检查-构造-赋值间隙的 await 会泄漏）。
        probe_clients: dict[str, httpx.AsyncClient] = {}

        async def _get_probe_client(proxy_url: str | None) -> httpx.AsyncClient:
            return await _pooled_probe_client(probe_clients, proxy_url)

        try:

            async def _check_one(a: AccountMeta) -> None:
                nonlocal checked_count, upgraded_count
                apath = account_store.get_auth_path(a.id)
                if not apath:
                    return
                async with sem:
                    try:
                        cdict = load_cookies_from_auth_file(apath)
                        if not cdict:
                            return
                        checked_count += 1
                        probe_client = await _get_probe_client(
                            getattr(a, "proxy_url", None)
                        )
                        if not a.email:
                            em = await probe_account_email(
                                cdict,
                                auth_user=a.auth_user or "0",
                                client=probe_client,
                            )
                            if em:
                                account_store.update_account_email(a.id, em)
                        t = await probe_account_benefit_tier(
                            cdict,
                            auth_user=a.auth_user or "0",
                            client=probe_client,
                        )
                        resolved_tier = t or "Free"
                        account_store.update_account_tier(
                            a.id, resolved_tier, checked_at=now_ts
                        )
                        if resolved_tier != "Free" and a.tier == "Free":
                            upgraded_count += 1
                            logger.info(
                                "账号订阅等级更新: id=%s (u/%s) -> %s",
                                a.id,
                                a.auth_user,
                                resolved_tier,
                            )
                    except Exception as e:
                        logger.debug("账号 %s 订阅检测失败: %s", a.id, e)

            await asyncio.gather(*(_check_one(a) for a in accounts_to_check))
        finally:
            for probe_client in probe_clients.values():
                with suppress(Exception):
                    await probe_client.aclose()

        logger.info(
            "账号校验完成: 已检测=%d, 付费升级=%d",
            checked_count,
            upgraded_count,
        )

    async def _cookie_rotation_loop() -> None:
        """后台定时轮换账号滚动 Cookie (__Secure-1PSIDTS / SIDCC)，间隔 7 分钟。"""
        import httpx

        from aistudio_api.infrastructure.account.cookie_parser import (
            compute_session_anchor,
            load_cookies_from_auth_file,
            probe_aistudio_session,
        )

        await asyncio.sleep(5.0)
        while True:
            try:
                now_ts = time.time()
                seen_anchors: set[str] = set()
                accounts = account_store.list_accounts()
                # 按代理分池的轮换客户端：账号绑定独立代理时走各自出口
                rot_clients: dict[str, httpx.AsyncClient] = {}
                try:
                    for acc in accounts:
                        if not acc.enabled:
                            continue
                        st = rotator.get_account_stats(acc.id)
                        if st and st.session_expired:
                            continue
                        last_rot = acc.last_rotated_at or 0.0
                        if last_rot and (now_ts - last_rot < 420.0):
                            continue
                        apath = account_store.get_auth_path(acc.id)
                        if not apath:
                            continue
                        cdict = load_cookies_from_auth_file(apath)
                        if not cdict:
                            continue
                        anchor = compute_session_anchor(cdict)
                        if anchor:
                            if anchor in seen_anchors:
                                continue
                            seen_anchors.add(anchor)

                        acc_proxy = getattr(acc, "proxy_url", None)
                        client_key = acc_proxy or ""
                        rot_client = rot_clients.get(client_key)
                        if rot_client is None:
                            from aistudio_api.infrastructure.proxy_bridge import (
                                resolve_effective_proxy_async,
                            )

                            rot_client = httpx.AsyncClient(
                                timeout=15.0,
                                proxy=await resolve_effective_proxy_async(acc_proxy),
                                follow_redirects=False,
                            )
                            rot_clients[client_key] = rot_client

                        before_ts = cdict.get("__Secure-1PSIDTS", "")
                        rot_res = await account_service.rotate_account_cookies(
                            acc.id, client.session, client=rot_client
                        )
                        status_code = rot_res.get("status", 0)
                        if status_code == 401:
                            alive, _ = await probe_aistudio_session(
                                cdict, client=rot_client
                            )
                            if not alive:
                                rotator.record_session_expired(acc.id)
                            continue
                        new_cdict = rot_res.get("cookies")
                        after_ts = (
                            new_cdict.get("__Secure-1PSIDTS", "")
                            if isinstance(new_cdict, dict)
                            else ""
                        )
                        if (
                            status_code == 200
                            and before_ts
                            and after_ts
                            and before_ts == after_ts
                        ):
                            alive, _ = await probe_aistudio_session(
                                new_cdict if isinstance(new_cdict, dict) else cdict,
                                client=rot_client,
                            )
                            if not alive:
                                rotator.record_session_expired(acc.id)
                finally:
                    for rot_client in rot_clients.values():
                        with suppress(Exception):
                            await rot_client.aclose()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("后台 Cookie 轮换巡检异常: %s", e)
            await asyncio.sleep(60.0)

    tier_task = asyncio.create_task(_auto_refresh_tiers())
    rotate_task = asyncio.create_task(_cookie_rotation_loop())

    # 新注入的环境变量账号：后台探活多登子账号 (u/1..u/N)
    env_probe_task = None
    if env_report.created_ids:

        async def _env_subaccount_probe() -> None:
            try:
                imported = await probe_env_sub_accounts(
                    account_store, list(env_report.created_ids)
                )
                if imported:
                    logger.info("环境变量子账号探活导入完成: 新增 %d 个", imported)
            except Exception as e:
                logger.debug("环境变量子账号探活失败: %s", e)

        env_probe_task = asyncio.create_task(_env_subaccount_probe())

    yield
    logger.info("正在关闭服务")
    bg_tasks = [t for t in (tier_task, rotate_task, env_probe_task) if t]
    for bg_task in bg_tasks:
        if not bg_task.done():
            bg_task.cancel()
    # 等待取消真正落地，避免残留 pending task 在事件循环关闭时告警
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    if client:
        try:
            await client.close()
        except Exception as e:
            logger.debug("关闭客户端异常: %s", e)
    await asyncio.to_thread(stop_signer_daemon)
    # 终止全部 TURN/SSTP 桥接子进程（fail-closed 组件随主进程同生命周期）
    from aistudio_api.infrastructure.proxy_bridge import shutdown_bridge_processes

    await asyncio.to_thread(shutdown_bridge_processes)
    # 统计与调度状态都改成了防抖写入（record 后 2s 才落盘）；若进程在最后一次
    # 记账后 2s 内退出，最后一次增量会丢失。关闭时显式刷新一次内存态。
    try:
        if runtime_state._save_task is not None:
            await runtime_state._save_task
        await asyncio.to_thread(runtime_state.save_stats)
        pending_rotator = runtime_state.rotator
        if pending_rotator is not None:
            await asyncio.to_thread(pending_rotator.save_state)
    except Exception as e:
        logger.debug("关闭时刷新统计/调度状态失败: %s", e)
    await asyncio.to_thread(stop_file_logging)
    runtime_state.client = None
    runtime_state.account_service = None
    runtime_state.rotator = None


app = FastAPI(title="AI Studio API", lifespan=lifespan)
app.include_router(system_public_router)
app.include_router(system_protected_router, dependencies=[Depends(require_web_auth)])
app.include_router(
    accounts_router, prefix="/api", dependencies=[Depends(require_web_auth)]
)
app.include_router(accounts_router, dependencies=[Depends(require_web_auth)])
app.include_router(gemini_router, dependencies=[Depends(require_api_key)])
app.include_router(models_router, dependencies=[Depends(require_api_key)])
app.include_router(compat_router, dependencies=[Depends(require_api_key)])
app.include_router(operations_router, dependencies=[Depends(require_web_auth)])


def _log_http_exception(request: Request, code: int, msg: str) -> None:
    """按状态码分级记录 HTTP 异常（兼容与非兼容分支共用）。"""
    if code >= 500:
        logger.error("HTTP %d 异常: path=%s, message=%s", code, request.url.path, msg)
    elif code in (401, 403, 429):
        logger.warning("HTTP %d: path=%s, message=%s", code, request.url.path, msg)
    else:
        logger.info("HTTP %d: path=%s, message=%s", code, request.url.path, msg)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    code, msg, status_str = classify_gemini_error_payload(exc)
    from aistudio_api.infrastructure.request_context import record_result

    record_result("rate_limited" if code == 429 else "error", error=msg)
    _log_http_exception(_request, code, msg)
    # OpenAI / Claude 兼容路径按各自协议格式下发错误体
    compat_response = build_compat_error_response(_request, exc)
    if compat_response is not None:
        return compat_response
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    from aistudio_api.infrastructure.request_context import record_result

    record_result("error", error="Invalid request parameters")
    # OpenAI / Claude 兼容路径按各自协议格式下发校验错误体
    compat_response = build_compat_error_response(_request, exc)
    if compat_response is not None:
        logger.warning("请求参数校验失败: path=%s, error=%s", _request.url.path, exc)
        return compat_response
    code, msg, status_str = 400, str(exc), "INVALID_ARGUMENT"
    logger.warning("请求参数校验失败: path=%s, error=%s", _request.url.path, exc)
    return JSONResponse(
        status_code=code,
        content={
            "error": {
                "code": code,
                "message": msg,
                "status": status_str,
            }
        },
    )


# 挂载前端静态资源与 SPA 路由支持
static_dir = Path(__file__).resolve().parents[1] / "static"
index_html_path = static_dir / "index.html"

if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.get("/auth/check")
async def auth_check():
    """检查认证状态，用于前端判断是否需要登录。"""
    from aistudio_api.api.dependencies import web_auth_required

    return {"auth_enabled": web_auth_required()}


@app.middleware("http")
async def spa_navigation_middleware(request: Request, call_next):
    """统一处理前端 SPA 单页应用导航：浏览器页面直达/刷新响应 index.html，API 请求直通 JSON。"""
    accept = request.headers.get("accept", "")
    is_html_nav = (
        request.method == "GET"
        and "text/html" in accept
        and "application/json" not in accept
    )
    is_api = request.url.path.startswith(
        (
            "/api",
            "/v1beta",
            "/v1",
            "/auth",
            "/assets",
            "/static",
            "/health",
            "/stats",
            "/rotation",
            "/config",
            "/api-keys",
        )
    )
    is_accounts_spa = request.url.path in ("/accounts", "/accounts/")
    if is_html_nav and (is_accounts_spa or not is_api) and index_html_path.is_file():
        response = FileResponse(index_html_path)
        response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        return response

    response = await call_next(request)

    if request.url.path.startswith(("/accounts", "/api/accounts")) or is_html_nav:
        existing_vary = response.headers.get("Vary")
        if existing_vary:
            if "Accept" not in [v.strip() for v in existing_vary.split(",")]:
                response.headers["Vary"] = f"{existing_vary}, Accept"
        else:
            response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )

    return response


@app.get("/")
async def root_index():
    """根路径兜底响应。"""
    if index_html_path.is_file():
        response = FileResponse(index_html_path)
        response.headers["Vary"] = "Accept"
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        return response
    return JSONResponse({"message": "AI Studio API Web UI"})


_cors_allowlist_cache: tuple[str, frozenset[str]] | None = None


def _allowed_cors_origins() -> frozenset[str]:
    """解析 CORS 白名单；按原始环境变量字符串缓存，配置变更时自动失效。"""
    global _cors_allowlist_cache
    raw = os.getenv("AISTUDIO_CORS_ALLOWED_ORIGINS", "")
    cached = _cors_allowlist_cache
    if cached is not None and cached[0] == raw:
        return cached[1]
    parsed = frozenset(origin.strip() for origin in raw.split(",") if origin.strip())
    _cors_allowlist_cache = (raw, parsed)
    return parsed


@app.middleware("http")
async def compat_cors_middleware(request: Request, call_next):
    """OpenAI / Claude 兼容路径的 CORS 支持（浏览器直连客户端）。

    仅作用于 ``/v1/`` 前缀：OPTIONS 预检直接短路返回 204，
    其余带 Origin 的响应回显跨域头。注册于最后使其成为最外层中间件，
    保证错误响应同样携带 CORS 头。
    """
    if not request.url.path.startswith("/v1/"):
        return await call_next(request)

    # 安全：默认不反射任意 Origin，也不无条件附带 Credentials。
    # 需要浏览器跨域的部署可通过 AISTUDIO_CORS_ALLOWED_ORIGINS
    # （逗号分隔的精确 Origin 列表）显式放行；留空时 /v1/ 仅允许同源。
    allowed_origins = _allowed_cors_origins()

    origin = request.headers.get("origin")
    allowed_origin = origin if (origin and origin in allowed_origins) else None

    if request.method == "OPTIONS":
        # 未被允许的跨域预检不给任何 Allow-* 头，直接短路，
        # 避免把任意站点的预检请求当作可信来源。
        preflight = Response(status_code=204)
        if allowed_origin:
            preflight.headers["Access-Control-Allow-Origin"] = allowed_origin
            preflight.headers["Access-Control-Allow-Credentials"] = "true"
            preflight.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            preflight.headers["Access-Control-Allow-Headers"] = (
                request.headers.get("access-control-request-headers")
                or "Authorization, Content-Type, x-api-key, anthropic-version, "
                "x-goog-api-key, x-anti-truncation"
            )
            preflight.headers["Access-Control-Max-Age"] = "86400"
        return preflight

    response = await call_next(request)
    if allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        # 合并而非覆盖：保留上游已写入的 Vary（如 Accept），
        # 避免命中同一 URL 的不同 Accept/Origin 变体被中间层缓存串包
        existing_vary = response.headers.get("Vary")
        vary_values = (
            [v.strip() for v in existing_vary.split(",") if v.strip()]
            if existing_vary
            else []
        )
        if "Origin" not in vary_values:
            vary_values.append("Origin")
        response.headers["Vary"] = ", ".join(vary_values)
    return response


app.add_middleware(RequestMiddleware)


def ensure_auth_configured() -> None:
    import secrets as _secrets

    from aistudio_api.api.dependencies import _all_configured_api_keys

    web_password_set = bool((settings.web_password or "").strip())
    configured_keys = settings.api_keys or _all_configured_api_keys()
    allow_no_auth = settings.allow_no_auth

    if not web_password_set and not configured_keys:
        if allow_no_auth:
            logger.warning(
                "未配置任何鉴权（AISTUDIO_API_KEYS / AISTUDIO_WEB_PASSWORD / "
                "config.yaml api_keys）且已显式设置 AISTUDIO_ALLOW_NO_AUTH=1："
                "服务将以完全开放模式运行，请仅在可信内网使用"
            )
        else:
            # 安全默认（2.8）：首次启动到运维手动配置之间的窗口期内，
            # 管理面板不再裸奔——自动生成一次性随机管理密码并打印到日志。
            generated = _secrets.token_urlsafe(12)
            settings.web_password = generated
            logger.warning("=" * 64)
            logger.warning(
                "未检测到任何凭据，已自动生成随机管理密码保护管理面板（每次重启都会重新生成）："
            )
            logger.warning("  管理密码: %s", generated)
            logger.warning(
                "  如需固定密码请设置环境变量 AISTUDIO_WEB_PASSWORD；"
                "如需在可信内网完全开放请设置 AISTUDIO_ALLOW_NO_AUTH=1"
            )
            logger.warning("=" * 64)
    elif configured_keys and not web_password_set:
        # 扩展告警（2.1）：覆盖“配置了 API Key 但未配置管理密码”的常见场景
        logger.warning(
            "已配置 API Key 但未设置管理密码（AISTUDIO_WEB_PASSWORD）："
            "管理控制台将允许任意有效 API Key 登录，且 Key 明文在 /api-keys、"
            "/config 中脱敏展示。建议设置独立管理密码以隔离面板与 API 权限。"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AI Studio API Server")
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args(argv)
    ensure_auth_configured()
    import uvicorn

    logger.info("启动服务: 监听端口=%d", args.port)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=args.port,
        log_config=None,
        # 优雅关停：SIGTERM 后先停止收新请求，给在途请求（尤其 SSE 流）
        # timeout_graceful_shutdown 秒排空；超时才强断连接，避免容器
        # 编排（Docker/K8s 默认 30s 后 SIGKILL）前在途流挂住整个关停。
        timeout_graceful_shutdown=25,
    )
