"""Replay sanitized browser wire captures through the real protocol pipeline.

Only the network boundary is replaced; byte decoding, wire parsing, application
services and HTTP protocol adapters all execute their production code.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import api_service_gemini
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

CAPTURES: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "har_2026_11_13_generations.json"
    ).read_text(encoding="utf-8")
)


@pytest.fixture(params=CAPTURES, ids=lambda case: case["name"])
def capture_gateway(request, monkeypatch):
    case = request.param
    raw = json.dumps(case["response"], ensure_ascii=False).encode()

    async def send_streaming_request(**kwargs):
        yield "status", 200
        for offset in range(0, len(raw), 7):
            yield "chunk", raw[offset : offset + 7]

    async def send_request(**kwargs):
        if kwargs["url"].endswith("/CountTokens"):
            assert json.loads(kwargs["body"]) == case["count_request"]
            return 200, json.dumps(case["count_response"]).encode()
        return 200, raw

    client = AIStudioClient()
    session = SimpleNamespace(
        ensure_ready=AsyncMock(return_value=SimpleNamespace(auth_user="0")),
        send_request=send_request,
        send_streaming_request=send_streaming_request,
    )
    monkeypatch.setattr(client, "_session", session)
    monkeypatch.setattr(
        client, "_streaming_gateway", StreamingGateway(cast(Any, session))
    )
    monkeypatch.setattr(
        client,
        "prepare_request",
        AsyncMock(
            return_value=PreparedRequest(
                url="https://capture/GenerateContent",
                headers={},
                body=json.dumps(case["request"]),
            )
        ),
    )
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_client] = lambda: client
    try:
        yield case
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["gemini", "chat", "responses", "claude"])
async def test_capture_through_http_adapters(capture_gateway, stream, protocol):
    case = capture_gateway
    model = case["request"][0].removeprefix("models/")
    if protocol == "gemini":
        method = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1beta/models/{model}:{method}"
        body = {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]}
    elif protocol == "responses":
        path = "/v1/responses"
        body = {"model": model, "input": "Hello", "stream": stream}
    else:
        path = "/v1/messages" if protocol == "claude" else "/v1/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": stream,
        }
        if protocol == "claude":
            body["max_tokens"] = 100
        elif stream:
            body["stream_options"] = {"include_usage": True}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, json=body)
    assert response.status_code == 200
    events = (
        [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        if stream
        else [response.json()]
    )
    assert all("error" not in event for event in events)
    usage = case["response"][0][-1][2]
    expected_completion = (usage[1] or 0) + (usage[9] if len(usage) > 9 else 0)
    if protocol == "gemini":
        parts = [
            part
            for event in events
            for candidate in event["candidates"]
            for part in candidate["content"]["parts"]
        ]
        assert (
            "".join(part.get("text", "") for part in parts if not part.get("thought"))
            == "你好，世界🙂"
        )
        assert [
            part["thoughtSignature"] for part in parts if part.get("thoughtSignature")
        ] == [f"sanitized-{case['name']}-signature"]
        assert events[-1]["usageMetadata"]["candidatesTokenCount"] == usage[1]
        assert events[-1]["usageMetadata"]["totalTokenCount"] == usage[2]
        assert events[-1]["candidates"][0]["finishReason"] == "STOP"
    elif protocol == "chat":
        text = (
            "".join(
                choice["delta"].get("content", "")
                for event in events
                for choice in event.get("choices", [])
            )
            if stream
            else events[0]["choices"][0]["message"]["content"]
        )
        assert text == "你好，世界🙂"
        assert events[-1]["usage"]["completion_tokens"] == expected_completion
        assert events[-1]["usage"]["total_tokens"] == usage[2]
    elif protocol == "responses":
        result = events[-1]["response"] if stream else events[0]
        assert result["status"] == "completed"
        assert (
            "".join(
                part.get("text", "")
                for item in result["output"]
                for part in item.get("content", [])
            )
            == "你好，世界🙂"
        )
        assert result["usage"]["output_tokens"] == expected_completion
        assert result["usage"]["total_tokens"] == usage[2]
    else:
        text = (
            "".join(event.get("delta", {}).get("text", "") for event in events)
            if stream
            else "".join(block.get("text", "") for block in events[0]["content"])
        )
        assert text == "你好，世界🙂"
        result = (
            next(event for event in events if event.get("type") == "message_delta")
            if stream
            else events[0]
        )
        assert result["usage"]["output_tokens"] == expected_completion
        assert (result["delta"] if stream else result)["stop_reason"] == "end_turn"


async def test_capture_count_tokens_through_real_client(capture_gateway):
    model = capture_gateway["request"][0].removeprefix("models/")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1beta/models/{model}:countTokens",
            json={"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
        )
    assert response.status_code == 200
    assert response.json()["totalTokens"] == capture_gateway["count_response"][0]


@pytest.mark.parametrize("case", CAPTURES, ids=lambda case: case["name"])
@pytest.mark.parametrize("size", [1, 2, 7, 64, 8192])
def test_capture_parser_and_request_roundtrip(case, size):
    raw = ")]}'\n" + json.dumps(case["response"], ensure_ascii=False)
    parser = IncrementalJSONStreamParser()
    chunks = []
    for offset in range(0, len(raw), size):
        chunks.extend(parser.feed(raw[offset : offset + size]))
    chunks.extend(parser.finish())
    assert chunks == case["response"][0]
    codec = AistudioWireCodec()
    assert (
        json.loads(codec.encode(codec.decode(json.dumps(case["request"]))))
        == case["request"]
    )


def test_cleared_optional_fields_do_not_survive_in_raw_request():
    codec = AistudioWireCodec()
    body = list(CAPTURES[0]["request"])
    body[5] = [[[None, "system"]], "user"]
    body[7] = "models/evergreen"
    body[10] = 1
    body[11] = "session"
    body[12] = {"unknown": "preserved"}
    request = codec.decode(json.dumps(body))
    request.system_instruction = None
    request.tools = None
    request.evergreen_model_uri = None
    request.request_flag = None
    request.session_token = None
    request.location = None
    encoded = json.loads(codec.encode(request))
    assert all(
        index >= len(encoded) or encoded[index] is None
        for index in (5, 6, 7, 10, 11, 13)
    )
    assert encoded[12] == {"unknown": "preserved"}


@pytest.mark.parametrize(
    "payload",
    [
        {"function_call": ("weather", {"city": "Paris"}, "c")},
        {"function_response": ("weather", {"temperature": 20}, "c")},
        {
            "executable_code": ("PYTHON", "print(1)"),
            "code_execution_result": ("OUTCOME_OK", "1"),
        },
        {"inline_data": ("image/png", "aGVsbG8=")},
    ],
)
def test_mixed_parts_keep_text_and_payload_after_decode_encode(payload):
    original = AistudioPart(text="caption", thought_signature="opaque", **payload)
    codec = AistudioWireCodec()
    raw = ["models/test", [[[original.to_wire()], "model"]]]
    assert json.loads(codec.encode(codec.decode(json.dumps(raw))))[1] == raw[1]
