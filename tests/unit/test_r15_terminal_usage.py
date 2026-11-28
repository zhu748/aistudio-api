"""Sparse provider meters belong to one attempt, independent of the client protocol."""

from __future__ import annotations

from typing import Any, cast

import anthropic
import httpx
import httpx2
import openai
import pytest
import test_anti_truncation_variants as variants
import test_ordered_tool_routes as routes
from google import genai
from google.genai import types

from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.session import GatewaySession
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.request_history import request_history

wire_source = routes.wire_source
variant_config = variants.variant_config
_EARLY = [17, 0, 17, 5]
_OUTPUT = [None, 2]
_BASE = "gemini-3.7-flash"
_URL = "http://fixture.invalid"


def _chunk(parts, meter, finish=None):
    chunk: list[object] = [*routes.chunk(parts, finish)]
    if meter is not None:
        chunk.extend([None, meter])
    return chunk


def _bundle(rows, *, wrapped=False):
    chunks = []
    for i, row in enumerate(rows):
        last = i == len(rows) - 1
        parts = [routes.text("b" if last else "a")]
        if wrapped:
            parts = (
                [routes.call("router_emit", {"content": "ab"}, "fixture")]
                if last
                else []
            )
        chunks.append(_chunk(parts, row, 1 if last else None))
    return routes.wire(*chunks)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([_EARLY, _OUTPUT], (17, 5, 2, 0, 19)),
        ([_EARLY, [None, 2, 29]], (17, 5, 2, 0, 29)),
        ([_EARLY, [9, 0, 9, 3], _OUTPUT], (9, 3, 2, 0, 11)),
        ([_EARLY, [9, 0, 9], _OUTPUT], (9, None, 2, 0, 11)),
        ([_EARLY, [0, 2]], (0, None, 2, 0, 2)),
        ([_EARLY, [0, 0, 0, 0], _OUTPUT], (0, 0, 2, 0, 2)),
        ([_EARLY, [17, 2, 19]], (17, None, 2, 0, 19)),
        ([_EARLY, [17, 2, 19, 0]], (17, 0, 2, 0, 19)),
        ([_EARLY, [None, None, None, 7], _OUTPUT], (17, 5, 2, 0, 19)),
        ([_EARLY, [None, 2, None, 7]], (17, 5, 2, 0, 19)),
        (
            [
                [17, 21, 48, 5, None, None, None, None, None, 10],
                [None, 2, None, None, None, None, None, None, None, 3],
            ],
            (17, 5, 2, 3, 22),
        ),
        ([_EARLY, None, [None] * 10], (17, 5, 0, 0, 17)),
        ([None, _OUTPUT], (None, None, 2, 0, None)),
        ([[None, None, None, 5], _OUTPUT], (None, None, 2, 0, None)),
        ([None, [None] * 10], None),
    ],
)
async def test_raw_readers_and_callback(rows, expected):
    source = routes.WireSource()
    source.raw = _bundle(rows)
    callbacks = []
    events = [
        event
        async for event in StreamingGateway(cast(GatewaySession, source)).stream_chat(
            request=PreparedRequest(url=_URL, headers={}, body="[]"),
            model=_BASE,
            on_usage=callbacks.append,
        )
    ]
    assert [kind for kind, _ in events] == [
        *(["body"] * len(rows)),
        "finish_reason",
        "usage",
        "done",
    ]
    assert len(callbacks) == sum(
        row is not None and any(x is not None for x in row) for row in rows
    )
    buffered = parse_text_output(source.raw.decode()).usage
    assert events[-2][1] == (buffered or None) == (callbacks[-1] if callbacks else None)
    if expected is None:
        assert buffered == {}
    else:
        prompt, cached, visible, reasoning, total = expected
        assert buffered == {
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "prompt_tokens_details": None,
            "completion_tokens": visible + reasoning,
            "completion_tokens_details": {
                "visible_tokens": visible,
                "reasoning_tokens": reasoning,
            },
            "total_tokens": total,
        }


@pytest.fixture(
    params=[
        ("", False),
        ("-at", False),
        ("-at", True),
        ("-at-stream", False),
        ("-at-stream", True),
    ]
)
def metered_model(wire_source, variant_config, request):
    suffix, wrapped = request.param
    wire_source.raw = _bundle([_EARLY, _OUTPUT], wrapped=wrapped)
    return _BASE + suffix


@pytest.fixture
def history_probe():
    ids: list[str] = []

    async def remember(response):
        ids.append(response.headers["x-request-id"])

    yield remember
    assert len(set(ids)) == len(ids) == 2  # one JSON request and one SSE request
    for request_id in ids:
        assert _history_counts(request_id) == (17, 5, 2, 19)


def _history_counts(request_id):
    item = request_history().get(request_id)
    assert item and item["result"] == "success"
    usage = item["usage"]
    assert isinstance(usage, dict)
    return tuple(
        usage.get(k)
        for k in ("prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens")
    )


def _headers(model):
    return {"X-Anti-Truncation": "0" if model == _BASE else "1"}


def _options(model, remember) -> dict[str, Any]:
    return {
        "api_key": "fixture",
        "max_retries": 0,
        "_strict_response_validation": True,
        "default_headers": _headers(model),
        "http_client": httpx2.AsyncClient(
            transport=httpx2.ASGITransport(routes.app),
            event_hooks={"response": [remember]},
        ),
    }


