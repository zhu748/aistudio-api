"""Quiet generations must keep the HTTP connection active without fake content."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.keepalive import HeartbeatStreamingResponse, keepalive_response
from aistudio_api.api.state import runtime_state
from aistudio_api.application import api_service_gemini as service
from aistudio_api.config import settings
from aistudio_api.domain.errors import TransportError
from aistudio_api.domain.models import Candidate, ModelOutput


@pytest.fixture
def quiet_gateway(monkeypatch):
    release, closed = asyncio.Event(), asyncio.Event()
    calls = []
    tool = {"name": "weather", "args": {"city": "上海"}, "call_id": "weather_1"}

    async def generate_content(**kwargs):
        calls.append("buffered")
        try:
            await release.wait()
            return ModelOutput(
                candidates=[Candidate(function_calls=[tool], finish_reason=1)]
            )
        finally:
            closed.set()

    async def stream_generate_content(**kwargs):
        calls.append("stream")
        try:
            await release.wait()
            yield "tool_calls", [tool]
            yield "finish_reason", 1
        finally:
            closed.set()

    client = SimpleNamespace(
        generate_content=generate_content,
        stream_generate_content=stream_generate_content,
    )
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_client] = lambda: client
    monkeypatch.setattr(service, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(service, "record_rotator_event", lambda *a, **k: None)
    monkeypatch.setattr(runtime_state, "account_service", None)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    monkeypatch.setattr(settings, "nonstream_keepalive", True)
    try:
        yield release, closed, calls
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def request_for(protocol, stream):
    payload = {"model": "gemini-3.8-flash", "stream": stream}
    if protocol == "gemini":
        suffix = "streamGenerateContent" if stream else "generateContent"
        return f"/v1beta/models/gemini-3.8-flash:{suffix}", {
            "contents": [{"role": "user", "parts": [{"text": "weather"}]}]
        }
    if protocol == "responses":
        return "/v1/responses", {**payload, "input": "weather"}
    return ("/v1/messages" if protocol == "claude" else "/v1/chat/completions"), {
        **payload,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "weather"}],
    }


@pytest.mark.parametrize("protocol", ["chat", "responses", "claude", "gemini"])
@pytest.mark.parametrize("stream", [True, False])
async def test_quiet_tools_emit_multiple_heartbeats_before_result(
    quiet_gateway, protocol, stream
):
    release, closed, calls = quiet_gateway
    heartbeats = []

    async def observed_app(scope, receive, send):
        async def observe(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body in (b": keepalive\n\n", b"\n\n", b" \n"):
                    assert not closed.is_set()
                    heartbeats.append(body)
                    if len(heartbeats) == 3:
                        release.set()
            await send(message)

        await app(scope, receive, observe)

    path, payload = request_for(protocol, stream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(observed_app), base_url="http://test"
    ) as client:
        async with asyncio.timeout(3):
            response = await client.post(path, json=payload)
    assert len(heartbeats) >= 3 and closed.is_set()
    assert len(calls) == 1
    assert "no-transform" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"
    assert "weather" in response.text
    if stream:
        assert "text/event-stream" in response.headers["content-type"]
    else:
        assert "application/json" in response.headers["content-type"]
        assert isinstance(response.json(), dict)
        assert "error" not in response.json()


@pytest.mark.parametrize("protocol", ["chat", "responses", "claude", "gemini"])
async def test_buffered_total_timeout_still_returns_valid_error_json(
    quiet_gateway, monkeypatch, protocol
):
    _, closed, _ = quiet_gateway
    monkeypatch.setattr(settings, "request_timeout", 0.1)
    path, payload = request_for(protocol, False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, json=payload)
    assert response.status_code == 200  # Heartbeat already committed headers.
    assert "total timeout" in response.json()["error"]["message"]
    assert closed.is_set()


@pytest.mark.parametrize("slow", [False, True])
async def test_json_error_preserves_early_status_and_late_error_body(monkeypatch, slow):
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    request = Request(
        {"type": "http", "path": "/v1/chat/completions", "headers": [], "state": {}}
    )
    release = asyncio.Event()

    async def generate():
        if slow:
            await release.wait()
        raise HTTPException(403, detail="permission denied")

    if not slow:
        with pytest.raises(HTTPException) as exc:
            await keepalive_response(generate, request)
        assert exc.value.status_code == 403
        return
    response = await keepalive_response(generate, request)
    messages = []

    async def send(message):
        messages.append(message)
        if message.get("body") == b" \n":
            release.set()

    assert isinstance(response, HeartbeatStreamingResponse)
    await response.stream_response(send)
    body = b"".join(m.get("body", b"") for m in messages)
    assert messages[0]["status"] == 200
    assert json.loads(body)["error"]["code"] == 403


async def test_source_context_stays_in_one_task_and_heartbeat_does_not_cancel_read(
    monkeypatch,
):
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    variable = ContextVar("test", default="parent")
    release, closed = asyncio.Event(), asyncio.Event()
    beats = 0

    async def source():
        token = variable.set("child")
        try:
            yield "data: first\n\n"
            await release.wait()
            assert variable.get() == "child"
            yield "data: last\n\n"
        finally:
            variable.reset(token)
            closed.set()

    async def send(message):
        nonlocal beats
        if message.get("body") == b": keepalive\n\n":
            beats += 1
            if beats == 3:
                release.set()

    async with asyncio.timeout(2):
        await HeartbeatStreamingResponse(source()).stream_response(send)
    assert closed.is_set() and variable.get() == "parent"


@pytest.mark.parametrize("json_mode", [False, True])
async def test_disconnect_releases_pending_generation(monkeypatch, json_mode):
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def generate():
        try:
            entered.set()
            await asyncio.Event().wait()
            return JSONResponse({"ok": True})
        finally:
            closed.set()

    async def source():
        await generate()
        yield "data: done\n\n"

    if json_mode:
        response = await keepalive_response(
            generate,
            Request(
                {"type": "http", "path": "/v1/messages", "headers": [], "state": {}}
            ),
        )
    else:
        response = HeartbeatStreamingResponse(source())

    async def send(message):
        pass

    async def receive():
        await entered.wait()
        return {"type": "http.disconnect"}

    async with asyncio.timeout(2):
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
    assert closed.is_set()


@pytest.mark.parametrize("has_output", [False, True])
async def test_single_account_transport_retry_is_bounded_and_never_replays_output(
    monkeypatch, has_output
):
    switch = AsyncMock(return_value=False)
    monkeypatch.setattr(service, "try_switch_account", switch)
    monkeypatch.setattr(service, "get_max_retries", lambda: 5)
    monkeypatch.setattr(service, "record_rotator_event", lambda *a, **k: None)
    monkeypatch.setattr(runtime_state, "account_service", None)
    for attempt in range(3):
        args = {
            "attempt": attempt,
            "model_path": "test",
            "normalized_model": "test",
            "has_yielded_data": has_output,
        }
        if not has_output and attempt < 2:
            assert await service.handle_attempt_exception(
                TransportError("connection reset"), **args
            )
        else:
            with pytest.raises(HTTPException) as exc:
                await service.handle_attempt_exception(
                    TransportError("connection reset"), **args
                )
            assert exc.value.status_code == 502
    assert switch.await_count == (0 if has_output else 2)


async def test_blocked_socket_send_times_out_and_releases_admission(monkeypatch):
    from aistudio_api.api.request_middleware import RequestMiddleware

    monkeypatch.setattr(settings, "stream_send_timeout", 0.02)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(runtime_state, "account_service", None)

    async def source(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"response"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def blocked(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    middleware = RequestMiddleware(source)
    async with asyncio.timeout(2):
        with pytest.raises(OSError, match="send timed out"):
            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "headers": [],
                },
                receive,
                blocked,
            )
    assert middleware.active == 0


async def test_json_header_send_failure_cancels_task_before_iterator_start(monkeypatch):
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    closed = asyncio.Event()

    async def generate():
        try:
            await asyncio.Event().wait()
            return JSONResponse({})
        finally:
            closed.set()

    response = await keepalive_response(
        generate,
        Request(
            {"type": "http", "path": "/v1/chat/completions", "headers": [], "state": {}}
        ),
    )
    assert isinstance(response, HeartbeatStreamingResponse)

    async def failed_send(message):
        raise OSError("disconnected")

    with pytest.raises(OSError, match="disconnected"):
        await response.stream_response(failed_send)
    assert closed.is_set()


@pytest.mark.parametrize("streaming", [True, False])
async def test_anyio_disconnect_finishes_async_http_response_close(streaming):
    import anyio

    from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport

    entered, closed = asyncio.Event(), asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unused"

        async def aclose(self):
            await anyio.sleep(0)  # HTTP/2 pool cleanup has real cancellation points.
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
                url="https://example.com/generate", headers={}, body="{}"
            ):
                pass
        else:
            await transport.send_request(
                url="https://example.com/generate", headers={}, body="{}"
            )

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await entered.wait()
            group.cancel_scope.cancel()
        assert closed.is_set()
    finally:
        await transport.close()
