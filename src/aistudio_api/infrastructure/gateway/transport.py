"""Direct HTTP streaming and non-streaming transport layer.

Provides high-performance, browserless HTTP communication with Google AI Studio
using connection pooling and chunk streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import ssl
import time
from collections.abc import AsyncGenerator

import httpx

from aistudio_api.config import settings
from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.account.cookie_parser import (
    apply_set_cookies,
    calculate_sapisid_hash,
    filter_minimal_cookies,
    get_tier_header_value,
    load_cookies_from_auth_file,
)
from aistudio_api.infrastructure.proxy_bridge import (
    resolve_proxy_url_async,
    resolve_proxy_url_cached,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("transport")

__all__ = [
    "HTTPStreamTransport",
    "strip_to_aistudio_fingerprint",
]

# httpx 的 HTTP/2 支持是可选依赖（h2 包）。若环境未安装 h2 而仍传
# http2=True，AsyncClient 在创建时即抛 ImportError（"Using http2=True, but
# the 'h2' package is not installed"），所有上游请求全部 500——曾在
# Render 干净环境复现（requirements 只装了 httpx[socks]）。这里做一次性
# 探测：缺失时打一条 WARNING（会进面板日志环形缓冲）并降级 HTTP/1.1，
# 保证服务可用而不是整站瘫。安装 `pip install 'httpx[http2]'` 后重启
# 即恢复 HTTP/2 多路复用。
# 注意：变量名不能用全大写（如 _H2_AVAILABLE），pyright 会将其视为
# 常量并拒绝 global 重定义（reportConstantRedefinition）。
_h2_available: bool | None = None


def _http2_enabled() -> bool:
    """h2 包可用时启用 HTTP/2；缺失时告警一次并降级为 HTTP/1.1。"""
    global _h2_available
    if _h2_available is None:
        try:
            import h2  # noqa: F401  # pyright: ignore[reportUnusedImport]  # 仅为探测可选依赖是否存在
        except ImportError:
            _h2_available = False
            logger.warning(
                "未安装 h2 包（httpx 的 http2 extra），上游连接已降级为 "
                "HTTP/1.1；执行 `pip install 'httpx[http2]'` 并重启可启用 "
                "HTTP/2 多路复用"
            )
        else:
            _h2_available = True
    return _h2_available


# httpx.AsyncClient 未显式传 verify 时会为每个实例调用
# ssl.create_default_context()，在 Windows 上枚举系统证书库实测单次
# 0.7~1.4 秒（同步阻塞事件循环；带 proxy 时更慢）。进程级共享同一个
# SSLContext 可把这个开销降到亚毫秒级，语义与默认行为一致（仍校验证书，
# 仍加载系统 CA）。证书更新在进程重启后生效。
_ssl_context: ssl.SSLContext | None = None


def _shared_ssl_context() -> ssl.SSLContext:
    """返回进程级共享的 SSL 上下文，避免反复枚举系统证书库。"""
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context()
    return _ssl_context


# 空 Cookie 的短 TTL 负缓存：账号 Cookie 加载失败/为空时，避免每个请求
# 都在事件循环里同步读一遍 auth.json + 查询 AccountStore（读盘虽小，
# 但在“账号损坏”这个持续状态下是每请求一次的纯浪费）。5 秒足够短，
# 面板修复账号后几乎立即恢复。
_empty_cookie_cache: dict[str, tuple[float, bool]] = {}
_EMPTY_COOKIE_TTL = 5.0


def _get_auth_cookies(auth_file: str | None = None) -> dict[str, str] | None:
    """Read credentials from the bound auth_file or active account."""
    if auth_file:
        cookies = load_cookies_from_auth_file(auth_file)
        if cookies:
            return cookies
    cache_key = auth_file or ""
    now = time.monotonic()
    cached = _empty_cookie_cache.get(cache_key)
    if cached is not None and now - cached[0] < _EMPTY_COOKIE_TTL:
        if cached[1]:
            return None  # 负缓存命中：已知无可用 Cookie
    else:
        cached = None
    with contextlib.suppress(Exception):
        from aistudio_api.infrastructure.account.account_store import AccountStore

        store = AccountStore()
        active = store.get_active_account()
        if not active:
            if cached is None:
                _empty_cookie_cache[cache_key] = (now, True)
            return None
        auth_path = store.get_auth_path_optional(active.id, require_exists=True)
        if auth_path:
            cookies = load_cookies_from_auth_file(auth_path)
            if cookies:
                _empty_cookie_cache.pop(cache_key, None)
                return cookies
    if cached is None:
        _empty_cookie_cache[cache_key] = (now, True)
    return None


def _get_active_account_tier() -> str:
    """Read active account's subscription tier."""
    with contextlib.suppress(Exception):
        from aistudio_api.infrastructure.account.account_store import AccountStore

        store = AccountStore()
        active = store.get_active_account()
        if active and getattr(active, "tier", None):
            return str(active.tier)
    return "Free"


