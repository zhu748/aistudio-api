"""Shutdown cuts at the real outer Chat send and SQLite bookkeeping boundaries."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import anyio
import httpcore
import httpx
import pytest
import test_client_disconnect as downstream
import test_ordered_tool_routes as wire
from starlette.types import Message
from test_r22_chat_delivered_terminal import _frame
from test_r26_preflight_lifecycle import _Peer

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.request_context import active_requests
from aistudio_api.infrastructure.request_history import request_history

isolated_history = downstream.isolated_history
_URL = "http://fixture.invalid/rpc"
_DONE = b"data: [DONE]\n\n"


@pytest.fixture
async def chat_peer(isolated_history, monkeypatch):
    payload = wire.wire(wire.chunk([wire.text("A finite answer.")], 1))
    peer = _Peer(
        [
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\n\r\n",
            payload,
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
        ]
    )
    backend = httpcore.AsyncMockBackend([])
    connect = AsyncMock(side_effect=[peer])
    monkeypatch.setattr(backend, "connect_tcp", connect)
    h1 = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=1))
    monkeypatch.setattr(h1._pool, "_network_backend", backend)
    gateway = AIStudioClient()
    transport = gateway.session._transport
    transport._clients[""] = httpx.AsyncClient(transport=h1, trust_env=False)
    prepared = PreparedRequest(
        url=_URL, headers={}, body="[]", ctx=SessionContext(proxy_url="")
    )
    monkeypatch.setattr(gateway, "prepare_request", AsyncMock(return_value=prepared))
    monkeypatch.setitem(app.dependency_overrides, get_client, lambda: gateway)
    try:
        yield transport, h1._pool, peer, connect
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "cut", ["baseline", "before_done", "after_done", "watcher_join", "repeat_persist"]
)
async def test_owned_history_teardown_at_shutdown(chat_peer, cut):
    transport, pool, peer, connect = chat_peer
    client = downstream._Client("chat", stream=True, spec_version="2.4")
    client.scope["headers"].append((b"x-anti-truncation", b"0"))
    reached, release, watcher_drained = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    cancel_scope = anyio.CancelScope()
    cancelled = []
    writes_at_return = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            final = not message.get("more_body", False)
            if (cut == "before_done" and message.get("body") == _DONE) or (
                cut == "after_done" and final
            ):
                reached.set()  # This send has not been acknowledged.
                await release.wait()
            await client.send(message)
            if final and cut in ("baseline", "repeat_persist"):
                reached.set()
        else:
            await client.send(message)

    async def receive() -> Message:
        try:
            return await client.receive()
        except asyncio.CancelledError:
            if cut == "watcher_join":
                reached.set()  # The wire has closed and the owner is joining us.
                await release.wait()
                watcher_drained.set()
            raise

    history = request_history()
    assert history.query()["total"] == 0
    with closing(sqlite3.connect(history.path, isolation_level=None)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        # A competing writer holds the real initial insert. The trigger records
        # committed writes in order, including anything lost/duplicated at exit.
        db.executescript(
            "CREATE TABLE writes(result TEXT);"
            "CREATE TRIGGER track AFTER INSERT ON requests BEGIN "
            "INSERT INTO writes VALUES(new.result); END;"
            "BEGIN IMMEDIATE;"
        )

        async def run() -> None:
            with cancel_scope:
                try:
                    await app(client.scope, receive, send)
                except asyncio.CancelledError as exc:
                    cancelled.append(exc.args)
                    raise
                finally:
                    writes_at_return.extend(
                        db.execute("SELECT result FROM writes ORDER BY rowid")
                    )

        initial_tasks = asyncio.all_tasks()
        async with asyncio.timeout(5), asyncio.TaskGroup() as group:
            owner = group.create_task(run())
            try:
                await reached.wait()
                await anyio.wait_all_tasks_blocked()
                assert not pool._requests and not transport._active_clients
                assert len(peer.requests) == 1
                if cut in ("before_done", "after_done"):
                    cancel_scope.cancel()  # ASGI host level cancellation.
                elif cut in ("watcher_join", "repeat_persist"):
                    if cut == "repeat_persist":
                        assert [row["stage"] for row in active_requests()] == [
                            "finished"
                        ]
                    owner.cancel("shutdown")
                    if cut == "repeat_persist":
                        await anyio.wait_all_tasks_blocked()
                        owner.cancel("again")  # During the first cancellation's drain.
                await anyio.wait_all_tasks_blocked()
            finally:
                release.set()
                db.rollback()

    before_done = cut == "before_done"
    assert bool(cancelled) == (cut != "baseline")
    if cut in ("watcher_join", "repeat_persist"):
        assert cancelled == [("shutdown",)]
    assert watcher_drained.is_set() == (cut == "watcher_join")
    assert not client.receiving and not active_requests()
    assert not (asyncio.all_tasks() - initial_tasks)
    bodies = [m for m in client.messages if m["type"] == "http.response.body"]
    frames = [f[0] for m in bodies if (f := _frame(m.get("body", b"")))]
    assert frames == ["role", "payload", "finish", *([] if before_done else ["done"])]
    assert sum(not m.get("more_body", False) for m in bodies) == (
        cut not in ("before_done", "after_done")
    )
    result = "cancelled" if before_done else "success"
    assert writes_at_return == [("running",), (result,)]
    row = client.stored_result()
    assert (row["status_code"], row["result"], row["error"], row["finish_reason"]) == (
        200,
        result,
        "",
        "" if before_done else "STOP",
    )
    # No retries or hidden leases: one more request uses the very same H11 peer.
    assert await transport.send_request(
        url=_URL, headers={}, body="[]", cookies={}, tier="Free", proxy_url=""
    ) == (200, b"ok")
    assert len(peer.requests) == 2 and connect.await_count == 1
    assert not pool._requests and not transport._active_clients