async def test_native_sdk_and_history(metered_model, history_probe):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(routes.app),
        event_hooks={"response": [history_probe]},
    ) as http:
        options = types.HttpOptions(
            base_url=_URL,
            api_version="v1beta",
            headers=_headers(metered_model),
            httpx_async_client=http,
        )
        async with genai.Client(api_key="fixture", http_options=options).aio as client:
            message = await client.models.generate_content(
                model=metered_model, contents="Hi"
            )
            frames = [
                f
                async for f in await client.models.generate_content_stream(
                    model=metered_model, contents="Hi"
                )
            ]
    assert message.text == "ab"
    assert all(f.usage_metadata is None for f in frames[:-1])
    for usage in (message.usage_metadata, frames[-1].usage_metadata):
        assert usage is not None
        assert (
            usage.prompt_token_count,
            usage.cached_content_token_count,
            usage.candidates_token_count,
            usage.thoughts_token_count,
            usage.total_token_count,
        ) == (17, 5, 2, 0, 19)


def _claude_counts(usage):
    return usage.input_tokens, usage.cache_read_input_tokens, usage.output_tokens


async def test_claude_sdk_and_history(metered_model, history_probe):
    params: dict[str, Any] = {
        "model": metered_model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 64,
    }
    async with anthropic.AsyncAnthropic(
        base_url=_URL, **_options(metered_model, history_probe)
    ) as client:
        message = await client.messages.create(**params, stream=False)
        starts, deltas = [], []
        async with client.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "message_start":
                    starts.append(_claude_counts(event.message.usage))
                if event.type == "message_delta":
                    deltas.append(_claude_counts(event.usage))
            final = await stream.get_final_message()
    assert starts == [(12, 5, 2 if metered_model.endswith("-at") else 1)]
    assert deltas == [(12, 5, 2)]
    assert _claude_counts(final.usage) == _claude_counts(message.usage) == deltas[0]
    assert final.stop_reason == message.stop_reason == "end_turn"


async def test_openai_chat_sdk_and_history(metered_model, history_probe):
    params: dict[str, Any] = {
        "model": metered_model,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    async with openai.AsyncOpenAI(
        base_url=_URL + "/v1", **_options(metered_model, history_probe)
    ) as client:
        message = await client.chat.completions.create(**params, stream=False)
        async with client.chat.completions.stream(
            **params, stream_options={"include_usage": True}
        ) as stream:
            tails = [
                len(e.chunk.choices)
                async for e in stream
                if e.type == "chunk" and e.chunk.usage
            ]
            final = await stream.get_final_completion()
    assert tails == [0]
    assert (
        final.choices[0].message.content == message.choices[0].message.content == "ab"
    )
    for usage in (message.usage, final.usage):
        assert usage and usage.prompt_tokens_details
        assert (
            usage.prompt_tokens,
            usage.prompt_tokens_details.cached_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        ) == (17, 5, 2, 19)


async def test_responses_sdk_and_history(metered_model, history_probe):
    async with openai.AsyncOpenAI(
        base_url=_URL + "/v1", **_options(metered_model, history_probe)
    ) as client:
        message = await client.responses.create(model=metered_model, input="Hi")
        async with client.responses.stream(model=metered_model, input="Hi") as stream:
            tails = [
                e.type
                async for e in stream
                if getattr(getattr(e, "response", None), "usage", None)
            ]
            final = await stream.get_final_response()
    assert tails == ["response.completed"]
    assert final.output_text == message.output_text == "ab"
    for usage in (message.usage, final.usage):
        assert usage and usage.input_tokens_details
        assert (
            usage.input_tokens,
            usage.input_tokens_details.cached_tokens,
            usage.output_tokens,
            usage.total_tokens,
        ) == (17, 5, 2, 19)


@pytest.mark.parametrize("protocol", routes.PROTOCOLS)
async def test_unpublished_retry_does_not_supply_the_prompt(
    wire_source, monkeypatch, protocol
):
    attempts = 0

    async def send(**_kwargs):
        nonlocal attempts
        attempts += 1
        yield "status", 200
        if attempts == 1:
            yield "chunk", routes.wire(_chunk([], [91, 0, 91, 40]))
            yield "error", "fixture connection closed"
        else:
            yield "chunk", _bundle([None, _OUTPUT])

    monkeypatch.setattr(wire_source, "send_streaming_request", send)
    path, payload = routes.input_request(protocol, True)
    if protocol == "chat":
        payload = {**payload, "stream_options": {"include_usage": True}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(routes.app), base_url=_URL
    ) as http:
        response = await http.post(path, json=payload, headers=_headers(_BASE))
    assert response.status_code == 200 and attempts == 2
    assert _history_counts(response.headers["x-request-id"]) == (None, None, 2, None)
    frames = routes.sse_events(response.text)
    if protocol == "native":
        assert frames[-1]["usageMetadata"] == {
            "promptTokenCount": 0,
            "candidatesTokenCount": 2,
            "thoughtsTokenCount": 0,
            "totalTokenCount": 2,
        }
    elif protocol == "claude":
        assert frames[0]["message"]["usage"] == {"input_tokens": 0, "output_tokens": 1}
        assert frames[-2]["usage"] == {"input_tokens": 0, "output_tokens": 2}
    elif protocol == "chat":
        assert frames[-1]["usage"] == {
            "prompt_tokens": 0,
            "completion_tokens": 2,
            "total_tokens": 2,
        }
    else:
        counts = frames[-1]["response"]["usage"]
        assert (
            counts["input_tokens"],
            counts["input_tokens_details"]["cached_tokens"],
            counts["output_tokens"],
            counts["total_tokens"],
        ) == (0, 0, 2, 2)
