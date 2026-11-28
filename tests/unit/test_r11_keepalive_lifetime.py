"""Deferred JSON and SSE preparation callbacks must not pin a finished request."""

import asyncio
import gc
import json
import weakref
from unittest.mock import AsyncMock

import h11
import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response
from starlette.types import Scope
from test_keepalive import request_for
from test_responses_scope_lifetime import _InMemoryTransport
from uvicorn import Config
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from aistudio_api.api import request_middleware, routes_compat, routes_gemini
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import active_request_count


@pytest.mark.parametrize(
    ("channel", "stream"),
    [
        ("chat", False),
        ("claude", False),
        ("responses", False),
        ("gemini", False),
        ("chat", True),
        ("claude", True),
    ],
)
async def test_deferred_callbacks_released_on_idle_connection(
    monkeypatch: pytest.MonkeyPatch, channel: str, stream: bool
) -> None:
    monkeypatch.setattr(settings, "nonstream_keepalive", True)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    monkeypatch.setattr(settings, "request_history", True)
    heartbeat = b": keepalive\n\n" if stream else b" \n"
    hook = "stream_failure" if stream else "json_keepalive_failure"
    release = asyncio.Event()
    request_ref: weakref.ReferenceType[Request] | None = None

    async def track_request(request: Request) -> object:
        nonlocal request_ref
        request_ref = weakref.ref(request)
        return object()

    async def generate(
        *_args: object, **_kwargs: object
    ) -> GeminiGenerateContentResponse:
        await release.wait()
        if stream:
            # No normal stream adapter replaces the preparation-failure hook.
            raise HTTPException(503, "Fixture unavailable")
        return GeminiGenerateContentResponse.model_validate(
            {"candidates": [{"content": {"parts": [{"text": "Fixture answer"}]}}]}
        )

    routes = routes_gemini if channel == "gemini" else routes_compat
    monkeypatch.setattr(routes, "handle_gemini_generate_content", generate)
    app = FastAPI()
    app.include_router(routes.router)
    app.add_middleware(request_middleware.RequestMiddleware)
    app.dependency_overrides[get_client] = track_request
    foreign = object()
    app_state = {"other_state": foreign}
    if channel != "responses":
        app_state["responses_accept_event"] = foreign
    server = ServerState()
    protocol = H11Protocol(
        Config(
            app,
            http="h11",
            ws="none",
            lifespan="off",
            access_log=False,
            log_config=None,
            proxy_headers=False,
        ),
        server,
        app_state,
        _loop=asyncio.get_running_loop(),
    )

    def hook_is_live() -> bool:
        return callable(protocol.scope.get("state", {}).get(hook))

    # Observe the final insert on the normal shielded history path without I/O.
    final_insert: list[tuple[object, bool, int]] = []

    class History:
        def insert(self, record: dict[str, object]) -> None:
            if "finished_at" in record:
                final_insert.append(
                    (record["result"], hook_is_live(), active_request_count())
                )

    monkeypatch.setattr(request_middleware, "request_history", History)

    def observe(packet: bytes) -> None:
        if heartbeat in packet:
            assert hook_is_live()
            assert active_request_count() == 1
            release.set()

    transport = _InMemoryTransport(observe)
    protocol.connection_made(transport)
    path, payload = request_for(channel, stream)
    body = json.dumps(payload).encode()
    # Real uvicorn h11 cycle; no listening sockets or upstream connections.
    wire = (
        f"POST {path} HTTP/1.1\r\nHost: fixture\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    try:
        protocol.data_received(wire)
        async with asyncio.timeout(3):
            while server.tasks:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert release.is_set()
        assert final_insert == [("error" if stream else "success", True, 1)]
        assert not active_request_count()
        assert not transport.closed and protocol.cycle.response_complete
        assert protocol.timeout_keep_alive_task is not None
        assert protocol.cycle.scope is protocol.scope
        peer = h11.Connection(h11.CLIENT)
        peer.receive_data(bytes(transport.output))
        headers = peer.next_event()
        assert isinstance(headers, h11.Response) and headers.status_code == 200
        received = bytearray()
        while isinstance(event := peer.next_event(), h11.Data):
            received.extend(event.data)
        assert isinstance(event, h11.EndOfMessage)
        assert received.startswith(heartbeat)
        if stream:
            assert b"Fixture unavailable" in received
            if channel == "chat":
                assert received.endswith(b"data: [DONE]\n\n")
            else:
                assert b"event: error\n" in received
        else:
            assert "Fixture answer" in str(json.loads(received))

        # Keep the HTTP connection (and therefore its last scope) alive.
        # The route's Request, with its cached JSON/body, must still be freed.
        gc.collect()
        assert protocol.scope.get("state") == app_state
        assert request_ref is not None and request_ref() is None
    finally:
        transport.close()
        protocol.connection_lost(None)


async def test_non_model_scope_keys_are_not_owned_by_the_gateway() -> None:
    state = {
        key: object()
        for key in (
            "stream_failure",
            "json_keepalive_failure",
            "responses_accept_event",
        )
    }
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/health",
        "headers": [],
        "state": state.copy(),
    }
    await request_middleware.RequestMiddleware(Response(status_code=204))(
        scope, AsyncMock(), AsyncMock()
    )
    assert scope["state"] == state