def _persist_set_cookies(
    set_cookies: list[str],
    auth_file: str | None = None,
    cookies: dict[str, str] | None = None,
) -> None:
    """[仅限线程池调用] 将上游 Set-Cookie 合并回内存 Cookie 与 AccountStore。

    内部含逐账号读盘、合并、原子写回等同步 I/O，账号越多阻塞越重；
    必须经由 asyncio.to_thread 调用，禁止在事件循环线程内直接执行（3.4）。

    线程安全：``cookies`` 可能是事件循环线程正在迭代的共享 SessionContext
    字典（filter_minimal_cookies / calculate_sapisid_hash 等）。禁止在工作
    线程逐键就地变更（并发插入会触发 "dictionary changed size during
    iteration"，殃及无辜并发请求）；先在快照上合并，再一次性原子写回
    （单次 dict.update 为 C 级调用，读侧也先做快照，两者叠加后无撕裂窗口）。
    """
    if cookies is not None:
        snapshot = dict(cookies)
        target_map = snapshot
    else:
        target_map = _get_auth_cookies(auth_file) or {}
    if not target_map:
        return
    updated = apply_set_cookies(target_map, set_cookies)
    if not updated:
        return
    if cookies is not None:
        # 单次原子写回共享 dict：其他线程要么看到旧值要么看到完整新值
        cookies.update({name: target_map[name] for name in updated})
    logger.debug("捕获上游 Set-Cookie 更新: %s", updated)
    try:
        from aistudio_api.infrastructure.account.account_store import AccountStore

        AccountStore().update_account_cookies(
            auth_file=auth_file,
            new_cookies=target_map,
        )
    except Exception as e:
        logger.warning("持久化 Set-Cookie 失败: %s", e)


def _mask_proxy_url(proxy_url: str | None) -> str:
    """脱敏代理地址中的账号密码，避免日志泄漏凭据。"""
    if not proxy_url:
        return "(直连)"
    return re.sub(r"//[^@/]+@", "//***@", str(proxy_url))


# ---------------------------------------------------------------------------
# 上游请求头白名单（指纹剥离核心）
# ---------------------------------------------------------------------------
# 项目对所有上游（alkalimakersuite-pa.clients6.google.com）请求**重建**
# 而非转发请求头：只有真实 aistudio.google.com 浏览器会发送的头才允许
# 出现（2026-11 HAR 抓包 + 消融矩阵对齐，见 docs/CAPTURE_ALIGNMENT_2026-11.md §2）。
# 调用方（兼容层路由 / 签名服务动态头 / 账号探活等）传入的任何其它头
# ——包括客户端应用自带头（Claude Code / one-api / curl 的 UA、
# x-stainless-*、x-app-* 等指纹）——一律在网关边界剥除，绝不透传上游，
# 防止客户端指纹混入逆向指纹集构成可识别特征。
#
# 允许集（值由白名单外的逻辑重建，这里只管名字）：
#   - 路由/浏览器基础头：Content-Type / Origin / Referer / User-Agent /
#     X-User-Agent / Accept / Accept-Language / Sec-Fetch-Site /
#     Sec-Fetch-Mode / Sec-Fetch-Dest（CORS fetch 三元组 + Accept 系）
#   - Google RPC 头：X-Goog-Api-Key / X-Goog-AuthUser /
#     X-Goog-Ext-519733851-bin / X-AIStudio-G1-Tier / X-AIStudio-Visit-Id
#   - 凭据头（本函数重建）：Authorization / Cookie
#   - WAA 签名服务动态头：X-Goog-Api-Key / X-Goog-AuthUser /
#     X-Goog-Ext-519733851-bin / X-AIStudio-Visit-Id / User-Agent /
#     X-User-Agent（waa-signer 返回的动态集即这些，全部落在允许集内）
#   - 抗截断等内部机制不会经由 headers 通道携带业务信息
_UPSTREAM_ALLOWED_HEADERS: frozenset[str] = frozenset(
    {
        # 基础 HTTP / 浏览器头
        "content-type",
        "origin",
        "referer",
        "user-agent",
        "x-user-agent",
        "accept",
        "accept-language",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        # Google AI Studio RPC 头
        "x-goog-api-key",
        "x-goog-authuser",
        "x-goog-ext-519733851-bin",
        "x-aistudio-g1-tier",
        "x-aistudio-visit-id",
        # 凭据头（值在本函数内重建）
        "authorization",
        "cookie",
    }
)

