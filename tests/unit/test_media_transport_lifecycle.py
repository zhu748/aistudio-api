"""Public media DNS work obeys request timeouts and transport shutdown."""

import asyncio
import socket
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from aistudio_api.infrastructure import safe_media as module


async def test_dns_cannot_create_a_pool_after_transport_close(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(*args):
        entered.set()
        await release.wait()
        return "8.8.8.8"

    monkeypatch.setattr(module, "resolve_public_address", resolve)
    pool = Mock()
    pool.handle_async_request = AsyncMock(return_value=httpx.Response(200))
    pool.aclose = AsyncMock()
    factory = Mock(return_value=pool)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    transport = module.PublicMediaTransport()
    pending = asyncio.create_task(
        transport.handle_async_request(
            httpx.Request("GET", "https://media.example/image")
        )
    )
    await entered.wait()
    await transport.aclose()
    release.set()
    try:
        with pytest.raises(RuntimeError, match="closed"):
            await pending
        factory.assert_not_called()
    finally:
        await transport.aclose()


async def test_dns_timeout_uses_httpx_connect_timeout(monkeypatch):
    async def resolve(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "resolve_public_address", resolve)
    transport = module.PublicMediaTransport()
    request = httpx.Request(
        "GET", "https://media.example/image", extensions={"timeout": {"connect": 0.01}}
    )
    try:
        with pytest.raises(httpx.ConnectTimeout):
            await asyncio.wait_for(transport.handle_async_request(request), 0.5)
    finally:
        await transport.aclose()


async def test_dns_failure_uses_httpx_connection_error(monkeypatch):
    monkeypatch.setattr(
        module,
        "resolve_public_address",
        AsyncMock(side_effect=socket.gaierror("DNS failed")),
    )
    transport = module.PublicMediaTransport()
    try:
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(
                httpx.Request("GET", "https://media.example/image")
            )
    finally:
        await transport.aclose()
