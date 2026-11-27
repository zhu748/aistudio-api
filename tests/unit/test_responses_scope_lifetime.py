"""An idle HTTP connection must not retain closed Responses output."""

import asyncio
import gc
import json
import weakref
from collections.abc import AsyncGenerator, Callable
from typing import cast

import h11
import pytest
from fastapi import FastAPI
from uvicorn import Config
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.application.api_service_gemini import GeminiStreamResponse
from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import active_requests

_PATH = "/v1/responses"
_TEXT = "Weather in Zürich: 天気 ☂\n" * 2048
_ARGS = {"city": "Zürich", "notes": "clear " * 1024}


class _InMemoryTransport(asyncio.Transport):
    def __init__(self, observe: Callable[[bytes], None]) -> None:
        super().__init__()
        self.output = bytearray()
        self.closed = False
        self.observe = observe

    def write(self, data: bytes | bytearray | memoryview) -> None:
        packet = bytes(data)
        self.output.extend(packet)
        self.observe(packet)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


def _response_events(peer: h11.Connection, raw: bytes) -> list[dict[str, object]]:
    peer.receive_data(raw)
    status = peer.next_event()
    assert isinstance(status, h11.Response) and status.status_code == 200
    body = bytearray()
    while isinstance(event := peer.next_event(), h11.Data):
        body.extend(event.data)
    return [
        json.loads(line[6:]) for line in body.split(b"\n") if line.startswith(b"data: ")
    ]


@pytest.mark.parametrize("outcome", ["completed", "deadline", "disconnect"])
async def test_responses_callbacks_released_on_retained_http_scope(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 1)
    wall_timer: asyncio.Timeout | None = None
    original_timeout = asyncio.timeout
    wall_budget = 23.5

    def capture_wall_timer(delay: float | None) -> asyncio.Timeout:
        nonlocal wall_timer
        timer = original_timeout(delay)
        if delay == wall_budget:
            wall_timer = timer
        return timer

    monkeypatch.setattr(settings, "request_timeout", wall_budget)
    monkeypatch.setattr(asyncio, "timeout", capture_wall_timer)

    source_closed = 0
    wait_for_consumer = asyncio.Event()

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        nonlocal source_closed
        try:
            yield {"candidates": [{"content": {"parts": [{"text": _TEXT}]}}]}
            if outcome != "completed":
                await wait_for_consumer.wait()
            call = {"id": "owned", "name": "weather", "args": _ARGS}
            yield {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": call, "thoughtSignature": "c2ln"}
                            ]
                        }
                    }
                ]
            }
            yield {"candidates": [{"finishReason": "STOP"}]}
        finally:
            source_closed += 1

    async def generate(*_args: object, **_kwargs: object) -> GeminiStreamResponse:
        return GeminiStreamResponse(frames())

    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", generate)
    app = FastAPI()
    app.include_router(routes_compat.router)
    app.add_middleware(RequestMiddleware)
    app.dependency_overrides[get_client] = object
    loop = asyncio.get_running_loop()
    server = ServerState()
    foreign_state = object()
    protocol = H11Protocol(
        Config(
            app,
            http="h11",
            ws="none",
            lifespan="off",
            access_log=False,
            log_config=None,
            proxy_headers=False,
            timeout_keep_alive=30,
        ),
        server,
        {"other_app_state": foreign_state},
        _loop=loop,
    )
    owners: dict[str, weakref.ReferenceType[object]] = {}
    request_registered_during_write = False

    def disconnect() -> None:
        transport.close()
        protocol.connection_lost(None)

    def observe(packet: bytes) -> None:
        nonlocal request_registered_during_write
        if b"event: response.output_text.delta\n" not in packet or owners:
            return
        state = protocol.scope.get("state")
        assert state is not None
        for key in ("stream_failure", "responses_accept_event"):
            owners[key] = weakref.ref(state[key].__self__)
        request_registered_during_write = len(active_requests()) == 1
        if outcome == "deadline":
            assert wall_timer is not None
            wall_timer.reschedule(loop.time())
        elif outcome == "disconnect":
            loop.call_soon(disconnect)

    transport = _InMemoryTransport(observe)
    protocol.connection_made(transport)
    peer = h11.Connection(h11.CLIENT)
    body = b'{"model":"fixture","input":"Weather?","stream":true}'
    request = peer.send(
        h11.Request(
            method="POST",
            target=_PATH,
            headers=[
                ("host", "fixture"),
                ("content-type", "application/json"),
                ("content-length", str(len(body))),
            ],
        )
    )
    assert request is not None
    head = peer.send(h11.Data(data=body))
    end = peer.send(h11.EndOfMessage())
    assert head is not None and end is not None
    try:
        protocol.data_received(request + head + end)
        async with asyncio.timeout(3):
            while server.tasks:
                await asyncio.sleep(0)
        # Let task completion callbacks release test-only references.
        await asyncio.sleep(0)
        assert source_closed == 1 and request_registered_during_write
        assert not active_requests()
        assert transport.closed == (outcome == "disconnect")
        assert protocol.cycle.scope is protocol.scope
        if outcome != "disconnect":
            assert protocol.cycle.response_complete
            assert protocol in server.connections
            assert protocol.timeout_keep_alive_task is not None

        events = _response_events(peer, bytes(transport.output))
        kinds = [e["type"] for e in events]
        assert [e["sequence_number"] for e in events] == list(range(len(events)))
        assert kinds[:2] == ["response.created", "response.in_progress"]
        if outcome == "disconnect":
            assert kinds[-1] == "response.output_text.delta"
        else:
            assert kinds[-1] == (
                "response.completed" if outcome == "completed" else "response.failed"
            )
            response = cast(dict[str, object], events[-1]["response"])
            output = cast(list[dict[str, object]], response["output"])
            assert output[0]["content"] == [
                {"type": "output_text", "text": _TEXT, "annotations": []}
            ]
            if outcome == "completed":
                assert len(output) == 2
                assert output[1]["call_id"] == "owned"
                assert output[1]["extra_content"] == {
                    "google": {"thought_signature": "c2ln"}
                }
                assert json.loads(cast(str, output[1]["arguments"])) == _ARGS
            else:
                assert len(output) == 1 and output[0]["status"] == "incomplete"
                assert "total timeout" in str(response["error"])

        # The real HTTP protocol deliberately keeps the request scope until the
        # next request or idle close. It must not keep either callback's owner.
        gc.collect()
        state = protocol.scope.get("state")
        assert state is not None
        assert state["other_app_state"] is foreign_state
        assert "stream_failure" not in state
        assert "responses_accept_event" not in state
        assert all(ref() is None for ref in owners.values())
    finally:
        if not transport.closed:
            disconnect()