# 调用方必须显式设置、禁止透传推断值的头（host/content-length 由
# httpx 依据目标 URL 与请求体自动生成；accept-encoding/连接管理头由
# httpx 传输层控制——均不属于浏览器指纹集，透传只会引入客户端痕迹）
_UPSTREAM_REBUILT_HEADERS: frozenset[str] = frozenset(
    {"host", "content-length", "accept-encoding"}
)


def strip_to_aistudio_fingerprint(
    headers: dict[str, str],
) -> dict[str, str]:
    """剥离一切非 AI Studio 逆向指纹头，仅保留白名单内的头名。

    黑名单式剔除（旧实现）无法防御未知客户端头的混入：任何调用方新增
    透传逻辑都会把客户端应用的头（SDK 指纹、代理头、自定义头）带上游。
    白名单取交集后，出站请求头集恒定为 ``_UPSTREAM_ALLOWED_HEADERS`` 的
    子集，与真实浏览器指纹对齐。

    返回键统一折叠为小写：WAA 签名服务（Go）返回 ``X-Goog-Authuser``，
    Python 侧字面量是 ``X-Goog-AuthUser``，若原样保留两种大小写变体，
    调用方 ``dict.update()`` 会新增而非覆盖，上游将收到重复且值冲突的
    头（真实浏览器只发单个 canonical 头，双变体本身构成非浏览器指纹）。
    同名变体后写入者胜出（调用方 update 语义下动态签名值优先于静态模板）。
    """
    stripped: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in _UPSTREAM_ALLOWED_HEADERS:
            stripped[lowered] = str(value)
    return stripped


# 小写头名 → canonical 形态（与真实 Chrome 浏览器发出的头名大小写一致）。
# 白名单键集的 canonical 重建在 ``_ensure_authorization_header`` 末尾执行，
# 保证出站指纹与浏览器一致而非全小写的非标准形态。
_CANONICAL_HEADER_NAMES: dict[str, str] = {
    "content-type": "Content-Type",
    "origin": "Origin",
    "referer": "Referer",
    "user-agent": "User-Agent",
    "x-user-agent": "X-User-Agent",
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "sec-fetch-site": "Sec-Fetch-Site",
    "sec-fetch-mode": "Sec-Fetch-Mode",
    "sec-fetch-dest": "Sec-Fetch-Dest",
    "x-goog-api-key": "X-Goog-Api-Key",
    "x-goog-authuser": "X-Goog-AuthUser",
    "x-goog-ext-519733851-bin": "X-Goog-Ext-519733851-bin",
    "x-aistudio-g1-tier": "X-AIStudio-G1-Tier",
    "x-aistudio-visit-id": "X-AIStudio-Visit-Id",
    "authorization": "Authorization",
    "cookie": "Cookie",
}


