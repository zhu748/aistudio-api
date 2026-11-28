"""Prefetching Claude's start usage must not pin the first frame until EOF."""

import asyncio
import gc
import weakref
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from test_ordered_tool_routes import request

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application.api_service_gemini import GeminiStreamResponse
from aistudio_api.application.compat_claude import claude_stream_adapter


class _Text(str):
    """A weak-referenceable payload for the structured frame path."""


async def test_first_claude_payload_released_while_upstream_open(monkeypatch):
    refs: list[weakref.ReferenceType[_Text]] = []
    paused, resume = asyncio.Event(), asyncio.Event()

    def first_text() -> str:
        text = _Text("a" * 1_048_576)
        refs.append(weakref.ref(text))
        return text

    def content(text: str) -> dict[str, object]:
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        yield {
            **content(first_text()),
            "usageMetadata": {"promptTokenCount": 17, "cachedContentTokenCount": 5},
        }
        for text in ("second", "third", "fourth"):
            yield content(text)
        paused.set()
        await resume.wait()
        yield {
            "candidates": [{"finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 17,
                "cachedContentTokenCount": 5,
                "candidatesTokenCount": 4,
            },
        }

    monkeypatch.setitem(app.dependency_overrides, get_client, object)
    monkeypatch.setattr(
        routes_compat,
        "handle_gemini_generate_content",
        AsyncMock(return_value=GeminiStreamResponse(frames())),
    )
    response = asyncio.create_task(request("claude", True))  # POST /v1/messages
    try:
        await asyncio.wait_for(paused.wait(), timeout=3)
        assert not response.done()
        gc.collect()
        assert len(refs) == 1
        retained = refs[0]() is not None
        assert not retained, "Prefetched first text still owned after the fourth frame"
    finally:
        resume.set()
        events = await asyncio.wait_for(response, timeout=3)

    assert [e["type"] for e in events] == [
        "message_start",
        "ping",
        "content_block_start",
        *["content_block_delta"] * 4,
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    base_usage = {"input_tokens": 12, "cache_read_input_tokens": 5}
    assert events[0]["message"]["usage"] == {**base_usage, "output_tokens": 1}
    assert [e["delta"]["text"] for e in events[3:7]] == [
        "a" * 1_048_576,
        "second",
        "third",
        "fourth",
    ]
    assert events[-2]["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert events[-2]["usage"] == {**base_usage, "output_tokens": 4}


async def test_cancel_during_first_frame_prefetch_closes_source():
    reading = asyncio.Event()

    class Source:
        def __init__(self):
            self.aclose = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self) -> dict[str, object]:
            reading.set()
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")

    source = Source()
    stream = claude_stream_adapter(source, requested_model="local", stop_sequences=None)
    first_event = asyncio.ensure_future(anext(stream))
    try:
        await asyncio.wait_for(reading.wait(), timeout=3)
        assert not first_event.done()
    finally:
        first_event.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_event
        await stream.aclose()
    source.aclose.assert_awaited_once()
