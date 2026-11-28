"""A possible stop prefix is literal when the source ends, even in error."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext

import anthropic
import httpx2
import openai
import pytest
import test_ordered_tool_routes as routes
from test_responses_failure_sdk import _arm_wire_deadline, _ASGIObserver

from aistudio_api.application.compat_service import (
    claude_stream_adapter,
    openai_stream_adapter,
)
from aistudio_api.config import settings

wire_source = routes.wire_source
_STOP = "<END>"
_HEADERS = {"X-Anti-Truncation": "0"}


@pytest.mark.parametrize("protocol", ["chat", "claude"])
@pytest.mark.parametrize(
    ("parts", "ending", "expected"),
    [
        (["Three <", "EN"], "error", "Three <EN"),
        (["<E", "N"], "error", "<EN"),
        (["Three <EN", "D>hidden"], "error", "Three "),
        (["Three <EN"], "stop", "Three <EN"),
        (["Three <EN"], "eof", "Three <EN"),
        (["Three <EN", "D>hidden"], "stop", "Three "),
    ],
)
async def test_sdk_receives_decoded_text_before_original_terminal(
    monkeypatch, wire_source, protocol, parts, ending, expected
):
    calls = closed = 0
    wire_source.raw = routes.wire(
        *(
            routes.chunk(
                [routes.text(p)],
                1 if ending == "stop" and i == len(parts) - 1 else None,
            )
            for i, p in enumerate(parts)
        )
    )
    fragmented = wire_source.send_streaming_request

    async def source(**kwargs):
        nonlocal calls, closed
        calls += 1
        try:
            async for item in fragmented(**kwargs):
                yield item
            if ending == "error":
                yield "error", "fixture read interrupted"
        finally:
            closed += 1

    monkeypatch.setattr(wire_source, "send_streaming_request", source)
    monkeypatch.setattr(settings, "request_history", False)
    raw = bytearray()
    observer = _ASGIObserver(lambda m: raw.extend(m.get("body", b"")))
    text = []
    failed = ending == "error"
    if protocol == "chat":
        with (
            pytest.raises(openai.APIError, match="fixture read interrupted")
            if failed
            else nullcontext()
        ):
            async with (
                observer.client() as sdk,
                sdk.chat.completions.stream(
                    model=routes.MODEL,
                    messages=[{"role": "user", "content": "Count."}],
                    stop=[_STOP],
                    stream_options={"include_usage": True},
                    extra_headers=_HEADERS,
                ) as stream,
            ):
                async for event in stream:
                    if event.type == "content.delta":
                        text.append(event.delta)
    else:
        with (
            pytest.raises(anthropic.APIStatusError, match="fixture read interrupted")
            if failed
            else nullcontext()
        ):
            async with (
                anthropic.AsyncAnthropic(
                    api_key="fixture",
                    base_url="http://fixture.invalid",
                    http_client=httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(observer)
                    ),
                    max_retries=0,
                    _strict_response_validation=True,
                ) as sdk,
                sdk.messages.stream(
                    model=routes.MODEL,
                    messages=[{"role": "user", "content": "Count."}],
                    max_tokens=64,
                    stop_sequences=[_STOP],
                    extra_headers=_HEADERS,
                ) as stream,
            ):
                async for event in stream:
                    if event.type == "text":
                        text.append(event.text)
    assert observer.statuses == [200] and calls == closed == 1
    assert "".join(text) == expected
    events = routes.sse_events(raw.decode())
    if protocol == "chat":
        assert raw.count(b"data: [DONE]\n\n") == 1 and raw.endswith(b"data: [DONE]\n\n")
        assert [
            c["finish_reason"]
            for e in events
            if "error" not in e
            for c in e["choices"]
            if c["finish_reason"]
        ] == ([] if failed else ["length" if ending == "eof" else "stop"])
        assert sum(e.get("usage") is not None for e in events) == int(not failed)
        if failed:
            assert events[-1]["error"]["code"] == 502
            assert events[-1]["error"]["type"] == "api_error"
    else:
        kinds = [e["type"] for e in events]
        assert (
            kinds.count("content_block_start") == kinds.count("content_block_stop") == 1
        )
        if failed:
            assert kinds[-2:] == ["content_block_stop", "error"]
            assert "message_delta" not in kinds and "message_stop" not in kinds
            assert events[-1]["error"]["type"] == "api_error"
        else:
            assert kinds[-2:] == ["message_delta", "message_stop"]
            matched = _STOP in "".join(parts)
            reason = "stop_sequence" if matched else "end_turn"
            assert events[-2]["delta"] == {
                "stop_reason": "max_tokens" if ending == "eof" else reason,
                "stop_sequence": _STOP if matched else None,
            }


@pytest.mark.parametrize("protocol", ["chat", "claude"])
async def test_conversion_error_flushes_only_previously_decoded_text(protocol):
    closed = 0

    async def source():
        nonlocal closed
        try:
            yield {"candidates": [{"content": {"parts": [{"text": "<EN"}]}}]}
            yield 'data: {"invalid":"remaining\n\n'
        finally:
            closed += 1

    stream = (
        openai_stream_adapter(
            source(),
            requested_model="model",
            include_usage=True,
            stop_sequences=[_STOP],
        )
        if protocol == "chat"
        else claude_stream_adapter(
            source(), requested_model="model", stop_sequences=[_STOP]
        )
    )
    events = routes.sse_events("".join([item async for item in stream]))
    assert closed == 1
    if protocol == "chat":
        assert events[-2]["choices"][0]["delta"] == {"content": "<EN"}
        assert events[-1]["error"]["code"] == 500
        assert events[-1]["error"]["type"] == "internal_error"
    else:
        assert events[-3]["delta"] == {"type": "text_delta", "text": "<EN"}
        assert events[-2]["type"] == "content_block_stop"
    assert events[-1]["error"]["message"] == "ValueError: Invalid Gemini SSE JSON frame"


@pytest.mark.parametrize("protocol", ["chat", "claude"])
async def test_deadline_while_source_is_open_does_not_publish_pending_text(
    wire_source, monkeypatch, protocol
):
    # The next wire chunk would complete the stop. A downstream deadline must
    # not publish the prefix simply because it cancels this pending read.
    prefix = routes.wire(routes.chunk([routes.text("Three <EN")]))[:-2] + b","
    wire_source.raw = (
        prefix + routes.wire(routes.chunk([routes.text("D>hidden")], 1))[2:]
    )
    wire_source.chunk_boundary = len(prefix)
    wire_source.pause = asyncio.Event()
    expire = _arm_wire_deadline(monkeypatch)

    def after_send(message):
        if b'"Three "' in message.get("body", b""):
            expire()

    observer = _ASGIObserver(after_send)
    path, params = routes.input_request(protocol, True)
    stop_field = "stop" if protocol == "chat" else "stop_sequences"
    async with (
        asyncio.timeout(4),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(observer), base_url="http://fixture.invalid"
        ) as client,
    ):
        response = await client.post(
            path, json={**params, stop_field: [_STOP]}, headers=_HEADERS
        )
    assert observer.statuses == [200]
    assert '"Three "' in response.text and "<EN" not in response.text
    assert "total timeout" in routes.sse_events(response.text)[-1]["error"]["message"]
