"""A disconnected JSON keepalive must not strand the foreground HTTP/1.1 peer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from test_client_disconnect import _Client
from test_r26_preflight_lifecycle import _Peer

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import runtime_state
from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.request_context import active_requests

_URL = "http://fixture.invalid/rpc"
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("failure", ["headers", "first_whitespace"])
@pytest.mark.parametrize("protocol", ["chat", "responses", "claude", "gemini"])
async def test_failed_first_buffered_keepalive_releases_http_connection(
    monkeypatch, spec, failure, protocol
):
    reading, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Pending(_Peer):
        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            chunk = await super().read(max_bytes, timeout)
            if chunk == b"abc":
                reading.set()
                await release.wait()
            return chunk

        async def aclose(self) -> None:
            await super().aclose()
            closed.set()

    first = Pending([b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n", b"abc"])
    second = _Peer([_OK])
    backend = httpcore.AsyncMockBackend([])
    monkeypatch.setattr(backend, "connect_tcp", AsyncMock(side_effect=[first, second]))
    wire = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=1))
    monkeypatch.setattr(wire._pool, "_network_backend", backend)
    gateway = AIStudioClient()
    transport = gateway.session._transport
    transport._clients[""] = httpx.AsyncClient(
        transport=wire, trust_env=False, timeout=httpx.Timeout(1.0, pool=0.1)
    )
    prepared = PreparedRequest(
        url=_URL, headers={}, body="[]", ctx=SessionContext(proxy_url="")
    )
    prepare = AsyncMock(return_value=prepared)
    monkeypatch.setattr(gateway, "prepare_request", prepare)
    monkeypatch.setitem(app.dependency_overrides, get_client, lambda: gateway)
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(runtime_state, "account_service", None)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "nonstream_keepalive", True)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.02)
    downstream = _Client(protocol, stream=False, spec_version=spec)

    async def send(message):
        if (failure == "headers" and message["type"] == "http.response.start") or (
            failure == "first_whitespace" and message.get("body") == b" \n"
        ):
            # ASGI send may report closure before receive. Do not release the
            # upstream read: the disconnected request is responsible for it.
            await reading.wait()
            raise OSError("fixture downstream closed")
        await downstream.send(message)

    initial_tasks = asyncio.all_tasks()
    try:
        async with asyncio.timeout(3):
            with pytest.raises(OSError, match="fixture downstream closed"):
                await app(downstream.scope, downstream.receive, send)
            assert [msg["type"] for msg in downstream.messages] == (
                [] if failure == "headers" else ["http.response.start"]
            )
            assert not active_requests() and not transport._active_clients
            assert not wire._pool._requests
            prepare.assert_awaited_once()
            assert len(first.requests) == 1 and first.requests[0].startswith(b"POST ")
            assert not second.requests
            # Zero live requests does not guarantee a usable pool. A partially
            # cancelled HTTP/1.1 cleanup used to leave its only peer ACTIVE.
            result = await transport.send_request(
                url=_URL, headers={}, body="[]", cookies={}, tier="Free", proxy_url=""
            )
            assert result == (200, b"ok") and closed.is_set()
            assert len(second.requests) == 1 and second.requests[0].startswith(b"POST ")
            assert len(first.requests) == 1
        assert not wire._pool._requests and not transport._active_clients
        assert asyncio.all_tasks() <= initial_tasks, "request left work running"
    finally:
        release.set()
        await gateway.close()
