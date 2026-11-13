"""OpenAI Responses API 兼容层测试。"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import cast

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiFunctionCallPayload,
    GeminiGenerateContentResponse,
    GeminiPartResponse,
    GeminiUsageMetadata,
)
from aistudio_api.api.schemas_compat import OpenAIResponsesRequest
from aistudio_api.application.responses_service import (
    build_responses_object,
    responses_stream_adapter,
    responses_to_gemini_request,
)


def _sse_frames(frames: list[dict[str, object]]) -> AsyncGenerator[str, None]:
    async def gen() -> AsyncGenerator[str, None]:
        for frame in frames:
            yield "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"

    return gen()


def _sample_response(
    *,
    finish_reason: str = "STOP",
    thought: str | None = None,
) -> GeminiGenerateContentResponse:
    parts: list[GeminiPartResponse] = []
    if thought:
        parts.append(GeminiPartResponse(text=thought, thought=True))
    parts.append(GeminiPartResponse(text="Hello world"))
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(role="model", parts=parts),
                finishReason=finish_reason,
            )
        ],
        usageMetadata=GeminiUsageMetadata(
            promptTokenCount=10,
            candidatesTokenCount=20,
            totalTokenCount=30,
            thoughtsTokenCount=4,
            cachedContentTokenCount=2,
        ),
    )


# ---------------------------------------------------------------------------
# 请求转换
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_string_input_and_instructions() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gpt-5.2",
            "input": "Hi there",
            "instructions": "Be terse.",
            "reasoning": {"effort": "low"},
            "max_output_tokens": 256,
            "temperature": 0.3,
        }
    )
    gemini_req, model = await responses_to_gemini_request(req)
    # gpt-* 家族名回退到默认 Gemini 模型
    assert model == "gemini-3.8-flash"
    assert gemini_req.contents[0].parts[0].text == "Hi there"
    assert gemini_req.contents[0].role == "user"
    assert gemini_req.systemInstruction is not None
    assert gemini_req.systemInstruction.parts[0].text == "Be terse."
    assert gemini_req.generationConfig is not None
    assert gemini_req.generationConfig.reasoningEffort == "low"
    assert gemini_req.generationConfig.maxOutputTokens == 256
    assert gemini_req.generationConfig.temperature == 0.3


@pytest.mark.asyncio
async def test_effort_alias_max_maps_to_xhigh() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {"model": "gemini-3.8-flash", "input": "x", "reasoning": {"effort": "max"}}
    )
    gemini_req, _ = await responses_to_gemini_request(req)
    assert gemini_req.generationConfig is not None
    assert gemini_req.generationConfig.reasoningEffort == "xhigh"


@pytest.mark.asyncio
async def test_function_call_round_trip_items() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {"role": "user", "content": "Weather?"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city": "SF"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "Sunny 72F",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {"type": "object"},
                }
            ],
            "tool_choice": "auto",
        }
    )
    gemini_req, _ = await responses_to_gemini_request(req)
    # user → function_call(model) → function_output(user) 三轮
    assert len(gemini_req.contents) == 3
    call_part = gemini_req.contents[1].parts[0]
    assert call_part.functionCall is not None
    assert call_part.functionCall.name == "get_weather"
    assert call_part.functionCall.args == {"city": "SF"}
    resp_part = gemini_req.contents[2].parts[0]
    assert resp_part.functionResponse is not None
    # call_id 映射把函数名回填到 functionResponse（容忍乱序历史）
    assert resp_part.functionResponse.name == "get_weather"
    assert resp_part.functionResponse.response == {"result": "Sunny 72F"}
    # 工具声明
    assert gemini_req.tools is not None
    decls = gemini_req.tools[0].functionDeclarations
    assert cast(list[dict[str, object]], decls)[0]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_reasoning_items_dropped_and_content_parts() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "input": [
                # reasoning 回传应被静默丢弃（不透传 thoughtSignature）
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Describe"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,aGVsbG8=",
                        },
                    ],
                },
            ],
        }
    )
    gemini_req, _ = await responses_to_gemini_request(req)
    assert len(gemini_req.contents) == 1
    parts = gemini_req.contents[0].parts
    assert parts[0].text == "Describe"
    assert parts[1].inlineData is not None
    assert parts[1].inlineData.mimeType == "image/png"


@pytest.mark.asyncio
async def test_empty_input_rejected() -> None:
    req = OpenAIResponsesRequest.model_validate({"model": "gemini-3.8-flash"})
    with pytest.raises(ValueError):
        await responses_to_gemini_request(req)


@pytest.mark.asyncio
async def test_system_message_item_joins_instructions() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "instructions": "Base system prompt.",
            "input": [
                {"role": "developer", "content": "Extra developer rule."},
                {"role": "user", "content": "Go"},
            ],
        }
    )
    gemini_req, _ = await responses_to_gemini_request(req)
    assert gemini_req.systemInstruction is not None
    assert gemini_req.systemInstruction.parts[0].text == "Base system prompt."
    # developer 消息以 [SYSTEM] 前缀并入 contents（保序）
    assert gemini_req.contents[0].parts[0].text == "[SYSTEM] Extra developer rule."


# ---------------------------------------------------------------------------
# 非流式响应构建
# ---------------------------------------------------------------------------


def test_build_responses_object_basic() -> None:
    obj = build_responses_object(_sample_response(thought="Hmm"), "gpt-5.2")
    assert obj["object"] == "response"
    assert obj["status"] == "completed"
    assert obj["model"] == "gpt-5.2"
    output = cast(list[dict[str, object]], obj["output"])
    assert [item["type"] for item in output] == ["reasoning", "message"]
    reasoning = output[0]
    summary = cast(list[dict[str, object]], reasoning["summary"])
    assert summary[0]["text"] == "Hmm"
    message = output[1]
    content = cast(list[dict[str, object]], message["content"])
    assert content[0]["type"] == "output_text"
    assert content[0]["text"] == "Hello world"
    usage = cast(dict[str, object], obj["usage"])
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 20
    assert usage["total_tokens"] == 30
    details = cast(dict[str, object], usage["output_tokens_details"])
    assert details["reasoning_tokens"] == 4
    cached = cast(dict[str, object], usage["input_tokens_details"])
    assert cached["cached_tokens"] == 2


def test_build_responses_object_function_call() -> None:
    resp = GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(
                    role="model",
                    parts=[
                        GeminiPartResponse(
                            functionCall=GeminiFunctionCallPayload(
                                id="call_9", name="get_weather", args={"city": "SF"}
                            )
                        )
                    ],
                ),
                finishReason="STOP",
            )
        ],
        usageMetadata=GeminiUsageMetadata(promptTokenCount=5, candidatesTokenCount=9),
    )
    obj = build_responses_object(resp, "gpt-5.2")
    output = cast(list[dict[str, object]], obj["output"])
    assert len(output) == 1
    call = output[0]
    assert call["type"] == "function_call"
    assert call["call_id"] == "call_9"
    assert call["name"] == "get_weather"
    assert json.loads(cast(str, call["arguments"])) == {"city": "SF"}


def test_build_responses_object_max_tokens_incomplete() -> None:
    obj = build_responses_object(
        _sample_response(finish_reason="MAX_TOKENS"), "gpt-5.2"
    )
    assert obj["status"] == "incomplete"
    details = cast(dict[str, object], obj["incomplete_details"])
    assert details["reason"] == "max_output_tokens"


def test_build_responses_object_safety_incomplete() -> None:
    obj = build_responses_object(_sample_response(finish_reason="SAFETY"), "gpt-5.2")
    assert obj["status"] == "incomplete"
    details = cast(dict[str, object], obj["incomplete_details"])
    assert details["reason"] == "content_filter"


# ---------------------------------------------------------------------------
# 流式适配
# ---------------------------------------------------------------------------


def _parse_events(raw: str) -> list[dict[str, object]]:
    events = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        data_lines = [
            line[len("data: ") :]
            for line in block.splitlines()
            if line.startswith("data: ")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


@pytest.mark.asyncio
async def test_stream_adapter_event_sequence() -> None:
    frames = [
        {"candidates": [{"content": {"parts": [{"text": "Hmm", "thought": True}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "Hi "}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "there"}]}}]},
        {
            "candidates": [{"finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 4,
                "totalTokenCount": 9,
            },
        },
    ]
    raw = ""
    async for chunk in responses_stream_adapter(
        _sse_frames(frames), requested_model="gpt-5.2"
    ):
        raw += chunk
    events = _parse_events(raw)
    types = [event["type"] for event in events]

    # 事件顺序符合 Responses 语义协议
    assert types[0] == "response.created"
    assert types[1] == "response.in_progress"
    assert types[2] == "response.output_item.added"  # reasoning item
    assert types[3] == "response.reasoning_summary_part.added"
    assert "response.reasoning_summary_text.delta" in types
    assert "response.output_text.delta" in types
    assert types[-1] == "response.completed"

    # sequence_number 从 0 连续递增
    seqs = [event["sequence_number"] for event in events]
    assert seqs == list(range(len(events)))

    # completed 快照携带完整 output 与 usage
    completed = events[-1]
    response = cast(dict[str, object], completed["response"])
    output = cast(list[dict[str, object]], response["output"])
    assert [item["type"] for item in output] == ["reasoning", "message"]
    usage = cast(dict[str, object], response["usage"])
    assert usage["input_tokens"] == 5
    assert usage["output_tokens"] == 4

    # output_text.done 携带完整累积文本
    done = next(
        event for event in events if event["type"] == "response.output_text.done"
    )
    assert done["text"] == "Hi there"


@pytest.mark.asyncio
async def test_stream_adapter_function_call_events() -> None:
    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "call_7",
                                    "name": "search",
                                    "args": {"q": "hi"},
                                }
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        },
    ]
    raw = ""
    async for chunk in responses_stream_adapter(
        _sse_frames(frames), requested_model="gpt-5.2"
    ):
        raw += chunk
    types = [event["type"] for event in _parse_events(raw)]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    # function_call item 的 output_item.done 状态为 completed
    events = _parse_events(raw)
    done_items = [
        event for event in events if event["type"] == "response.output_item.done"
    ]
    fn_done = next(
        event
        for event in done_items
        if cast(dict[str, object], event["item"])["type"] == "function_call"
    )
    item = cast(dict[str, object], fn_done["item"])
    assert item["status"] == "completed"
    assert item["call_id"] == "call_7"
    assert json.loads(cast(str, item["arguments"])) == {"q": "hi"}


@pytest.mark.asyncio
async def test_stream_adapter_error_emits_failed() -> None:
    frames = [{"error": {"code": 429, "message": "rate limited"}}]
    raw = ""
    async for chunk in responses_stream_adapter(
        _sse_frames(frames), requested_model="gpt-5.2"
    ):
        raw += chunk
    events = _parse_events(raw)
    types = [event["type"] for event in events]
    assert types[-1] == "response.failed"
    failed = events[-1]
    response = cast(dict[str, object], failed["response"])
    assert response["status"] == "failed"
    error = cast(dict[str, object], response["error"])
    assert "rate limited" in cast(str, error["message"])


@pytest.mark.asyncio
async def test_stream_adapter_max_tokens_emits_incomplete() -> None:
    frames = [
        {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]},
        {"candidates": [{"finishReason": "MAX_TOKENS"}]},
    ]
    raw = ""
    async for chunk in responses_stream_adapter(
        _sse_frames(frames), requested_model="gpt-5.2"
    ):
        raw += chunk
    events = _parse_events(raw)
    assert events[-1]["type"] == "response.incomplete"
    response = cast(dict[str, object], events[-1]["response"])
    assert response["status"] == "incomplete"


# ---------------------------------------------------------------------------
# HTTP 端点（经 ASGI transport 打全链路）
# ---------------------------------------------------------------------------


@pytest.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_responses_endpoint_non_streaming(
    http_client: httpx.AsyncClient,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from aistudio_api.api.app import app
    from aistudio_api.api.dependencies import get_client
    from aistudio_api.domain.models import Candidate, ModelOutput

    mock_client = MagicMock()
    output = ModelOutput(
        candidates=[
            Candidate(text="Hello from upstream", finish_reason=1),
        ],
        usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    )
    mock_client.generate_content = AsyncMock(return_value=output)
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        resp = await http_client.post(
            "/v1/responses",
            json={"model": "gemini-3.8-flash", "input": "Hi"},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    output_items = cast(list[dict[str, object]], payload["output"])
    assert any(
        cast(dict[str, object], item.get("content", [{}])[0]).get("text")
        == "Hello from upstream"
        for item in output_items
        if item["type"] == "message"
    )


@pytest.mark.asyncio
async def test_responses_endpoint_validation_error(
    http_client: httpx.AsyncClient,
) -> None:
    from unittest.mock import MagicMock

    from aistudio_api.api.app import app
    from aistudio_api.api.dependencies import get_client

    app.dependency_overrides[get_client] = lambda: MagicMock()
    try:
        resp = await http_client.post(
            "/v1/responses",
            json={"model": "gemini-3.8-flash"},  # 缺 input
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
    payload = resp.json()
    assert "error" in payload
    assert payload["error"]["type"] == "invalid_request_error"
