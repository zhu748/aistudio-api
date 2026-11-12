"""Gateway session management for authentication and request signing."""

from __future__ import annotations

import inspect
import itertools
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.infrastructure.account.cookie_parser import (
    ROTATING_SESSION_COOKIE_NAMES,
    build_makersuite_headers,
    get_auth_user_from_meta,
    get_tier_from_meta,
    load_account_meta,
    load_cookies_from_auth_file,
    normalize_benefit_tier,
    normalize_proxy_url,
    parse_raw_cookies,
)
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.request_context import (
    current_request,
    request_event,
    upstream_attempt,
)
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.redaction import redact_text
from aistudio_api.infrastructure.waa.signer import WaaSigner

log = get_logger("session")


@dataclass
class SessionContext:
    """Encapsulates active authentication context and metadata for AI Studio gateway."""

    auth_user: str = "0"
    auth_file: str | None = None
    tier: str = "Free"
    cookies: dict[str, str] = field(default_factory=dict)
    proxy_url: str | None = None
    context_id: int = field(default_factory=itertools.count(1).__next__)
    created_at: float = field(default_factory=time.time)

    def is_usable(self) -> bool:
        return bool(self.auth_file or self.cookies)


GENERATE_CONTENT_URL = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent"

DEFAULT_UPSTREAM_HEADERS: dict[str, str] = build_makersuite_headers()


