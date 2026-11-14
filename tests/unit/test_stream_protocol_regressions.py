"""Protocol and resource bounds reproduced during the streaming audit."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any, cast

import httpx
import pytest

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application import compat_service
from aistudio_api.application.compat_service import (
    _parse_gemini_sse_frames,
    build_claude_message,
    build_openai_completion,
    claude_stream_adapter,
    openai_stream_adapter,
)
from aistudio_api.application.responses_service import (
    build_responses_object,
    responses_stream_adapter,
)
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport


def frame(parts: list[dict[str, object]], finish: str = "STOP") -> dict[str, object]:
    return {"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}


async def source(items: list[object]) -> AsyncGenerator[object, None]:
    for item in items:
        yield item


async def convert(protocol: str, frames: list[object]) -> list[dict[str, Any]]:
    if protocol == "claude":
        stream = claude_stream_adapter(
            source(frames), requested_model="test", stop_sequences=None
        )
    elif protocol == "responses":
        stream = responses_stream_adapter(source(frames), requested_model="test")
    else:
        stream = openai_stream_adapter(
            source(frames), requested_model="test", include_usage=False
        )
    return [
        json.loads(line[6:])
        async for event in stream
        for line in event.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("fragment_size", [1, 7, 4096])
async def test_sse_line_endings_multiline_data_and_utf8(newline, fragment_size):
    first = frame([{"text": "中文\u2028\u2029\u0085🙂"}])
    second = frame([{"text": "next"}])
    raw = (
        ": keepalive"
        + newline
        + newline
        + "data: "
        + json.dumps(first, ensure_ascii=False)
        + newline
        + newline
        + "data: {"
        + newline
        + 'data: "candidates": '
        + json.dumps(second["candidates"])
        + "}"
        + newline
        + newline
    ).encode()
    chunks: list[object] = [
        raw[offset : offset + fragment_size]
        for offset in range(0, len(raw), fragment_size)
    ]
    assert [f async for f in _parse_gemini_sse_frames(source(chunks))] == [
        first,
        second,
    ]


@pytest.mark.parametrize("terminated", [False, True])
@pytest.mark.parametrize("text", ["a" * 256, "中" * 100])
async def test_sse_size_limit_includes_complete_events_and_utf8_bytes(
    monkeypatch, terminated, text
):
    monkeypatch.setattr(compat_service, "_MAX_SSE_EVENT_BYTES", 200)
    raw = "data: " + json.dumps(frame([{"text": text}]), ensure_ascii=False)
    if terminated:
        raw += "\n\n"
    with pytest.raises(ValueError, match="size limit"):
        [f async for f in _parse_gemini_sse_frames(source([raw]))]


async def test_sse_size_limit_is_per_event_in_a_single_network_chunk(monkeypatch):
    monkeypatch.setattr(compat_service, "_MAX_SSE_EVENT_BYTES", 200)
    item = frame([{"text": "hi"}])
    raw = ("data: " + json.dumps(item) + "\n\n") * 10
    assert [f async for f in _parse_gemini_sse_frames(source([raw]))] == [item] * 10


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize("finish", ["SAFETY", "MAX_TOKENS", "FUTURE_REASON"])
@pytest.mark.parametrize("pending", [False, True])
async def test_interrupted_calls_never_flush_at_a_text_boundary(
    protocol, finish, pending
):
    call: dict[str, object] = {
        "functionCall": {"id": "c", "name": "write", "args": {"path": "x"}}
    }
    frames: list[object] = []
    if pending:
        frames.append(frame([call], finish=""))
    frames.append(
        frame([{"text": "partial"}] if pending else [call, {"text": "partial"}], finish)
    )
    events = await convert(protocol, frames)
    assert not any(
        tool
        for event in events
        for choice in event.get("choices", [])
        for tool in choice["delta"].get("tool_calls", [])
    )
    assert not any(e.get("content_block", {}).get("type") == "tool_use" for e in events)
    assert not any(e.get("item", {}).get("type") == "function_call" for e in events)


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize("finish", ["SAFETY", "MAX_TOKENS", "FUTURE_REASON"])
def test_non_streaming_interrupted_calls_are_not_executable(protocol, finish):
    response = GeminiGenerateContentResponse.model_validate(
        frame(
            [
                {"functionCall": {"id": "c", "name": "write", "args": {"path": "x"}}},
                {"text": "partial"},
            ],
            finish,
        )
    )
    if protocol == "chat":
        result = cast(dict[str, Any], build_openai_completion(response, "test"))
        assert not result["choices"][0]["message"].get("tool_calls")
    elif protocol == "claude":
        result = cast(dict[str, Any], build_claude_message(response, "test", None))
        assert all(block["type"] != "tool_use" for block in result["content"])
    else:
        result = cast(dict[str, Any], build_responses_object(response, "test"))
        assert all(item["type"] != "function_call" for item in result["output"])


@pytest.mark.parametrize("protocol", ["claude", "responses"])
@pytest.mark.parametrize("finish", ["FUTURE_REASON", "FINISH_REASON_UNSPECIFIED"])
async def test_unknown_and_unspecified_finish_do_not_signal_success(protocol, finish):
    events = await convert(protocol, [frame([{"text": "partial"}], finish)])
    response = GeminiGenerateContentResponse.model_validate(
        frame([{"text": "partial"}], finish)
    )
    if protocol == "claude":
        assert (
            next(
                e["delta"]["stop_reason"]
                for e in events
                if e["type"] == "message_delta"
            )
            == "max_tokens"
        )
        assert (
            build_claude_message(response, "test", None)["stop_reason"] == "max_tokens"
        )
    else:
        assert events[-1]["type"] == "response.incomplete"
        assert build_responses_object(response, "test")["status"] == "incomplete"


async def test_error_response_read_is_bounded_and_closes_upstream():
    read = 0
    closed = False

    class ErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal read
            for _ in range(100):
                read += 1
                yield b"x" * 4096

        async def aclose(self):
            nonlocal closed
            closed = True

    transport = HTTPStreamTransport()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(502, stream=ErrorStream())
        )
    )
    transport._clients[transport.proxy_url or ""] = client
    try:
        events = [
            event
            async for event in transport.send_streaming_request(
                url="https://example.com/api", headers={}, body="{}"
            )
        ]
    finally:
        await transport.close()
    assert events[0] == ("status", 502)
    assert (
        sum(
            len(payload)
            for tag, payload in events
            if tag == "chunk" and isinstance(payload, bytes)
        )
        == 65536
    )
    assert read == 16
    assert closed
