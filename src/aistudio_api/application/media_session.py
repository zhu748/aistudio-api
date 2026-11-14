"""Request-scoped media connections, concurrency and aggregate size budget."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

from aistudio_api.config import settings
from aistudio_api.infrastructure.proxy_bridge import resolve_proxy_url_async
from aistudio_api.infrastructure.safe_media import PublicMediaTransport


@dataclass
class MediaSession:
    client: httpx.AsyncClient | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    total_bytes: int = 0
    downloads: int = 0

    def account_bytes(self, size: int) -> None:
        self.total_bytes += size
        if self.total_bytes > settings.max_request_bytes:
            raise ValueError("Total media exceeds configured request size limit")

    async def get_client(self) -> httpx.AsyncClient:
        if self.client is None:
            # 解析在锁外完成：turn/sstp 桥接冷启动是秒级耗时且含真实
            # await 让出点，夹在锁内会让并发媒体下载首图前全部排队
            # （同 transport._get_client / signer._get_client 的教训）。
            proxy = await resolve_proxy_url_async(settings.proxy_url)
            async with self.lock:
                if self.client is None:
                    # SSL 上下文 / h2 / 代理均由 PublicMediaTransport 内部
                    # 消费（每 origin 的 AsyncHTTPTransport 已用
                    # _shared_ssl_context）；客户端层再传 verify/http2 会被
                    # 自定义 transport 短路，属无效参数，不传。
                    self.client = httpx.AsyncClient(
                        timeout=15.0,
                        follow_redirects=True,
                        max_redirects=5,
                        transport=PublicMediaTransport(proxy=proxy),
                        trust_env=False,
                    )
                return self.client
        return self.client


current_media: ContextVar[MediaSession | None] = ContextVar(
    "aistudio_media", default=None
)


def has_remote_media(blocks: list[object]) -> bool:
    """Keep ordinary text/inline content on the synchronous conversion path."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        url = block.get("image_url")
        if isinstance(url, dict):
            url = url.get("url")
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "url":
            url = source.get("url")
        if isinstance(url, str) and url.strip().lower().startswith(
            ("http://", "https://")
        ):
            return True
    return False


@asynccontextmanager
async def media_session() -> AsyncIterator[None]:
    """One conversion owns and deterministically closes its download connections."""
    if current_media.get() is not None:
        yield
        return
    session = MediaSession()
    token = current_media.set(session)
    try:
        yield
    finally:
        current_media.reset(token)
        if session.client is not None:
            await session.client.aclose()
