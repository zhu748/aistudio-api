"""Terminal event delivery and EOF cleanup under request cancellation."""

import asyncio
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from aistudio_api.api.keepalive import HeartbeatStreamingResponse
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.state import runtime_state
from aistudio_api.application.compat_service import (
    claude_stream_adapter,
    openai_stream_adapter,
)
from aistudio_api.application.responses_service import responses_stream_adapter
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize("failed", [False, True])
async def test_timeout_after_terminal_event_only_finishes_http(
    monkeypatch, protocol, failed
):
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "request_timeout", 0.05)
    monkeypatch.setattr(runtime_state, "account_service", None)
    path = {
        "chat": "/v1/chat/completions",
        "claude": "/v1/messages",
        "responses": "/v1/responses",
    }[protocol]
    messages = []

    async def frames():
        if failed:
            yield {"error": {"code": 503, "message": "original error"}}
        else:
            yield {
                "candidates": [
                    {"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}
                ]
            }

    async def delayed_final(scope, receive, send):
        def register(handler):
            scope["state"]["stream_failure"] = handler

        if protocol == "chat":
            stream = openai_stream_adapter(
                frames(),
                requested_model="test",
                include_usage=False,
                register_failure=register,
            )
        elif protocol == "claude":
            stream = claude_stream_adapter(
                frames(),
                requested_model="test",
                stop_sequences=None,
                register_failure=register,
            )
        else:
            stream = responses_stream_adapter(
                frames(), requested_model="test", register_failure=register
            )
        response = HeartbeatStreamingResponse(stream, media_type="text/event-stream")

        async def slow_end(message):
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                await asyncio.Event().wait()
            await send(message)

        await response(scope, receive, slow_end)

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    middleware = RequestMiddleware(delayed_final)
    persist = AsyncMock()
    monkeypatch.setattr(middleware, "_persist", persist)
    async with asyncio.timeout(1):
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "asgi": {"spec_version": "2.4"},
            },
            receive,
            send,
        )
    raw = b"".join(m.get("body", b"") for m in messages)
    assert b"total timeout" not in raw
    assert not messages[-1].get("more_body", False)
    if protocol == "chat":
        assert raw.count(b"data: [DONE]") == 1
    elif protocol == "responses":
        assert raw.count(b"event: response.failed") == int(failed)
        assert raw.count(b"event: response.completed") == int(not failed)
    else:
        assert raw.count(b"event: error") == int(failed)
        assert raw.count(b"event: message_stop") == int(not failed)
    assert persist.call_args.args[0]["result"] != "timeout"


@pytest.mark.parametrize("streaming", [True, False])
async def test_cancellation_during_eof_close_completes_transport_cleanup(streaming):
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"complete"

        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    def handler(request):
        return httpx.Response(200, stream=Body())

    transport = HTTPStreamTransport()
    transport._clients[transport.proxy_url or ""] = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    async def consume():
        if streaming:
            async for _ in transport.send_streaming_request(
                url="https://example.com", headers={}, body="{}"
            ):
                pass
        else:
            await transport.send_request(
                url="https://example.com", headers={}, body="{}"
            )

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await closing.wait()
            group.cancel_scope.cancel()
            release.set()
        assert closed.is_set()
    finally:
        await transport.close()


@pytest.mark.parametrize("repeated", [False, True])
async def test_raw_task_cancellation_during_eof_close_is_drained(repeated):
    closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"complete"

        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    transport = HTTPStreamTransport()
    transport._clients[transport.proxy_url or ""] = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Body())
        )
    )
    task = asyncio.create_task(
        transport.send_request(url="https://example.com", headers={}, body="{}")
    )
    try:
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        if repeated:
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    finally:
        release.set()
        await transport.close()