def _ensure_authorization_header(
    headers: dict[str, str],
    auth_user: str = "0",
    tier: str | None = None,
    auth_file: str | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, str]:
    """Ensure SAPISIDHASH Authorization, X-Goog-AuthUser, and X-AIStudio-G1-Tier are set.

    纯同步函数（L4）：无任何 await，声明为 async 只会让每个请求多一次
    协程创建与调度的纯开销。

    处理顺序：先白名单剥离并折叠为小写键（仅保留 AI Studio 逆向指纹头），
    再重建必需的凭据头，最后按 canonical 形态重建键名。凭据头
    （Authorization/Cookie）优先使用本地账号 Cookie 计算/组装——本地
    Cookie 经 Set-Cookie 回写持续轮换（__Secure-1PSIDTS/SIDCC），而签名
    服务 proof 缓存命中时回传的是缓存时刻的旧 Cookie 快照，用它压制
    本地新值会在滚动窗口内引发连环 401。``incoming_auth``/incoming
    Cookie 仅在本地无凭据时兜底。
    """
    # strip 后键恒为小写（大小写变体已折叠），后续用小写键操作即可
    clean_headers = strip_to_aistudio_fingerprint(headers)

    incoming_auth = clean_headers.get("authorization") or ""
    incoming_auth = incoming_auth if isinstance(incoming_auth, str) else ""
    # host/content-length/accept-encoding 属于 httpx 传输层管辖，不得透传。
    # 白名单本就不含这三个头名，此处保留为纵深防御（防止未来白名单扩张）。
    clean_headers.pop("host", None)
    clean_headers.pop("content-length", None)
    clean_headers.pop("accept-encoding", None)

    clean_headers["x-goog-authuser"] = str(auth_user or "0")
    effective_tier = tier if tier is not None else _get_active_account_tier()
    tier_header = get_tier_header_value(effective_tier)
    if tier_header:
        clean_headers["x-aistudio-g1-tier"] = tier_header

    raw_cookie_dict = cookies or _get_auth_cookies(auth_file)
    cookie_dict = filter_minimal_cookies(raw_cookie_dict) if raw_cookie_dict else None

    if cookie_dict:
        # 本地凭据权威：总是用最新 Cookie 计算/组装凭据头（覆盖签名服务
        # 缓存的旧值）；本地无凭据时才回退 incoming（聊胜于无）。
        fresh_auth = calculate_sapisid_hash(cookie_dict)
        if fresh_auth:
            clean_headers["authorization"] = fresh_auth
        clean_headers["cookie"] = "; ".join(
            f"{k}={v}" for k, v in cookie_dict.items() if v
        )
    if "authorization" not in clean_headers and incoming_auth:
        clean_headers["authorization"] = incoming_auth

    # canonical 重建：与真实浏览器头名大小写对齐（httpx 按传入键原样发送）
    return {
        _CANONICAL_HEADER_NAMES.get(key, key): value
        for key, value in clean_headers.items()
    }


