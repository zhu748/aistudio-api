"""Large completed function arguments should follow the actual HTTP consumer."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect
from starlette.types import Scope

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.application import responses_service
from aistudio_api.application.api_service_gemini import GeminiStreamResponse
from aistudio_api.config import settings

_TOOL_EVENTS = [
    "response.output_item.added",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.done",
    "response.output_item.done",
]


@pytest.fixture
def post_responses(monkeypatch):
    app = FastAPI()
    app.include_router(routes_compat.router)
    app.add_middleware(RequestMiddleware)

    async def client():
        return object()

    app.dependency_overrides[get_client] = client
    monkeypatch.setattr(settings, "request_history", False)

    async def post(frames, send):
        monkeypatch.setattr(
            routes_compat,
            "handle_gemini_generate_content",
            AsyncMock(return_value=GeminiStreamResponse(frames)),
        )
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/v1/responses",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("test", 80),
        }
        submitted = False

        async def receive():
            nonlocal submitted
            if submitted:
                await asyncio.Event().wait()
            submitted = True
            return {
                "type": "http.request",
                "body": b'{"model":"fixture","input":"test","stream":true}',
            }

        await app(scope, receive, send)

    return post


@pytest.mark.parametrize("disconnect_at", [None, 0, 1])
async def test_argument_frames_wait_for_each_asgi_write(
    post_responses, monkeypatch, disconnect_at
):
    args = {"q": '🙂 "a"\n' * 8096}
    signature = "c2ln"
    new_call = {
        "functionCall": {"name": "lookup", "id": "fixture-id", "args": args},
        "thoughtSignature": signature,
    }
    closed = asyncio.Event()

    async def frames():
        try:
            yield {"candidates": [{"content": {"parts": [new_call]}}]}
            yield {"candidates": [{"finishReason": "STOP"}]}
        finally:
            closed.set()

    encoded = []
    original_sse = responses_service._ResponsesStreamEmitter.sse

    def tracked(self, kind, payload):
        if kind in _TOOL_EVENTS:
            encoded.append(kind)
        return original_sse(self, kind, payload)

    monkeypatch.setattr(responses_service._ResponsesStreamEmitter, "sse", tracked)
    reached = [asyncio.Event() for _ in _TOOL_EVENTS]
    resume = [asyncio.Event() for _ in _TOOL_EVENTS]
    events = []

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        for line in message.get("body", b"").split(b"\n"):
            if not line.startswith(b"data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            if event["type"] in _TOOL_EVENTS:
                index = _TOOL_EVENTS.index(event["type"])
                reached[index].set()
                await resume[index].wait()
                if index == disconnect_at:
                    raise OSError("fixture disconnected")

    request = asyncio.create_task(post_responses(frames(), send))
    try:
        for index, pause in enumerate(reached):
            await asyncio.wait_for(pause.wait(), timeout=3)
            # Count actual JSON serializations, not merely frames delivered:
            # the three full argument envelopes must not be queued behind us.
            assert encoded == _TOOL_EVENTS[: index + 1]
            assert not request.done()
            resume[index].set()
            if index == disconnect_at:
                break
    finally:
        for gate in resume:
            gate.set()
        if disconnect_at is None:
            await asyncio.wait_for(request, timeout=3)
        else:
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(request, timeout=3)

    assert closed.is_set()
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    if disconnect_at is not None:
        assert events[-1]["type"] == _TOOL_EVENTS[disconnect_at]
        assert encoded == _TOOL_EVENTS[: disconnect_at + 1]
        return

    assert [e["type"] for e in events] == [
        "response.created",
        "response.in_progress",
        *_TOOL_EVENTS,
        "response.completed",
    ]
    added, delta, done, item_done, terminal = events[2:]
    item = item_done["item"]
    assert added["item"] == {**item, "arguments": "", "status": "in_progress"}
    assert item["type"] == "function_call"
    assert (item["name"], item["call_id"]) == ("lookup", "fixture-id")
    assert item["extra_content"]["google"]["thought_signature"] == signature
    assert delta["item_id"] == done["item_id"] == item["id"]
    assert all(e["output_index"] == 0 for e in (added, delta, done, item_done))
    assert delta["delta"] == done["arguments"] == item["arguments"]
    assert json.loads(item["arguments"]) == args
    assert terminal["response"]["output"] == [item]


@pytest.mark.parametrize(
    ("last", "terminal"),
    [
        ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "response.incomplete"),
        ({"error": {"code": 429, "message": "fixture busy"}}, "response.failed"),
    ],
)
async def test_unconfirmed_arguments_never_get_serialized(
    post_responses, last, terminal
):
    async def frames():
        yield {
            "candidates": [
                {
                    "content": {
                        "parts": [{"functionCall": {"name": "lookup", "args": {}}}]
                    }
                }
            ]
        }
        yield last

    events = []

    async def send(message):
        for line in message.get("body", b"").split(b"\n"):
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))

    await post_responses(frames(), send)
    assert [e["type"] for e in events] == [
        "response.created",
        "response.in_progress",
        terminal,
    ]
    assert events[-1]["response"]["output"] == []
