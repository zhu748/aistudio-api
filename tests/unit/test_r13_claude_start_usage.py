"""Only Claude may project known input counts onto its first publishable frame."""

from __future__ import annotations

import asyncio
from pathlib import Path

import anthropic
import httpx
import httpx2 as sdk_http
import pytest
import test_anti_truncation_variants as variants
import test_ordered_tool_routes as routes

from aistudio_api.config import settings

wire_source = routes.wire_source
variant_config = variants.variant_config


def _chunk(parts, usage, finish=None):
    chunk: list[object] = [*routes.chunk(parts, finish)]
    if usage is not None:
        chunk.extend([None, usage])
    return chunk


def _counts(usage: anthropic.types.Usage | anthropic.types.MessageDeltaUsage):
    # The SDK mutates its message_start model as it aggregates later events.
    return usage.input_tokens, usage.cache_read_input_tokens, usage.output_tokens


@pytest.mark.parametrize("buffered", [False, True], ids=["stream", "buffered-at"])
@pytest.mark.parametrize(
    ("meters", "start", "final"),
    [
        ("capture", (5, None, 1), (5, None, 161)),
        (([17, 7, 24, 5], [20, 3, 23, 1]), (12, 5, 1), (19, 1, 3)),
        (([17, 0, 17, 5], [17, 2, 19]), (12, 5, 1), (17, None, 2)),
        (([17, 0, 17, 5], [17, 2, 19, 0]), (12, 5, 1), (17, None, 2)),
        (([17, 0, 17], [17, 2, 19]), (17, None, 1), (17, None, 2)),
        ((None, [17, 2, 19, 5]), (0, None, 1), (12, 5, 2)),
    ],
    ids=["capture", "revised", "cache-omitted", "cache-zero", "no-cache", "late-only"],
)
async def test_sdk_start_and_final_usage(
    wire_source, variant_config, buffered, meters, start, final
):
    if meters == "capture":
        wire_source.raw = (
            Path(__file__).parents[1] / "fixtures" / "test_output.json"
        ).read_bytes()
    else:
        wire_source.raw = routes.wire(
            _chunk([routes.text("first")], meters[0]),
            _chunk([routes.text("last")], meters[1], 1),
        )
    if buffered:
        start = final
    elif start[1] and final[1] is None:
        # Anthropic retains an old field unless a terminal delta resets it.
        final = (final[0], 0, final[2])
    starts = []
    deltas = []
    async with (
        anthropic.AsyncAnthropic(
            api_key="fixture",
            base_url="http://test",
            http_client=sdk_http.AsyncClient(
                transport=sdk_http.ASGITransport(routes.app)
            ),
            _strict_response_validation=True,
            max_retries=0,
        ) as client,
        client.messages.stream(
            model="gemini-3.7-flash" + ("-at" if buffered else ""),
            max_tokens=256,
            messages=[{"role": "user", "content": "Hi"}],
            extra_headers={"X-Anti-Truncation": "0"},
        ) as stream,
    ):
        async for event in stream:
            if event.type == "message_start":
                starts.append(_counts(event.message.usage))
            if event.type == "message_delta":
                deltas.append(_counts(event.usage))
                assert ("cache_read_input_tokens" in event.usage.model_fields_set) == (
                    final[1] is not None
                )
        message = await stream.get_final_message()
    assert starts == [start] and deltas == [final]
    assert _counts(message.usage) == final
    assert message.content and message.stop_reason == "end_turn"


@pytest.mark.parametrize("parts", [[], [routes.call("lookup", {}, "held")]])
@pytest.mark.parametrize("winning_meter", [None, [17, 0, 17, 5]])
async def test_unpublished_attempt_cannot_seed_winners_usage(
    wire_source, monkeypatch, parts, winning_meter
):
    attempts = 0

    async def source(**_kwargs):
        nonlocal attempts
        attempts += 1
        yield "status", 200
        if attempts == 1:
            yield "chunk", routes.wire(_chunk(parts, [91, 0, 91, 40]))
            yield "error", "fixture connection closed"
        else:
            yield (
                "chunk",
                routes.wire(
                    _chunk([routes.text("winning")], winning_meter),
                    _chunk([routes.text(" answer")], [17, 2, 19, 5], 1),
                ),
            )

    monkeypatch.setattr(wire_source, "send_streaming_request", source)
    events = await routes.request("claude", True)
    assert attempts == 2 and "winning" in str(events)
    assert not routes.tool_calls("claude", True, events)
    assert [e["message"]["usage"] for e in events if e["type"] == "message_start"] == [
        {"input_tokens": 12, "cache_read_input_tokens": 5, "output_tokens": 1}
        if winning_meter
        else {"input_tokens": 0, "output_tokens": 1}
    ]
    assert events[-2]["usage"] == {
        "input_tokens": 12,
        "cache_read_input_tokens": 5,
        "output_tokens": 2,
    }


@pytest.mark.parametrize("empty", [False, True], ids=["first-text", "empty-diagnostic"])
async def test_pending_meter_is_latest_coherent_snapshot(wire_source, empty):
    wire_source.raw = routes.wire(
        _chunk([], [91, 0, 91, 40]),
        _chunk([], [23, 0, 23]),
        _chunk([] if empty else [routes.text("answer")], None, 1),
    )
    events = await routes.request("claude", True)
    assert events[0]["message"]["usage"] == {"input_tokens": 23, "output_tokens": 1}
    assert events[-2]["usage"] == {"input_tokens": 23, "output_tokens": 0}


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ([[17, 0, 17, 5], [None, None, None, 5]], (12, 5)),
        ([[17, 0, 17, 5], [None, None, None, 7]], (12, 5)),
        ([[None, None, None, 5]], (0, 5)),
        ([[None, None, None, 5], [23, 0, 23]], (23, None)),
    ],
)
async def test_sparse_prepublication_meter_does_not_corrupt_prompt_pair(
    wire_source, prefix, expected
):
    wire_source.raw = routes.wire(
        *(_chunk([], meter) for meter in prefix),
        _chunk([routes.text("first")], None),
        _chunk([routes.text("last")], [23, 2, 25], 1),
    )
    start = (await routes.request("claude", True))[0]["message"]["usage"]
    assert (start["input_tokens"], start.get("cache_read_input_tokens")) == expected


