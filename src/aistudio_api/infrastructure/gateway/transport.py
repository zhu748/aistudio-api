"""Direct HTTP streaming and non-streaming transport layer.

Provides high-performance, browserless HTTP communication with Google AI Studio
using connection pooling and chunk streaming.
"""

from __future__ import annotations

import re
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
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("transport")

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


def _get_auth_cookies(auth_file: str | None = None) -> dict[str, str] | None:
    """Read credentials from the bound auth_file or active account."""
    if auth_file:
        cookies = load_cookies_from_auth_file(auth_file)
        if cookies:
            return cookies
    try:
        from aistudio_api.infrastructure.account.account_store import AccountStore

        store = AccountStore()
        active = store.get_active_account()
        if not active:
            return None
        auth_path = store.get_auth_path_optional(active.id, require_exists=True)
        if auth_path:
            cookies = load_cookies_from_auth_file(auth_path)
            if cookies:
                return cookies
    except Exception:
        pass
    return None


def _get_active_account_tier() -> str:
    """Read active account's subscription tier."""
    try:
        from aistudio_api.infrastructure.account.account_store import AccountStore

        store = AccountStore()
        active = store.get_active_account()
        if active and getattr(active, "tier", None):
            return str(active.tier)
    except Exception:
        pass
    return "Free"


def _sync_response_cookies(
    resp: httpx.Response,
    auth_file: str | None = None,
    cookies: dict[str, str] | None = None,
) -> None:
    """捕获上游响应中的 Set-Cookie 并同步回内存与 AccountStore。"""
    set_cookies = resp.headers.get_list("set-cookie")
    if not set_cookies:
        return
    target_map = (
        cookies if cookies is not None else (_get_auth_cookies(auth_file) or {})
    )
    if not target_map:
        return
    updated = apply_set_cookies(target_map, set_cookies)
    if not updated:
        return
    logger.debug("捕获上游 Set-Cookie 更新: %s", updated)
    try:
        from aistudio_api.infrastructure.account.account_store import AccountStore

        AccountStore().update_account_cookies(
            auth_file=auth_file,
            new_cookies=target_map,
        )
    except Exception as e:
        logger.debug("持久化 Set-Cookie 失败: %s", e)


def _mask_proxy_url(proxy_url: str | None) -> str:
    """脱敏代理地址中的账号密码，避免日志泄漏凭据。"""
    if not proxy_url:
        return "(直连)"
    return re.sub(r"//[^@/]+@", "//***@", str(proxy_url))


async def _ensure_authorization_header(
    headers: dict[str, str],
    auth_user: str = "0",
    tier: str | None = None,
    auth_file: str | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, str]:
    """Ensure SAPISIDHASH Authorization, X-Goog-AuthUser, and X-AIStudio-G1-Tier are set."""
    incoming_auth = ""
    for k, v in headers.items():
        if k.lower() == "authorization" and v:
            incoming_auth = str(v)
            break

    clean_headers = {
        k: v
        for k, v in headers.items()
        if k.lower()
        not in (
            "host",
            "content-length",
            "authorization",
            "x-goog-authuser",
            "x-aistudio-g1-tier",
        )
    }
    clean_headers["X-Goog-AuthUser"] = str(auth_user or "0")
    effective_tier = tier if tier is not None else _get_active_account_tier()
    tier_header = get_tier_header_value(effective_tier)
    if tier_header:
        clean_headers["X-AIStudio-G1-Tier"] = tier_header

    raw_cookie_dict = cookies or _get_auth_cookies(auth_file)
    cookie_dict = filter_minimal_cookies(raw_cookie_dict) if raw_cookie_dict else None

    if incoming_auth:
        clean_headers["Authorization"] = incoming_auth
    elif cookie_dict:
        fresh_auth = calculate_sapisid_hash(cookie_dict)
        if fresh_auth:
            clean_headers["Authorization"] = fresh_auth
    if "Cookie" not in clean_headers and cookie_dict:
        clean_headers["Cookie"] = "; ".join(
            f"{k}={v}" for k, v in cookie_dict.items() if v
        )

    return clean_headers


class HTTPStreamTransport:
    """Direct HTTP streaming and non-streaming transport layer."""

    def __init__(self, proxy_url: str | None = None, timeout: float = 120.0) -> None:
        self.proxy_url = proxy_url or settings.proxy_url
        self.timeout = timeout
        # 按代理地址分池的客户端缓存：key 为代理 URL（空串表示直连）
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def close(self) -> None:
        for client in self._clients.values():
            if not client.is_closed:
                await client.aclose()
        self._clients.clear()

    async def _get_client(self, proxy_url: str | None = None) -> httpx.AsyncClient:
        """获取（或创建）绑定指定出口代理的客户端；proxy_url=None 时使用全局默认代理。"""
        effective = self.proxy_url if proxy_url is None else (proxy_url or None)
        key = effective or ""
        client = self._clients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                proxy=effective,
                http2=_http2_enabled(),
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
        clean_headers = await _ensure_authorization_header(
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

        client = await self._get_client(proxy_url)
        try:
            resp = await client.post(
                url, headers=clean_headers, content=body.encode("utf-8")
            )
            _sync_response_cookies(resp, auth_file=auth_file, cookies=cookies)
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
                _mask_proxy_url(proxy_url),
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
        clean_headers = await _ensure_authorization_header(
            headers,
            auth_user=auth_user,
            tier=tier,
            auth_file=auth_file,
            cookies=cookies,
        )
        logger.debug("建立流式请求: url=%s, u/%s", url, auth_user)

        client = await self._get_client(proxy_url)

        resp = None
        try:
            req = client.build_request(
                "POST", url, headers=clean_headers, content=body.encode("utf-8")
            )
            resp = await client.send(req, stream=True)
            _sync_response_cookies(resp, auth_file=auth_file, cookies=cookies)
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
                _mask_proxy_url(proxy_url),
                err_detail,
            )
            yield ("error", err_detail)
        finally:
            if resp is not None:
                await resp.aclose()
