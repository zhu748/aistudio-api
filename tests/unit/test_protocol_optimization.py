"""Protocol contracts and resource lifetime regressions from the full review."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request

from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application.chat_service import (
    encode_schema_to_wire,
    normalize_gemini_request,
)
from aistudio_api.application.compat_service import (
    _openai_finish_reason,
    build_claude_message,
    build_openai_completion,
    claude_stream_adapter,
    claude_to_gemini_request,
    openai_stream_adapter,
    openai_to_gemini_request,
)
from aistudio_api.application.responses_service import (
    build_responses_object,
    responses_stream_adapter,
    responses_to_gemini_request,
)
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport
from aistudio_api.infrastructure.gateway.wire_spec import SchemaIndex
from aistudio_api.infrastructure.request_history import request_history


def frame(parts: list[dict], finish: str | None = None) -> dict:
    candidate = {"content": {"parts": parts}, "index": 0}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


async def source(frames: list[dict]) -> AsyncGenerator[object, None]:
    for item in frames:
        yield item  # Structured path used by real routes.


async def convert(protocol: str, frames: list[dict], stops=None) -> list[dict]:
    if protocol == "claude":
        stream = claude_stream_adapter(
            source(frames), requested_model="test", stop_sequences=stops
        )
    elif protocol == "responses":
        stream = responses_stream_adapter(source(frames), requested_model="test")
    else:
        stream = openai_stream_adapter(
            source(frames), requested_model="test", include_usage=True
        )
    result = []
    async for event in stream:
        for line in event.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                result.append(json.loads(line[6:]))
    return result


def calls_from(protocol: str, events: list[dict]) -> list[dict]:
    if protocol == "claude":
        return [
            e["content_block"]
            for e in events
            if e.get("content_block", {}).get("type") == "tool_use"
        ]
    if protocol == "responses":
        return [
            e["item"]
            for e in events
            if e.get("type") == "response.output_item.done"
            and e["item"]["type"] == "function_call"
        ]
    return [
        t
        for e in events
        for c in e.get("choices", [])
        for t in c["delta"].get("tool_calls", [])
    ]


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_independent_idless_calls_keep_names_and_arguments(protocol):
    events = await convert(
        protocol,
        [
            frame(
                [
                    {"functionCall": {"name": "weather", "args": {"city": "Paris"}}},
                    {"functionCall": {"name": "weather", "args": {"city": "Tokyo"}}},
                    {"functionCall": {"name": "time", "args": {"zone": "UTC"}}},
                ],
                "STOP",
            )
        ],
    )
    calls = calls_from(protocol, events)
    assert len(calls) == 3
    ids = [c.get("id") for c in calls]
    assert len(set(ids)) == 3
    if protocol == "chat":
        assert [json.loads(c["function"]["arguments"]) for c in calls] == [
            {"city": "Paris"},
            {"city": "Tokyo"},
            {"zone": "UTC"},
        ]


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize(
    "arguments",
    [
        ['{"city":"Par', 'is"}'],
        ['{"city":"Par', '{"city":"Paris"}'],
        [{"city": "Par"}, {"city": "Paris"}],
    ],
)
async def test_tool_shards_snapshots_and_replays_produce_one_call(protocol, arguments):
    frames = [
        frame([{"functionCall": {"id": "stable", "name": "weather", "args": args}}])
        for args in arguments
    ]
    frames.append(frame([], "STOP"))
    calls = calls_from(protocol, await convert(protocol, frames))
    assert len(calls) == 1 and calls[0].get("id", calls[0].get("call_id"))
    if protocol == "chat":
        assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Paris"}
    elif protocol == "responses":
        assert json.loads(calls[0]["arguments"]) == {"city": "Paris"}


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_invalid_shards_are_errors_and_never_executable_calls(protocol):
    events = await convert(
        protocol,
        [
            frame(
                [
                    {
                        "functionCall": {
                            "id": "c",
                            "name": "write",
                            "args": '{"path":"partial',
                        }
                    }
                ],
                "STOP",
            )
        ],
    )
    assert not calls_from(protocol, events)
    assert any("error" in e or e.get("type") == "response.failed" for e in events)


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_replayed_completed_tool_call_is_not_executed_again(protocol):
    call = {"id": "stable", "name": "weather", "args": {"city": "Paris"}}
    events = await convert(
        protocol,
        [
            frame([{"functionCall": call, "thoughtSignature": "signature"}]),
            frame([{"text": "checking"}]),
            frame([{"functionCall": call}], "STOP"),
        ],
    )
    assert len(calls_from(protocol, events)) == 1
    assert not any("error" in e or e.get("type") == "response.failed" for e in events)


@pytest.mark.parametrize(
    "pieces,stops,expected",
    [
        (["hello <EN", "D> secret"], ["<END>"], "hello "),
        (["a Y b X tail"], ["X", "Y"], "a "),
        (["a <EN"], ["<END>"], "a <EN"),
    ],
)
async def test_stop_sequences_across_frames_and_earliest_match(pieces, stops, expected):
    events = await convert(
        "claude", [*[frame([{"text": p}]) for p in pieces], frame([], "STOP")], stops
    )
    assert "".join(e.get("delta", {}).get("text", "") for e in events) == expected
    response = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": p} for p in pieces]},
                    "finishReason": "STOP",
                }
            ]
        }
    )
    result = cast(dict[str, Any], build_claude_message(response, "test", stops))
    assert "".join(b.get("text", "") for b in result["content"]) == expected


@pytest.mark.parametrize("finish", ["OTHER", "MAX_TOKENS", "FUTURE_REASON"])
def test_incomplete_tool_response_cannot_be_reported_as_success(finish):
    assert _openai_finish_reason(finish, has_tool_calls=True) == "length"


@pytest.mark.parametrize(
    "finish",
    ["MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "MISSING_THOUGHT_SIGNATURE"],
)
async def test_responses_tool_errors_are_incomplete(finish):
    events = await convert("responses", [frame([], finish)])
    assert events[-1]["type"] == "response.incomplete"


async def test_claude_disabled_thinking_suppresses_profile_defaults():
    request, model = await claude_to_gemini_request(
        ClaudeMessagesRequest.model_validate(
            {
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "disabled"},
            }
        )
    )
    normalized = normalize_gemini_request(request, model)
    assert normalized.generation_config_overrides is not None
    assert "thinking_config" in normalized.generation_config_overrides
    assert normalized.generation_config_overrides["thinking_config"] is None


async def test_previous_response_id_is_rejected_instead_of_losing_history():
    with pytest.raises(ValueError, match="complete conversation"):
        await responses_to_gemini_request(
            OpenAIResponsesRequest(
                model="test", input="next", previous_response_id="resp_old"
            )
        )


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_tool_signature_survives_full_history_roundtrip(protocol):
    response = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "weather",
                                    "args": {},
                                    "id": "c",
                                },
                                "thoughtSignature": "opaque_signature",
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    if protocol == "chat":
        completion = cast(dict[str, Any], build_openai_completion(response, "test"))
        request, _ = await openai_to_gemini_request(
            OpenAIChatCompletionRequest.model_validate(
                {"model": "test", "messages": [completion["choices"][0]["message"]]}
            )
        )
    elif protocol == "claude":
        message = build_claude_message(response, "test", None)
        request, _ = await claude_to_gemini_request(
            ClaudeMessagesRequest.model_validate(
                {
                    "model": "test",
                    "messages": [{"role": "assistant", "content": message["content"]}],
                }
            )
        )
    else:
        result = build_responses_object(response, "test")
        request, _ = await responses_to_gemini_request(
            OpenAIResponsesRequest.model_validate(
                {"model": "test", "input": result["output"]}
            )
        )
    assert request.contents[0].parts[0].thoughtSignature == "opaque_signature"
    events = await convert(
        protocol,
        [
            frame(
                [
                    {
                        "functionCall": {"id": "c", "name": "weather", "args": {}},
                        "thoughtSignature": "opaque_signature",
                    }
                ],
                "STOP",
            )
        ],
    )
    assert (
        calls_from(protocol, events)[0]["extra_content"]["google"]["thought_signature"]
        == "opaque_signature"
    )


def test_schema_refs_resolve_required_fields_and_escaped_json_pointer():
    schema = {
        "type": "object",
        "$defs": {
            "A/B": {
                "type": "object",
                "properties": {"street": {"type": "string"}},
                "required": ["street"],
            }
        },
        "properties": {"address": {"$ref": "#/$defs/A~1B"}},
    }
    child = cast(list[Any], encode_schema_to_wire(schema)[SchemaIndex.PROPERTIES])[0][1]
    assert child[SchemaIndex.REQUIRED] == ["street"]
    assert child[SchemaIndex.PROPERTIES][0][0] == "street"


def test_schema_ref_named_property_is_literal_and_not_a_reference():
    wire = encode_schema_to_wire(
        {
            "type": "object",
            "properties": {"$ref": {"type": "string"}, "$defs": {"type": "integer"}},
        }
    )
    assert wire[SchemaIndex.PROPERTIES] == [["$ref", [1]], ["$defs", [3]]]


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.com/schema"},
        {"$ref": "#/$defs/missing"},
        {"$defs": {"A": {"$ref": "#/$defs/A"}}, "$ref": "#/$defs/A"},
    ],
)
def test_unsupported_schema_refs_fail_explicitly(schema):
    with pytest.raises(ValueError):
        encode_schema_to_wire(schema)


def test_parser_handles_large_sharded_string_without_retaining_consumed_data():
    raw = '[[["' + "a" * (2 * 1024 * 1024) + '"]]]'
    parser = IncrementalJSONStreamParser()
    result = []
    for index in range(0, len(raw), 1024):
        result.extend(parser.feed(raw[index : index + 1024]))
    assert result == [["a" * (2 * 1024 * 1024)]]
    assert not parser.buffer


async def test_responses_body_limit_history_and_protocol(monkeypatch):
    monkeypatch.setattr(settings, "request_history", True)
    app = FastAPI()
    app.add_middleware(RequestMiddleware)

    @app.post("/v1/responses")
    async def generate(request: Request):
        await request.json()
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/responses", json={"model": "test", "input": "a" * 1_100_000}
        )
    assert response.status_code == 200
    record = request_history().get(response.headers["x-request-id"])
    assert record is not None and record["protocol"] == "openai"


async def test_concurrency_rejects_before_history_io(monkeypatch):
    import aistudio_api.api.request_middleware as module

    writes = AsyncMock()
    monkeypatch.setattr(module.asyncio, "to_thread", writes)
    monkeypatch.setattr(settings, "max_concurrent_requests", 1)
    middleware = RequestMiddleware(FastAPI())
    middleware.active = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(middleware), base_url="http://test"
    ) as client:
        response = await client.post("/v1/responses", json={})
    assert (
        response.status_code == 429
        and response.json()["error"]["type"] == "rate_limit_error"
    )
    writes.assert_not_called()
    assert middleware.active == 1


async def test_retired_pool_waits_for_active_stream_and_closes_when_drained():
    transport = HTTPStreamTransport()
    async with transport._lease_client("http://old:1") as client:
        await transport.retire_stale_clients("http://new:2")
        await asyncio.sleep(0)
        assert not client.is_closed
    await asyncio.gather(*transport._retired_close_tasks)
    assert client.is_closed
    await transport.close()


async def test_transport_cannot_create_connections_after_close():
    transport = HTTPStreamTransport()
    await transport.close()
    with pytest.raises(RuntimeError, match="closed"):
        await transport._get_client()


async def test_byte_sse_chunks_preserve_utf8_and_malformed_frames_fail():
    from aistudio_api.application.compat_service import _iter_gemini_sse_frames

    raw = (
        "data: " + json.dumps(frame([{"text": "中文🙂"}]), ensure_ascii=False) + "\n\n"
    ).encode()

    async def bytes_source():
        for byte in raw:
            yield bytes([byte])

    frames = [
        cast(dict[str, Any], f) async for f in _iter_gemini_sse_frames(bytes_source())
    ]
    assert frames[0]["candidates"][0]["content"]["parts"][0]["text"] == "中文🙂"

    async def malformed():
        yield "data: {broken}\n\n"

    with pytest.raises(ValueError, match="Invalid"):
        [f async for f in _iter_gemini_sse_frames(malformed())]


async def test_late_native_signature_survives_and_can_be_returned_in_history(
    monkeypatch,
):
    from types import SimpleNamespace

    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.application import api_service_gemini as service

    monkeypatch.setattr(service, "ensure_active_account", AsyncMock())

    async def generate(**kwargs):
        yield "body", "answer"
        yield "thought_signature", "late-signature"
        yield "finish_reason", 1
        yield "usage", None

    response = service._build_gemini_streaming_response(
        client=cast(Any, SimpleNamespace(stream_generate_content=generate)),
        req=GeminiGenerateContentRequest.model_validate(
            {"contents": [{"parts": [{"text": "hi"}]}]}
        ),
        model_path="gemini-3.8-flash",
        anti_truncation=False,
    )
    assert isinstance(response, service.GeminiStreamResponse)
    frames = [f async for f in response.frames]
    parts = [
        p
        for f in frames
        for c in cast(list[dict], f.get("candidates", []))
        for p in c["content"]["parts"]
    ]
    assert any(p.get("thoughtSignature") == "late-signature" for p in parts)
    request = GeminiGenerateContentRequest.model_validate(
        {"contents": [{"role": "model", "parts": parts}]}
    )
    normalized = normalize_gemini_request(request, "test")
    assert any(
        p.thought_signature == "late-signature" for p in normalized.contents[0].parts
    )


async def test_real_claude_thinking_signature_roundtrips_but_placeholder_is_not_forwarded():
    response = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "reason",
                                "thought": True,
                                "thoughtSignature": "real-signature",
                            }
                        ]
                    }
                }
            ]
        }
    )
    message = build_claude_message(response, "test", None)
    request, _ = await claude_to_gemini_request(
        ClaudeMessagesRequest.model_validate(
            {
                "model": "test",
                "messages": [{"role": "assistant", "content": message["content"]}],
            }
        )
    )
    assert request.contents[0].parts[0].thoughtSignature == "real-signature"
