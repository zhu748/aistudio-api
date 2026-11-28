"""A stalled next read must not keep the previously accepted SSE frame alive."""

import asyncio
import gc
import json
from collections.abc import AsyncGenerator
from weakref import WeakValueDictionary

import httpx
import pytest
from fastapi import Request

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.keepalive import HeartbeatStreamingResponse, keepalive_response
from aistudio_api.application.api_service_gemini import GeminiStreamResponse
from aistudio_api.application.responses_service import _ResponsesStreamEmitter
from aistudio_api.config import settings


class _Marker:
    pass


class _Encoded(bytes):
    marker: _Marker


def _watch(text: str, live: WeakValueDictionary[str, object]) -> str:
    class WatchedText(str):
        def encode(self, encoding="utf-8", errors="strict"):
            body = _Encoded(super().encode(encoding, errors))
            # bytes cannot be weak-referenced; only these bytes own this marker.
            body.marker = _Marker()
            live["encoded"] = body.marker
            return body

    frame = WatchedText(text)
    live["text"] = frame
    return frame


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize(
    "ending", ["complete", "cancel_read", "cancel_send", "timeout"]
)
async def test_boundary_releases_frame_only_after_send(monkeypatch, deferred, ending):
    live = WeakValueDictionary()
    heartbeat, writing, accept = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reading, finish = asyncio.Event(), asyncio.Event()
    text = "data: Zürich 🌱\n\n"

    class Source:
        reads = closes = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.reads += 1
            if self.reads == 1:
                # The producer keeps no reference to the returned frame.
                return _watch(text, live)
            reading.set()
            await finish.wait()
            raise StopAsyncIteration

        async def aclose(self):
            await asyncio.sleep(0)
            self.closes += 1

    source = Source()

    async def generate():
        if deferred:
            await heartbeat.wait()
        return HeartbeatStreamingResponse(source)

    async def send(message):
        body = message.get("body")
        if body == b": keepalive\n\n":
            heartbeat.set()
        elif isinstance(body, _Encoded):
            assert body == text.encode()
            assert set(live) == {"text", "encoded"}
            writing.set()
            await accept.wait()

    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    if ending == "timeout":
        monkeypatch.setattr(settings, "stream_send_timeout", 0.05)
    response = await keepalive_response(
        generate, Request({"type": "http", "path": "/v1/responses"}), stream=True
    )
    assert isinstance(response, HeartbeatStreamingResponse)
    task = asyncio.create_task(response.stream_response(send))
    try:
        async with asyncio.timeout(3):
            await writing.wait()
            assert source.reads == 1
            if ending == "timeout":
                with pytest.raises(OSError, match="send timed out"):
                    await task
            else:
                if ending != "cancel_send":
                    accept.set()
                    await reading.wait()
                    gc.collect()
                    assert not live
                if ending == "complete":
                    finish.set()
                    await task
                else:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert source.reads == (1 if ending in ("cancel_send", "timeout") else 2)
    assert source.closes == 1


@pytest.mark.parametrize("deferred", [False, True])
async def test_quiet_responses_route_releases_accepted_catalog(monkeypatch, deferred):
    live = WeakValueDictionary()
    heartbeat, reading, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = _ResponsesStreamEmitter.in_progress
    status = None
    saw_catalog = False
    closes = 0

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        nonlocal closes
        try:
            reading.set()
            await finish.wait()
            yield {"candidates": [{"finishReason": "STOP"}]}
        finally:
            closes += 1

    async def generate(*_args, **_kwargs):
        if deferred:
            await heartbeat.wait()
        return GeminiStreamResponse(frames())

    async def observe(scope, receive, send):
        async def sink(message):
            nonlocal status, saw_catalog
            body = message.get("body")
            if isinstance(body, _Encoded):
                assert body.startswith(b"event: response.in_progress\n")
                saw_catalog = True
                # HTTPX buffers ASGI bodies. Give the client its own copy just
                # as a socket consumer would, not another owner of the marker.
                message = {**message, "body": bytes(body)}
            elif body == b": keepalive\n\n":
                heartbeat.set()
                if saw_catalog and reading.is_set() and not finish.is_set():
                    # Both Starlette middleware forwarders have now moved on to
                    # this heartbeat, while our SSE source is still parked.
                    gc.collect()
                    status = set(live)
                    finish.set()
            await send(message)

        await app(scope, receive, sink)

    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.02)
    monkeypatch.setitem(app.dependency_overrides, get_client, object)
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", generate)
    monkeypatch.setattr(
        _ResponsesStreamEmitter,
        "in_progress",
        lambda self: _watch(original(self), live),
    )
    tool = {
        "type": "function",
        "name": "preview",
        "description": "京都 🌱 " * 128,
        "parameters": {"type": "object"},
        "strict": False,
    }
    async with (
        asyncio.timeout(3),
        httpx.AsyncClient(
            base_url="http://fixture.invalid",
            transport=httpx.ASGITransport(app=observe),
        ) as client,
    ):
        response = await client.post(
            "/v1/responses",
            json={"model": "fixture", "input": "test", "stream": True, "tools": [tool]},
        )
    assert status == set()
    assert closes == 1 and response.status_code == 200
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [(e["type"], e["sequence_number"]) for e in events] == [
        ("response.created", 0),
        ("response.in_progress", 1),
        ("response.completed", 2),
    ]
    assert all(e["response"]["tools"] == [tool] for e in events)