@pytest.mark.parametrize("visible", [False, True], ids=["held-call", "live-text"])
@pytest.mark.parametrize("ending", ["complete", "cancel", "deadline"])
async def test_usage_waits_for_publication_not_for_another_wire_chunk(
    wire_source, monkeypatch, visible, ending
):
    waiting, release = asyncio.Event(), asyncio.Event()
    parts = [routes.text("live")] if visible else []
    parts.append(routes.call("lookup", {}, "held"))
    first = routes.wire(_chunk(parts, [17, 0, 17, 5]))[:-2] + b","
    last = routes.wire(_chunk([routes.text("end")], [17, 2, 19, 5], 1))[2:]
    closed = 0

    async def source(**_kwargs):
        nonlocal closed
        try:
            yield "status", 200
            yield "chunk", first
            waiting.set()
            await release.wait()
            yield "chunk", last
        finally:
            closed += 1

    monkeypatch.setattr(wire_source, "send_streaming_request", source)
    packets = []
    checkpoint = asyncio.Event()
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)

    async def observed(scope, receive, send):
        async def watch(message):
            if message["type"] == "http.response.body":
                packet = message["body"]
                packets.append(packet)
                if (visible and b'"text":"live"' in packet) or (
                    not visible
                    and waiting.is_set()
                    and len(packets) > 1
                    and packet == b": keepalive\n\n"
                ):
                    checkpoint.set()
            await send(message)

        await routes.app(scope, receive, watch)

    # Fire a real middleware timeout only after reaching the desired boundary.
    original_timeout = asyncio.timeout
    budget = 23.5
    deadline = original_timeout(budget)
    monkeypatch.setattr(settings, "request_timeout", budget)
    monkeypatch.setattr(
        asyncio, "timeout", lambda d: deadline if d == budget else original_timeout(d)
    )
    path, payload = routes.input_request("claude", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(observed), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(path, json=payload, headers={"X-Anti-Truncation": "0"})
        )
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            await asyncio.wait_for(checkpoint.wait(), 3)
            assert packets[0] == b": keepalive\n\n"
            before = routes.sse_events(b"".join(packets).decode())
            if visible:
                assert before[0]["message"]["usage"] == {
                    "input_tokens": 12,
                    "cache_read_input_tokens": 5,
                    "output_tokens": 1,
                }
                assert before[1]["type"] == "ping"
                assert before[-1]["delta"]["text"] == "live"
            else:
                assert before == []
            assert not routes.tool_calls("claude", True, before)
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if ending == "deadline":
                    deadline.reschedule(asyncio.get_running_loop().time())
                else:
                    release.set()
                response = await asyncio.wait_for(task, 3)
                assert response.status_code == 200
            events = routes.sse_events(b"".join(packets).decode())
            assert closed == 1
            if ending == "complete":
                assert events[-1]["type"] == "message_stop"
                assert len(routes.tool_calls("claude", True, events)) == 1
            else:
                assert not routes.tool_calls("claude", True, events)
                assert not any(
                    e["type"] in {"message_delta", "message_stop"} for e in events
                )
                if ending == "deadline":
                    assert events[-1]["type"] == "error"
        finally:
            release.set()
            if not task.done():
                await asyncio.wait_for(task, 3)


@pytest.mark.parametrize("protocol", ["native", "chat", "responses"])
async def test_other_routes_keep_usage_terminal_only(wire_source, protocol):
    wire_source.raw = routes.wire(
        _chunk([routes.text("first")], [17, 0, 17, 5]),
        _chunk([routes.text("last")], [17, 2, 19, 5], 1),
    )
    path, payload = routes.input_request(protocol, True)
    if protocol == "chat":
        payload = {**payload, "stream_options": {"include_usage": True}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(routes.app), base_url="http://test"
    ) as client:
        response = await client.post(
            path, json=payload, headers={"X-Anti-Truncation": "0"}
        )
    assert response.status_code == 200
    frames = routes.sse_events(response.text)
    if protocol == "native":
        assert len(frames) == 3
        assert routes.native_parts(frames) == [{"text": "first"}, {"text": "last"}]
        assert all("usageMetadata" not in frame for frame in frames[:-1])
        assert frames[-1]["usageMetadata"]["promptTokenCount"] == 17
        assert frames[-1]["usageMetadata"]["cachedContentTokenCount"] == 5
    elif protocol == "chat":
        assert all(frame.get("usage") is None for frame in frames[:-1])
        assert frames[-1]["choices"] == []
        assert frames[-1]["usage"]["prompt_tokens"] == 17
        assert frames[-1]["usage"]["prompt_tokens_details"]["cached_tokens"] == 5
    else:
        assert all(
            frame.get("response", {}).get("usage") is None for frame in frames[:-1]
        )
        assert frames[-1]["type"] == "response.completed"
        usage = frames[-1]["response"]["usage"]
        assert (
            usage["input_tokens"] == 17
            and usage["input_tokens_details"]["cached_tokens"] == 5
        )