class GatewaySession:
    """Manages authentication state, WAA request signing, and transport routing."""

    def __init__(self):
        self._auth_file: str | None = settings.auth_file or None
        self._auth_user: str | None = None
        self._tier: str | None = None
        self._active_context: SessionContext | None = None
        self._transport = HTTPStreamTransport(proxy_url=settings.proxy_url)
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_active_waa_server,
        )

        active_waa = get_active_waa_server()
        server_url = str(active_waa.get("url") or settings.proof_server_url)
        self._signer = WaaSigner(
            proof_server_url=server_url,
            proxy_url=settings.proxy_url,
        )

    @property
    def is_ready(self) -> bool:
        return self._active_context is not None and self._active_context.is_usable()

    @property
    def active_context(self) -> SessionContext | None:
        return self._active_context

    @property
    def last_signer_headers(self) -> dict[str, str]:
        """Get dynamic protocol headers returned by the underlying signer."""
        return self._signer.last_headers

    def set_ready_context(self, context: SessionContext) -> None:
        """Set active ready context (used by transitions and test fixtures)."""
        self._auth_file = context.auth_file
        self._auth_user = context.auth_user
        self._tier = context.tier
        self._active_context = context

    def get_current_auth_user(self) -> str:
        """Get auth_user index of active account."""
        if self._auth_user is not None:
            return self._auth_user
        if self._active_context is not None:
            return self._active_context.auth_user
        return get_auth_user_from_meta(self._auth_file)

    def get_current_tier(self) -> str:
        """Get benefit tier of active account."""
        if self._active_context is not None and self._active_context.tier:
            return self._active_context.tier
        if self._tier is not None:
            return self._tier
        return get_tier_from_meta(self._auth_file)

    async def ensure_ready(self) -> SessionContext:
        """Ensure session context is initialized and ready."""
        if self.is_ready and self._active_context is not None:
            return self._active_context
        return await self.transition_to_account(self._auth_file, force_refresh=False)

    async def switch_auth(self, auth_file: str | None) -> None:
        """Atomic account switch."""
        await self.transition_to_account(auth_file, force_refresh=True)

    async def set_default_proxy(self, proxy: str | None) -> None:
        self._transport.proxy_url = proxy
        await self._signer.reconfigure(proxy_url=proxy or "")

    async def transition_to_account(
        self, auth_file: str | None, force_refresh: bool = False
    ) -> SessionContext:
        """Execute instantaneous in-memory session transition."""
        if (
            not force_refresh
            and self.is_ready
            and self._active_context is not None
            and self._active_context.auth_file == auth_file
        ):
            return self._active_context

        # 一次读 meta.json 取出三个字段：原先分别调用三个 get_*_from_meta
        # 会把同一个文件解析三遍（实测约 0.28ms 纯浪费，且是同步 IO）。
        target_meta = load_account_meta(auth_file)
        target_auth_user = str(target_meta.get("auth_user") or "0")
        raw_tier = target_meta.get("tier")
        target_tier = normalize_benefit_tier(str(raw_tier or "Free"))
        raw_proxy = target_meta.get("proxy_url")
        target_proxy = normalize_proxy_url(str(raw_proxy)) if raw_proxy else None
        cookies = load_cookies_from_auth_file(auth_file)

        self._auth_file = auth_file
        self._auth_user = target_auth_user
        self._tier = target_tier

        new_context = SessionContext(
            auth_user=target_auth_user,
            auth_file=auth_file,
            tier=target_tier,
            cookies=cookies,
            proxy_url=target_proxy,
        )
        self._active_context = new_context
        log.info(
            "会话已切换: auth_file=%s, u/%s, tier=%s",
            auth_file,
            target_auth_user,
            target_tier,
        )
        return new_context

    async def generate_snapshot(
        self,
        contents: list[AistudioContent],
        ctx: SessionContext | None = None,
        model: str = "models/gemini-3.7-flash",
    ) -> str:
        """Generate fresh WAA proof token for request contents."""
        active_ctx = ctx or await self.ensure_ready()
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_active_waa_server,
        )

        active_waa = get_active_waa_server()
        target_url = str(active_waa.get("url") or "").rstrip("/")
        # 面板切换签名节点后的热生效（3.5）：走 WaaSigner.reconfigure 的
        # 原子替换路径。旧实现通过私有属性打洞就地改写 server_url 并关闭
        # 共享连接池，会击落并发在途的签名请求且存在双重进入竞态。
        if target_url and getattr(self._signer, "server_url", None) != target_url:
            reconfigure_fn = getattr(self._signer, "reconfigure", None)
            if callable(reconfigure_fn):
                res = reconfigure_fn(server_url=target_url)
                if inspect.isawaitable(res):
                    await res

        if not active_ctx.cookies and active_ctx.auth_file:
            active_ctx.cookies = load_cookies_from_auth_file(active_ctx.auth_file)
        cookies = active_ctx.cookies
        auth_user = active_ctx.auth_user or "0"
        tier = active_ctx.tier or "Free"

        observation = current_request.get()
        if observation is not None:
            observation.model = model
            observation.account_id = (
                Path(active_ctx.auth_file).parent.name if active_ctx.auth_file else ""
            )
            observation.auth_user = auth_user
            observation.proxy = redact_text(
                active_ctx.proxy_url or settings.proxy_url or ""
            )
            observation.signer = redact_text(target_url)

        request_event("signing", "正在生成请求签名")
        proof = await self._signer.generate_snapshot(
            contents=contents,
            cookies=cookies,
            auth_user=auth_user,
            tier=tier,
            model=model,
            upstream_proxy=active_ctx.proxy_url or settings.proxy_url,
        )
        self._sync_signer_cookies(active_ctx)
        request_event("signed", "请求签名完成")
        return proof

    def _sync_signer_cookies(self, active_ctx: SessionContext) -> None:
        """当 WAA 签名服务在内部刷新了滚动 Cookie 时，将其同步回 SessionContext 与 AccountStore。"""
        dyn_headers = getattr(self._signer, "last_headers", None)
        if not isinstance(dyn_headers, dict):
            return
        raw_cookie = dyn_headers.get("Cookie") or dyn_headers.get("cookie")
        if not raw_cookie or not isinstance(raw_cookie, str):
            return
        parsed = parse_raw_cookies(raw_cookie)
        if not parsed:
            return
        changed = False
        for k in ROTATING_SESSION_COOKIE_NAMES:
            val = parsed.get(k)
            if val and active_ctx.cookies.get(k) != val:
                active_ctx.cookies[k] = val
                changed = True
        if changed and active_ctx.auth_file:
            try:
                from aistudio_api.infrastructure.account.account_store import (
                    AccountStore,
                )

                AccountStore().update_account_cookies(
                    auth_file=active_ctx.auth_file,
                    new_cookies=active_ctx.cookies,
                )
            except Exception as e:
                log.debug("回写签名服务 Cookie 失败: %s", e)

    async def generate_snapshot_with_headers(
        self,
        contents: list[AistudioContent],
        ctx: SessionContext | None = None,
        model: str = "models/gemini-3.7-flash",
    ) -> tuple[str, dict[str, str]]:
        """Generate fresh WAA proof token and dynamic session headers."""
        proof = await self.generate_snapshot(
            contents=contents,
            ctx=ctx,
            model=model,
        )
        return proof, dict(self.last_signer_headers)

    async def send_request(
        self,
        *,
        body: str,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        ctx: object | None = None,
    ) -> tuple[int, bytes]:
        active_ctx = (
            ctx if isinstance(ctx, SessionContext) else await self.ensure_ready()
        )
        target_url = url or GENERATE_CONTENT_URL
        req_headers = dict(DEFAULT_UPSTREAM_HEADERS)
        req_headers["X-Goog-AuthUser"] = active_ctx.auth_user
        if headers:
            req_headers.update(headers)
        with upstream_attempt(
            active_ctx.auth_file,
            active_ctx.auth_user,
            active_ctx.proxy_url or settings.proxy_url or "",
        ) as attempt:
            result = await self._transport.send_request(
                url=target_url,
                headers=req_headers,
                body=body,
                auth_user=active_ctx.auth_user,
                tier=active_ctx.tier,
                auth_file=active_ctx.auth_file,
                cookies=active_ctx.cookies,
                proxy_url=active_ctx.proxy_url,
            )
            attempt["status_code"] = result[0]
            return result

    async def send_streaming_request(
        self,
        *,
        body: str,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        ctx: object | None = None,
    ) -> AsyncGenerator[tuple[str, object], None]:
        active_ctx = (
            ctx if isinstance(ctx, SessionContext) else await self.ensure_ready()
        )
        target_url = url or GENERATE_CONTENT_URL
        req_headers = dict(DEFAULT_UPSTREAM_HEADERS)
        req_headers["X-Goog-AuthUser"] = active_ctx.auth_user
        if headers:
            req_headers.update(headers)
        # aclosing：上层丢弃本生成器时确定性关闭 transport 生成器，
        # 释放上游 httpx 连接（async for 不保证关闭被迭代对象）。
        upstream = self._transport.send_streaming_request(
            url=target_url,
            headers=req_headers,
            body=body,
            auth_user=active_ctx.auth_user,
            tier=active_ctx.tier,
            auth_file=active_ctx.auth_file,
            cookies=active_ctx.cookies,
            proxy_url=active_ctx.proxy_url,
        )
        with upstream_attempt(
            active_ctx.auth_file,
            active_ctx.auth_user,
            active_ctx.proxy_url or settings.proxy_url or "",
        ) as attempt:
            try:
                async for event in upstream:
                    if event[0] == "status":
                        attempt["status_code"] = event[1]
                    elif event[0] == "error":
                        attempt["error"] = redact_text(str(event[1]))[:500]
                    yield event
            finally:
                await upstream.aclose()

    async def close(self) -> None:
        """Close transport, signer, and reset active context."""
        await self._transport.close()
        await self._signer.close()
        self._active_context = None


__all__ = [
    "DEFAULT_UPSTREAM_HEADERS",
    "GENERATE_CONTENT_URL",
    "GatewaySession",
    "SessionContext",
]
