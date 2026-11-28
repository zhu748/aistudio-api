"""Exercise anonymous OPTIONS cleanup through real HTTPX/httpcore HTTP/1.1."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from test_client_disconnect import _Client

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import runtime_state
from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway import transport as transport_module
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport
from aistudio_api.infrastructure.request_context import active_requests

_URL = "http://fixture.invalid/rpc"
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


class _Peer(httpcore.AsyncMockStream):
    def __init__(self, reads: list[bytes]) -> None:
        super().__init__(reads)
        self.requests: list[bytes] = []

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if buffer.startswith((b"OPTIONS ", b"POST ")):
            self.requests.append(buffer)


def _wire(monkeypatch, transport: HTTPStreamTransport, *peers: _Peer):
    # Only the socket is fake. HTTPX owns the real one-connection pool, which
    # drives the H11 request/response streams, EOF close and connection reuse.
    wire = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=1))
    backend = httpcore.AsyncMockBackend([])
    monkeypatch.setattr(backend, "connect_tcp", AsyncMock(side_effect=peers))
    monkeypatch.setattr(wire._pool, "_network_backend", backend)
    monkeypatch.setattr(
        transport_module, "_is_browser_rpc_target", lambda url: url == _URL
    )
    transport._clients[""] = httpx.AsyncClient(
        transport=wire, trust_env=False, headers={"Authorization": "fixture"}
    )
    return wire._pool


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("phase", ["body", "eof_close"])
async def test_downstream_disconnect_during_preflight(monkeypatch, stream, phase):
    peer = _Peer(
        [
            (
                b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                b"Access-Control-Max-Age: 600\r\nContent-Length: 3\r\n\r\n"
            ),
            b"ack",
        ]
    )
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    read, close = peer.read, peer.aclose

    async def gated_read(max_bytes, timeout=None):
        data = await read(max_bytes, timeout)
        if phase == "body" and data == b"ack":
            entered.set()
            await release.wait()
        return data

    async def gated_close():
        if phase == "eof_close":
            entered.set()
            await release.wait()
        await close()
        closed.set()

    monkeypatch.setattr(peer, "read", gated_read)
    monkeypatch.setattr(peer, "aclose", gated_close)
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(runtime_state, "account_service", None)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "nonstream_keepalive", True)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 10.0)
    monkeypatch.setattr(settings, "request_timeout", 30.0)
    gateway = AIStudioClient()
    transport = gateway.session._transport
    pool = _wire(monkeypatch, transport, peer)
    prepared = PreparedRequest(
        url=_URL,
        headers={"X-AIStudio-Visit-Id": "fixture"},
        body="[]",
        ctx=SessionContext(proxy_url=""),
    )
    monkeypatch.setattr(gateway, "prepare_request", AsyncMock(return_value=prepared))
    monkeypatch.setitem(app.dependency_overrides, get_client, lambda: gateway)
    downstream = _Client("chat", stream=stream, spec_version="2.4")
    disconnected = asyncio.Event()

    async def receive():
        message = await downstream.receive()
        if message["type"] == "http.disconnect":
            # Let the ASGI watcher process this message and issue cancellation
            # before releasing socket I/O. Some HTTP stacks shield close
            # internally; waiting for the socket's task to be cancelled would
            # itself deadlock an otherwise valid streamed disconnect.
            asyncio.get_running_loop().call_soon(disconnected.set)
        return message

    try:
        async with asyncio.timeout(3), asyncio.TaskGroup() as group:
            group.create_task(app(downstream.scope, receive, downstream.send))
            try:
                await entered.wait()
                assert len(pool._requests) == 1 and transport._active_clients
                downstream.disconnect.set()
                await disconnected.wait()
            finally:
                release.set()
        assert closed.is_set() and not pool._requests and not pool.connections
        assert not active_requests() and not transport._active_clients
        assert not transport._preflight_locks and not transport._preflight_cache
        assert [request.split(b" ", 1)[0] for request in peer.requests] == [b"OPTIONS"]
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    ("status", "extra", "expected_age"),
    [
        (b"200 OK", b"", 600),
        (b"503 Unavailable", b"", 5),
        (b"200 OK", b"Content-Encoding: gzip\r\nConnection: close\r\n", 5),
    ],
)
async def test_preflight_drains_before_caching_and_allows_post(
    monkeypatch, status, extra, expected_age
):
    # The last case fails while decoding the OPTIONS body despite the 2xx
    # status and advertised age. Only the advisory fallback may be cached.
    preflight = b"HTTP/1.1 " + status + b"\r\n" + extra
    preflight += b"Access-Control-Max-Age: 600\r\nContent-Length: 3\r\n\r\n"
    if extra:
        peers = [_Peer([preflight, b"ack"]), _Peer([_OK, _OK])]
    else:
        peers = [_Peer([preflight, b"ack", _OK, _OK])]
    transport = HTTPStreamTransport()
    pool = _wire(monkeypatch, transport, *peers)

    async def post():
        return await transport.send_request(
            url=_URL, headers={}, body="{}", cookies={}, tier="Free", proxy_url=""
        )

    try:
        start = time.monotonic()
        assert await post() == (200, b"ok")
        (expiry,) = transport._preflight_cache.values()
        assert start + expected_age <= expiry <= time.monotonic() + expected_age
        assert await post() == (200, b"ok")
        headers = [header for peer in peers for header in peer.requests]
        assert [h.split(b" ", 1)[0] for h in headers] == [b"OPTIONS", b"POST", b"POST"]
        assert b"\r\nauthorization:" not in headers[0].lower()
        assert b"\r\ncookie:" not in headers[0].lower()
        assert len(pool.connections) == 1 and pool.connections[0].is_idle()
        assert not pool._requests and not transport._active_clients
    finally:
        await transport.close()
