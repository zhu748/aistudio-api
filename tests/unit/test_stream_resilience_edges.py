"""Failures around preparation and final delivery must not corrupt the wire."""

import asyncio
import json

import httpx
import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request
from test_keepalive import quiet_gateway as _quiet_gateway, request_for

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.keepalive import keepalive_response
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings

quiet_gateway = _quiet_gateway


@pytest.mark.parametrize("protocol", ["chat", "responses", "claude"])
@pytest.mark.parametrize("stream", [True, False])
async def test_slow_preparation_is_covered_by_heartbeats(
    quiet_gateway, monkeypatch, protocol, stream
):
    release, _, _ = quiet_gateway
    name = {
        "chat": "openai_to_gemini_request",
        "responses": "responses_to_gemini_request",
        "claude": "claude_to_gemini_request",
    }[protocol]
    original = getattr(routes_compat, name)
    prepared = asyncio.Event()

    async def slow(request):
        await release.wait()
        result = await original(request)
        prepared.set()
        return result

    monkeypatch.setattr(routes_compat, name, slow)
    beats = 0

    async def observe_app(scope, receive, send):
        async def observed_send(message):
            nonlocal beats
            if message.get("body") in (b": keepalive\n\n", b" \n"):
                if not prepared.is_set():
                    beats += 1
                if beats == 3:
                    release.set()
            await send(message)

        await app(scope, receive, observed_send)

    path, payload = request_for(protocol, stream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(observe_app), base_url="http://test"
    ) as client:
        async with asyncio.timeout(1):
            response = await client.post(path, json=payload)
    assert beats >= 3 and "weather" in response.text


async def test_timeout_after_json_delivery_does_not_append_another_json(monkeypatch):
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    monkeypatch.setattr(settings, "request_timeout", 0.1)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(runtime_state, "account_service", None)
    release = asyncio.Event()
    messages = []

    async def generate():
        await release.wait()
        return JSONResponse({"result": "complete"})

    async def delayed_teardown(scope, receive, send):
        response = await keepalive_response(generate, Request(scope))

        async def delayed_final(message):
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                await asyncio.Event().wait()
            await send(message)

        await response(scope, receive, delayed_final)

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message.get("body") == b" \n":
            release.set()

    middleware = RequestMiddleware(delayed_teardown)
    async with asyncio.timeout(1):
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "asgi": {"spec_version": "2.4"},
            },
            receive,
            send,
        )
    raw = b"".join(m.get("body", b"") for m in messages)
    assert json.loads(raw) == {"result": "complete"}
    assert len([m for m in messages if m["type"] == "http.response.start"]) == 1
    assert messages[-1].get("more_body", False) is False


@pytest.mark.parametrize("protocol", ["chat", "responses", "claude"])
@pytest.mark.parametrize("failure", ["validation", "timeout"])
async def test_slow_preparation_errors_remain_protocol_events(
    quiet_gateway, monkeypatch, protocol, failure
):
    name = {
        "chat": "openai_to_gemini_request",
        "responses": "responses_to_gemini_request",
        "claude": "claude_to_gemini_request",
    }[protocol]
    closed = asyncio.Event()

    async def fail(request):
        try:
            if failure == "validation":
                await asyncio.sleep(0.04)
                raise ValueError("invalid media")
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(routes_compat, name, fail)
    monkeypatch.setattr(settings, "request_timeout", 0.1)
    path, payload = request_for(protocol, True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, json=payload)
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert response.status_code == 200 and closed.is_set()
    assert ": keepalive" in response.text and events
    if protocol == "chat":
        assert response.text.endswith("data: [DONE]\n\n")
        assert events[-1]["error"]["code"] == (400 if failure == "validation" else 504)
    elif protocol == "responses":
        assert [event["type"] for event in events] == [
            "response.created",
            "response.failed",
        ]
        assert events[0]["response"]["id"] == events[1]["response"]["id"]
        assert events[1]["response"]["error"]["code"] == (
            "invalid_prompt" if failure == "validation" else "server_error"
        )
    else:
        assert events[-1]["type"] == "error"
        assert "event: error" in response.text


async def test_asgi24_heartbeat_socket_error_is_cancelled_not_internal_error(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from starlette.requests import ClientDisconnect

    from aistudio_api.api.keepalive import HeartbeatStreamingResponse

    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(runtime_state, "account_service", None)
    closed = asyncio.Event()

    async def source():
        try:
            await asyncio.Event().wait()
            yield "data: unused\n\n"
        finally:
            closed.set()

    response = HeartbeatStreamingResponse(source(), media_type="text/event-stream")
    middleware = RequestMiddleware(response)
    persisted = AsyncMock()
    monkeypatch.setattr(middleware, "_persist", persisted)
    count = 0

    async def send(message):
        nonlocal count
        if message.get("body") == b": keepalive\n\n":
            count += 1
            if count == 2:
                raise OSError("connection reset")

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
                "asgi": {"spec_version": "2.4"},
            },
            receive,
            send,
        )
    assert closed.is_set() and middleware.active == 0
    assert persisted.call_args.args[0]["result"] == "cancelled"


@pytest.mark.parametrize("code", [502, 503, 504])
@pytest.mark.parametrize("has_output", [False, True])
async def test_temporary_http_error_retry_obeys_output_and_budget(
    monkeypatch, code, has_output
):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from aistudio_api.application import api_service_gemini as service
    from aistudio_api.domain.errors import RequestError

    monkeypatch.setattr(service, "try_switch_account", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "record_rotator_event", lambda *a, **k: None)
    monkeypatch.setattr(runtime_state, "account_service", None)
    for retry in (0, 2):
        call = service.handle_attempt_exception(
            RequestError(code, "temporary upstream failure"),
            attempt=4,
            transient_attempt=retry,
            model_path="test",
            normalized_model="test",
            has_yielded_data=has_output,
        )
        if retry == 0 and not has_output:
            assert await call
        else:
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == code


@pytest.mark.parametrize("stream", [True, False])
async def test_full_request_recovers_from_two_upstream_503s(
    quiet_gateway, monkeypatch, stream
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aistudio_api.api.dependencies import get_client
    from aistudio_api.application import api_service_gemini as service
    from aistudio_api.domain.errors import RequestError
    from aistudio_api.domain.models import Candidate, ModelOutput

    attempts = []
    monkeypatch.setattr(service, "try_switch_account", AsyncMock(return_value=False))

    def attempt(kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3:
            raise RequestError(503, "temporarily unavailable")

    async def generate(**kwargs):
        attempt(kwargs)
        return ModelOutput(candidates=[Candidate(text="recovered", finish_reason=1)])

    async def events(**kwargs):
        attempt(kwargs)
        yield "body", "recovered"
        yield "finish_reason", 1

    app.dependency_overrides[get_client] = lambda: SimpleNamespace(
        generate_content=generate, stream_generate_content=events
    )
    path, payload = request_for("chat", stream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, json=payload)
    assert len(attempts) == 3 and "recovered" in response.text
    assert all(item["prepared"] is None for item in attempts)
    if not stream:
        assert response.json()["choices"][0]["message"]["content"] == "recovered"