class HTTPStreamTransport:
    """Direct HTTP streaming and non-streaming transport layer."""

    def __init__(self, proxy_url: str | None = None, timeout: float = 120.0) -> None:
        self.proxy_url = proxy_url or settings.proxy_url
        self.timeout = timeout
        # 后台 Set-Cookie 持久化任务：流式请求不再在首块前同步等待
        # 落盘（持锁 stat+fsync 5-15ms，并发排队放大首字延迟数百 ms）
        self._pending_cookie_tasks: set[asyncio.Task[None]] = set()
        # 已退役代理池的延迟关闭任务（retire_stale_clients 创建）
        self._retired_close_tasks: set[asyncio.Task[None]] = set()
        # 已退役待回收的客户端（close 兜底用；延迟任务正常完成时自清）
        self._retired_clients: set[httpx.AsyncClient] = set()
        # 按代理地址分池的客户端缓存：key 为代理 URL（空串表示直连）
        self._clients: dict[str, httpx.AsyncClient] = {}
        # 首次并发调用会同时走到建池分支，若不互斥会创建多个客户端并泄漏未关闭的连接池；
        # 锁按事件循环惰性创建，避免跨 loop 复用同一个 Lock 对象。
        self._client_lock: asyncio.Lock | None = None
        self._client_lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_client_lock(self) -> asyncio.Lock:
        """返回与当前事件循环绑定的建池互斥锁。"""
        loop = asyncio.get_running_loop()
        if self._client_lock is None or self._client_lock_loop is not loop:
            self._client_lock = asyncio.Lock()
            self._client_lock_loop = loop
        return self._client_lock

    def _schedule_cookie_persist(
        self,
        set_cookies: list[str],
        auth_file: str | None,
        cookies: dict[str, str] | None,
    ) -> None:
        """把 Set-Cookie 持久化调度为后台任务（失败仅记日志，不影响请求流）。"""
        task = asyncio.create_task(
            asyncio.to_thread(_persist_set_cookies, set_cookies, auth_file, cookies)
        )
        self._pending_cookie_tasks.add(task)
        task.add_done_callback(self._pending_cookie_tasks.discard)

    async def _flush_cookie_persist(self) -> None:
        """关停前排空后台 Cookie 持久化任务，避免滚动凭据丢失。"""
        if self._pending_cookie_tasks:
            await asyncio.gather(*self._pending_cookie_tasks, return_exceptions=True)

    async def close(self) -> None:
        # 与 _get_client 的建池分支互斥：否则并发建池插入 self._clients
        # 会让这里的迭代抛 "dictionary changed size during iteration"，
        # 且 close 之后新建的客户端将永远不会被关闭（连接泄漏）。
        await self._flush_cookie_persist()
        async with self._get_client_lock():
            for client in self._clients.values():
                if not client.is_closed:
                    await client.aclose()
            self._clients.clear()
        pending = list(self._retired_close_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            self._retired_close_tasks.clear()
        # 兜底：任务若在启动帧之前就被取消（retire 后立即 close），
        # CancelledError 不会经过 except 分支，aclose 从未执行；
        # 此时 retired 客户端已不在 _clients 中，须显式回收。
        for client in self._retired_clients:
            if not client.is_closed:
                with contextlib.suppress(Exception):
                    await client.aclose()
        self._retired_clients.clear()

    async def retire_stale_clients(self, keep_proxy: str | None) -> None:
        """退役除 keep_proxy 外的所有客户端池（面板切换全局代理时调用）。

        照抄 WaaSigner.reconfigure 的「retired + 延迟关闭」模式：立即从
        池中摘除、延迟 aclose，让并发在途请求持有的引用能安全跑完，
        而非滞留到进程退出（反复切代理会累积多个满配连接池）。

        保留键用 resolve_proxy_url_cached（无副作用）：桥接代理（turn/sstp）
        的客户端池以「已解析的本地 SOCKS5」为键，而冷启动解析会在此处
        触发子进程拉起，退役路径绝不能有这种副作用。
        """
        keep_key = resolve_proxy_url_cached(keep_proxy) or ""
        retired: list[httpx.AsyncClient] = []
        async with self._get_client_lock():
            for key in [k for k in self._clients if k != keep_key]:
                retired.append(self._clients.pop(key))
        if not retired:
            return

        self._retired_clients.update(retired)

        async def _delayed_close() -> None:
            try:
                # 35s 宽限：足以让在途流式请求（含 SSE 长流）排空
                await asyncio.sleep(35.0)
            except asyncio.CancelledError:
                # 进程关停：宽限期作废，立即回收（cancel 时 sleep 抛出，
                # finally 统一关闭；重新 raise 让任务以取消态结束）
                try:
                    for client in retired:
                        if not client.is_closed:
                            await client.aclose()
                finally:
                    raise
            for client in retired:
                if not client.is_closed:
                    await client.aclose()
            self._retired_clients.difference_update(retired)

        task = asyncio.create_task(_delayed_close())
        self._retired_close_tasks.add(task)
        task.add_done_callback(self._retired_close_tasks.discard)

    async def _get_client(self, proxy_url: str | None = None) -> httpx.AsyncClient:
        """获取（或创建）绑定指定出口代理的客户端；proxy_url=None 时使用全局默认代理。

        turn / turns / sstp 代理在此处解析为本地回环 SOCKS5（桥接子进程
        由 proxy_bridge 托管，首次冷启动放线程池，不阻塞事件循环）；
        客户端池以解析后的地址为键，同一桥接节点复用同一连接池。
        """
        effective_raw = self.proxy_url if proxy_url is None else (proxy_url or None)
        effective = (
            await resolve_proxy_url_async(effective_raw) if effective_raw else None
        )
        key = effective or ""
        client = self._clients.get(key)
        if client is not None and not client.is_closed:
            return client
        async with self._get_client_lock():
            client = self._clients.get(key)
            if client is not None and not client.is_closed:
                return client
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                proxy=effective,
                trust_env=False,
                http2=_http2_enabled(),
                verify=_shared_ssl_context(),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
            self._clients[key] = client
            return client

    async def send_request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: str,
        auth_user: str = "0",
        tier: str | None = None,
        auth_file: str | None = None,
        cookies: dict[str, str] | None = None,
        proxy_url: str | None = None,
    ) -> tuple[int, bytes]:
        """Execute non-streaming HTTP request directly."""
        t0 = time.perf_counter()
        clean_headers = _ensure_authorization_header(
            headers,
            auth_user=auth_user,
            tier=tier,
            auth_file=auth_file,
            cookies=cookies,
        )
        logger.debug(
            "发送请求: url=%s, body_len=%d, u/%s",
            url,
            len(body),
            auth_user,
        )

        effective_proxy = self.proxy_url if proxy_url is None else proxy_url
        client = await self._get_client(effective_proxy)
        try:
            resp = await client.post(
                url, headers=clean_headers, content=body.encode("utf-8")
            )
            # Set-Cookie 回写下放线程池（3.4）：逐账号读盘/写盘的同步 I/O
            # 若留在事件循环内会拖慢所有并发请求的首字延迟。
            set_cookies = resp.headers.get_list("set-cookie")
            if set_cookies:
                await asyncio.to_thread(
                    _persist_set_cookies, set_cookies, auth_file, cookies
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.debug(
                "请求完成: status=%d, 耗时=%.1fms, 响应长度=%d",
                resp.status_code,
                elapsed_ms,
                len(resp.content),
            )
            return resp.status_code, resp.content
        except Exception as exc:
            err_detail = (
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            )
            logger.error(
                "HTTP 请求失败: url=%s, proxy=%s, error=%s",
                url,
                _mask_proxy_url(effective_proxy),
                err_detail,
            )
            raise TransportError(f"HTTP request failed: {err_detail}") from exc

    async def send_streaming_request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: str,
        auth_user: str = "0",
        tier: str | None = None,
        auth_file: str | None = None,
        cookies: dict[str, str] | None = None,
        proxy_url: str | None = None,
    ) -> AsyncGenerator[tuple[str, object], None]:
        """Stream HTTP response chunks directly."""
        clean_headers = _ensure_authorization_header(
            headers,
            auth_user=auth_user,
            tier=tier,
            auth_file=auth_file,
            cookies=cookies,
        )
        logger.debug("建立流式请求: url=%s, u/%s", url, auth_user)

        effective_proxy = self.proxy_url if proxy_url is None else proxy_url
        client = await self._get_client(effective_proxy)

        resp = None
        try:
            req = client.build_request(
                "POST", url, headers=clean_headers, content=body.encode("utf-8")
            )
            resp = await client.send(req, stream=True)
            set_cookies = resp.headers.get_list("set-cookie")
            if set_cookies:
                # 后台落盘：Google 上游普遍带滚动 Set-Cookie，
                # 同步等待会把持锁磁盘 IO 排队进流式首字关键路径
                self._schedule_cookie_persist(set_cookies, auth_file, cookies)
            yield ("status", resp.status_code)

            if resp.status_code != 200:
                err_body = await resp.aread()
                yield ("chunk", err_body)
                return

            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield ("chunk", chunk)

        except Exception as exc:
            err_detail = (
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            )
            logger.warning(
                "流式传输异常: url=%s, proxy=%s, error=%s",
                url,
                _mask_proxy_url(effective_proxy),
                err_detail,
            )
            yield ("error", err_detail)
        finally:
            if resp is not None:
                await resp.aclose()
