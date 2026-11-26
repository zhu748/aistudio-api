"""Responses close frames should follow downstream backpressure."""

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.application import responses_service
from aistudio_api.application.api_service_gemini import GeminiStreamResponse
from aistudio_api.config import settings


@pytest.mark.parametrize(
    ("finish", "outcome", "item_status"),
    [
        ("STOP", "completed", "completed"),
        ("MAX_TOKENS", "incomplete", "incomplete"),
        ("error_frame", "failed", "incomplete"),
        ("exception", "failed", "incomplete"),
        ("disconnect", None, "completed"),
    ],
)
async def test_responses_terminal_serialization_follows_asgi_send(
    monkeypatch, finish, outcome, item_status
):
    monkeypatch.setattr(settings, "request_history", False)
    assembled = []
    original_sse = responses_service._ResponsesStreamEmitter.sse

    def track_encoding(self, kind, payload):
        assembled.append(kind)
        return original_sse(self, kind, payload)

    monkeypatch.setattr(
        responses_service._ResponsesStreamEmitter, "sse", track_encoding
    )

    source_closed = asyncio.Event()
    reasoning = "计划 🙂"
    text = 'the "quoted" answer\n' * 2048

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        try:
            yield {
                "candidates": [
                    {"content": {"parts": [{"text": reasoning, "thought": True}]}}
                ]
            }
            for fragment in (text[:2048], text[2048:]):
                yield {"candidates": [{"content": {"parts": [{"text": fragment}]}}]}
            if finish == "error_frame":
                yield {"error": {"code": 429, "message": "fixture exhausted"}}
                return
            if finish == "exception":
                raise ValueError("fixture interrupted")
            yield {
                "candidates": [
                    {"finishReason": "STOP" if finish == "disconnect" else finish}
                ],
                "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 12},
            }
        finally:
            source_closed.set()

    monkeypatch.setattr(
        routes_compat,
        "handle_gemini_generate_content",
        AsyncMock(return_value=GeminiStreamResponse(frames())),
    )
    app = FastAPI()
    app.include_router(routes_compat.router)
    app.add_middleware(RequestMiddleware)

    async def dummy_client():
        return object()

    app.dependency_overrides[get_client] = dummy_client
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/responses",
        "query_string": b"",
        "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
        "scheme": "http",
        "client": ("test", 1),
        "server": ("test", 80),
    }
    submitted = False
    events = []
    reading = asyncio.Event()
    release = asyncio.Event()

    async def receive():
        nonlocal submitted
        if submitted:
            await asyncio.Event().wait()
        submitted = True
        return {
            "type": "http.request",
            "body": b'{"model":"fixture","input":"test","stream":true}',
        }

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
            return
        for line in message.get("body", b"").split(b"\n"):
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] == "response.output_text.done":
                    reading.set()
                    await release.wait()
                    if finish == "disconnect":
                        raise OSError("fixture disconnected")

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(reading.wait(), timeout=2)
        assert assembled[-1] == "response.output_text.done"
        assert not task.done()
    finally:
        release.set()
        if finish == "disconnect":
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(task, timeout=2)
        else:
            await asyncio.wait_for(task, timeout=2)
    assert source_closed.is_set()

    event_types = [event["type"] for event in events]
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    if outcome is None:
        assert event_types[-1] == "response.output_text.done"
        assert assembled[-1] == "response.output_text.done"
        return

    terminal = f"response.{outcome}"
    reasoning_done = event_types.index("response.reasoning_summary_text.done")
    assert event_types[reasoning_done : reasoning_done + 4] == [
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.done",
        "response.output_item.done",
        "response.output_item.added",
    ]
    assert event_types[-4:] == [
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        terminal,
    ]
    response = events[-1]["response"]
    assert response["status"] == outcome
    assert response["output"] == [
        events[reasoning_done + 2]["item"],
        events[-2]["item"],
    ]
    reason_item, text_item = response["output"]
    assert reason_item["summary"][0]["text"] == reasoning
    assert text_item["content"][0]["text"] == text
    assert reason_item["status"] == "completed"
    assert text_item["status"] == item_status
    if finish == "error_frame":
        assert response["error"] == {
            "code": "rate_limit_error",
            "message": "fixture exhausted",
        }
    elif finish == "exception":
        assert response["error"] == {
            "code": "internal_error",
            "message": "ValueError: fixture interrupted",
        }
