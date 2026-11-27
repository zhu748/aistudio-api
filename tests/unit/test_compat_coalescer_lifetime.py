"""Finished compatibility tool assembly is not retained by a slow HTTP client."""

from __future__ import annotations

import asyncio
import gc
import json
import weakref
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.types import Message, Scope
from test_ordered_tool_routes import input_request, sse_events, tool_calls

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import stream_utils
from aistudio_api.application.api_service_gemini import GeminiStreamResponse

_CALLS = [
    {"id": "first", "name": "lookup", "args": {"city": "Paris"}},
    {"id": "second", "name": "lookup", "args": {"city": "Oslo"}},
]
_SUFFIX = "Retained display text."
_ERROR = "Fixture interrupted."
_FINISH = {"chat": "tool_calls", "claude": "tool_use", "responses": "completed"}


def _signal(protocol: str, event: dict[str, Any]) -> tuple[str, object] | None:
    """Project the wire events at the assembly boundary without generated IDs."""
    if protocol == "chat":
        if "error" in event:
            return "error", event["error"]
        choice = event["choices"][0]
        delta = choice["delta"]
        if calls := delta.get("tool_calls"):
            return "tool", calls[0]["id"]
        if text := delta.get("content"):
            return "text", text
        if finish := choice["finish_reason"]:
            return "finish", finish
    elif protocol == "claude":
        kind = event["type"]
        if kind == "error":
            return "error", event["error"]
        if (
            kind == "content_block_start"
            and event["content_block"]["type"] == "tool_use"
        ):
            return "tool", event["content_block"]["id"]
        if kind == "content_block_delta" and event["delta"]["type"] == "text_delta":
            return "text", event["delta"]["text"]
        if kind == "message_delta":
            return "finish", event["delta"]["stop_reason"]
    else:
        kind = event["type"]
        if kind == "response.failed":
            response = event["response"]
            return "error", {
                "status": response["status"],
                "message": response["error"]["message"],
            }
        if (
            kind == "response.output_item.added"
            and event["item"]["type"] == "function_call"
        ):
            return "tool", event["item"]["call_id"]
        if kind == "response.output_text.delta":
            return "text", event["delta"]
        if kind == "response.completed":
            return "finish", event["response"]["status"]
    return None


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize("ending", ["stop", "error_with_suffix", "error"])
async def test_finished_compat_assembler_is_released_before_asgi_write(
    monkeypatch: pytest.MonkeyPatch, protocol: str, ending: str
):
    refs: list[weakref.ReferenceType[stream_utils.ToolResponseAssembler]] = []

    class TrackedAssembler(stream_utils.ToolResponseAssembler):
        def __init__(self, *, synthesize_ids: bool = False) -> None:
            super().__init__(synthesize_ids=synthesize_ids)
            refs.append(weakref.ref(self))

    monkeypatch.setattr(stream_utils, "ToolResponseAssembler", TrackedAssembler)
    closed = asyncio.Event()

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        parts: list[dict[str, object]] = [
            {"functionCall": call, "thoughtSignature": "c2ln"} for call in _CALLS
        ]
        if ending != "error":
            parts.append({"text": _SUFFIX})
        try:
            yield {"candidates": [{"content": {"parts": parts}}]}
            # The assembler must still own the undecided batch at this point.
            assert len(refs) == 1 and refs[0]() is not None
            if ending == "stop":
                yield {"candidates": [{"finishReason": "STOP"}]}
            else:
                yield {"error": {"code": 429, "message": _ERROR}}
        finally:
            closed.set()

    app = FastAPI()
    app.include_router(routes_compat.router)
    app.dependency_overrides[get_client] = object
    monkeypatch.setattr(
        routes_compat,
        "handle_gemini_generate_content",
        AsyncMock(return_value=GeminiStreamResponse(frames())),
    )
    path, payload = input_request(protocol, True)
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "scheme": "http",
        "server": ("test", 80),
    }
    submitted = False

    async def receive() -> Message:
        nonlocal submitted
        if submitted:
            await asyncio.Event().wait()
        submitted = True
        return {
            "type": "http.request",
            "body": json.dumps(payload).encode(),
            "more_body": False,
        }

    paused, resume = asyncio.Event(), asyncio.Event()
    events: list[dict[str, Any]] = []
    signals: list[tuple[str, object]] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] != "http.response.body":
            return
        for event in sse_events(message.get("body", b"").decode()):
            events.append(event)
            if (signal := _signal(protocol, event)) is not None:
                signals.append(signal)
                if not paused.is_set():
                    paused.set()
                    await resume.wait()

    request = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(paused.wait(), timeout=3)
        assert not request.done()
        gc.collect()
        assert len(refs) == 1 and refs[0]() is None
    finally:
        resume.set()
        await asyncio.wait_for(request, timeout=3)
    assert closed.is_set()

    if ending == "stop":
        assert tool_calls(protocol, True, events) == _CALLS
        assert signals == [
            ("tool", "first"),
            ("tool", "second"),
            ("text", _SUFFIX),
            ("finish", _FINISH[protocol]),
        ]
    else:
        assert not tool_calls(protocol, True, events)
        error: dict[str, object] = {"type": "rate_limit_error", "message": _ERROR}
        if protocol == "chat":
            error.update(code=429, param=None)
        elif protocol == "responses":
            error = {"status": "failed", "message": _ERROR}
        expected: list[tuple[str, object]] = (
            [("text", _SUFFIX)] if ending == "error_with_suffix" else []
        )
        assert signals == [*expected, ("error", error)]
