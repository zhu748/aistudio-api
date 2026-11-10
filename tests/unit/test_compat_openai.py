"""OpenAI Chat Completions 兼容层测试。"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiFunctionCallPayload,
    GeminiGenerateContentResponse,
    GeminiPartResponse,
    GeminiUsageMetadata,
)
from aistudio_api.api.schemas_compat import OpenAIChatCompletionRequest
from aistudio_api.application.compat_service import (
    _openai_tools_to_gemini,
    build_openai_completion,
    openai_stream_adapter,
    openai_to_gemini_request,
    resolve_compat_model,
)
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.model_defaults import get_compat_settings


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    yield client
    await client.aclose()


def _sse_frames(frames: list[dict[str, object]]) -> AsyncGenerator[str, None]:
    async def gen() -> AsyncGenerator[str, None]:
        for frame in frames:
            yield "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"

    return gen()


# ---------------------------------------------------------------------------
# 模型映射
# ---------------------------------------------------------------------------


def test_resolve_compat_model_mapping_and_fallback() -> None:
    # 精确映射
    assert resolve_compat_model("gpt-4o-mini", channel="openai") == "gemini-3.7-flash"
    # 家族回退到配置默认模型
    assert resolve_compat_model("gpt-4o", channel="openai") == "gemini-3.8-flash"
    assert resolve_compat_model("o1-mini", channel="openai") == "gemini-3.8-flash"
    # Claude 家族回退
    assert (
        resolve_compat_model("claude-sonnet-4-5", channel="claude")
        == "gemini-3.8-flash"
    )
    # 前缀映射命中
    assert resolve_compat_model("claude-opus-4-1", channel="claude") == "gemini-4-argon"
    # 非 OpenAI 家族名原样透传（models/ 前缀剥离）
    assert resolve_compat_model("models/gemini-3.8-flash", channel="openai") == (
        "gemini-3.8-flash"
    )


def test_get_compat_settings_shape() -> None:
    compat = get_compat_settings()
    assert compat["openai_default_model"] == "gemini-3.8-flash"
    assert compat["claude_default_model"] == "gemini-3.8-flash"
    mapping = cast(dict[str, object], compat["openai_model_mapping"])
    assert mapping["gpt-4o-mini"] == "gemini-3.7-flash"
    claude_mapping = cast(dict[str, object], compat["claude_model_mapping"])
    assert claude_mapping["claude-opus"] == "gemini-4-argon"


# ---------------------------------------------------------------------------
# 请求转换
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_request_system_multiblock_text_keeps_separator() -> None:
    """system content 数组的多块文本以空行分隔，不得无分隔符粘连。"""
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "You are a"},
                        {"type": "text", "text": "helpful assistant."},
                    ],
                },
                {"role": "user", "content": "Hi"},
            ],
        }
    )
    gemini_req, _ = await openai_to_gemini_request(req)
    assert gemini_req.systemInstruction is not None
    assert gemini_req.systemInstruction.parts[0].text == (
        "You are a\n\nhelpful assistant."
    )


@pytest.mark.asyncio
async def test_openai_request_rejects_oversized_data_url_media() -> None:
    """超大 base64 data URL 在入口即拒绝（400），不透传上游。"""
    huge_b64 = "A" * (28 * 1024 * 1024)  # ≈21MB 原始字节，超过 20MB 限额
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{huge_b64}"},
                        },
                    ],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="exceeds"):
        await openai_to_gemini_request(req)


def test_openai_tools_skip_non_function_types() -> None:
    """custom / 内置工具类型不误声明为 Gemini 函数。"""
    tools = _openai_tools_to_gemini(
        [
            {"type": "custom", "custom": {"name": "web_search", "prompt": "..."}},
            {
                "type": "function",
                "function": {"name": "f1", "parameters": {"type": "object"}},
            },
        ]
    )
    assert tools is not None
    declarations = tools[0].functionDeclarations
    assert declarations is not None
    assert [d["name"] for d in declarations] == ["f1"]


@pytest.mark.asyncio
async def test_openai_streaming_include_usage_accepts_truthy_string(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    """宽松客户端把 include_usage 写成字符串 'true' 时也应生效。"""

    async def fake_stream(
        *_args: object, **_kwargs: object
    ) -> AsyncGenerator[tuple[str, object], None]:
        yield ("body", "hi")
        yield ("usage", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})

    mock_client.stream_generate_content = fake_stream
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
                "stream_options": {"include_usage": "true"},
            },
        )
        assert response.status_code == 200
        text = response.text
        assert "data: [DONE]" in text
        # 字符串 'true' 也应触収独立的 usage 尾块（choices 为空）
        assert '"total_tokens": 5' in text
        assert '"choices": []' in text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_openai_request_basic_conversion() -> None:
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "Hi there."},
                {"role": "user", "content": [{"type": "text", "text": "How are you?"}]},
            ],
            "temperature": 0.7,
            "max_tokens": 512,
            "stop": ["END"],
            "frequency_penalty": 0.1,
            "presence_penalty": 0.2,
            "seed": 42,
            "reasoning_effort": "low",
        }
    )
    gemini_req, resolved = await openai_to_gemini_request(req)

    assert resolved == "gemini-3.8-flash"
    assert len(gemini_req.contents) == 3
    assert gemini_req.contents[0].role == "user"
    assert gemini_req.contents[0].parts[0].text == "Hello!"
    assert gemini_req.contents[1].role == "model"
    assert gemini_req.contents[1].parts[0].text == "Hi there."
    assert gemini_req.contents[2].parts[0].text == "How are you?"
    assert gemini_req.systemInstruction is not None
    assert gemini_req.systemInstruction.parts[0].text == "You are helpful."
    assert gemini_req.generationConfig is not None
    assert gemini_req.generationConfig.temperature == 0.7
    assert gemini_req.generationConfig.maxOutputTokens == 512
    assert gemini_req.generationConfig.stopSequences == ["END"]
    assert gemini_req.generationConfig.frequencyPenalty == 0.1
    assert gemini_req.generationConfig.presencePenalty == 0.2
    assert gemini_req.generationConfig.seed == 42
    assert gemini_req.generationConfig.reasoningEffort == "low"


@pytest.mark.asyncio
async def test_openai_request_tools_and_tool_roundtrip() -> None:
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get weather",
                                "arguments": '{"location": "Beijing"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_abc123",
                    "content": '{"temp": 25}',
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "response_format": {"type": "json_object"},
        }
    )
    gemini_req, resolved = await openai_to_gemini_request(req)

    assert resolved == "gemini-3.8-flash"
    # assistant tool_calls → functionCall parts（保留 id 供回传）
    model_content = gemini_req.contents[0]
    assert model_content.role == "model"
    fc_part = model_content.parts[0]
    assert fc_part.functionCall is not None
    assert fc_part.functionCall.name == "get_weather"
    assert fc_part.functionCall.args == {"location": "Beijing"}
    assert fc_part.functionCall.id == "call_abc123"
    # tool 消息 → functionResponse（name 由 tool_call_id 反查）
    tool_content = gemini_req.contents[1]
    assert tool_content.role == "user"
    fr_part = tool_content.parts[0]
    assert fr_part.functionResponse is not None
    assert fr_part.functionResponse.name == "get_weather"
    assert fr_part.functionResponse.id == "call_abc123"
    assert fr_part.functionResponse.response == {"temp": 25}
    # tools 声明透传
    assert gemini_req.tools is not None
    assert gemini_req.tools[0].functionDeclarations is not None
    assert gemini_req.tools[0].functionDeclarations[0]["name"] == "get_weather"
    # tool_choice auto + json_object → mime
    assert gemini_req.toolConfig == {"functionCallingConfig": {"mode": "AUTO"}}
    assert gemini_req.generationConfig is not None
    assert gemini_req.generationConfig.responseMimeType == "application/json"


@pytest.mark.asyncio
async def test_openai_request_image_data_url() -> None:
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                        },
                    ],
                }
            ],
        }
    )
    gemini_req, _ = await openai_to_gemini_request(req)
    parts = gemini_req.contents[0].parts
    assert parts[0].text == "What is this?"
    assert parts[1].inlineData is not None
    assert parts[1].inlineData.mimeType == "image/png"
    assert parts[1].inlineData.data == "iVBORw0KGgo="


@pytest.mark.asyncio
async def test_openai_request_tool_choice_forces_single_function() -> None:
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "f1", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "f1"}},
        }
    )
    gemini_req, _ = await openai_to_gemini_request(req)
    assert gemini_req.toolConfig == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["f1"]}
    }


# ---------------------------------------------------------------------------
# 响应构建
# ---------------------------------------------------------------------------


def _gemini_response(
    parts: list[GeminiPartResponse],
    *,
    finish_reason: str = "STOP",
    usage: GeminiUsageMetadata | None = None,
) -> GeminiGenerateContentResponse:
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(parts=parts),
                finishReason=finish_reason,
                index=0,
            )
        ],
        usageMetadata=usage,
    )


def test_build_openai_completion_text_and_reasoning() -> None:
    response = _gemini_response(
        [
            GeminiPartResponse(text="Let me think...", thought=True),
            GeminiPartResponse(text="Hello world"),
        ],
        usage=GeminiUsageMetadata(
            promptTokenCount=10,
            candidatesTokenCount=20,
            thoughtsTokenCount=8,
            totalTokenCount=30,
        ),
    )
    completion = cast(dict[str, Any], build_openai_completion(response, "gpt-4o"))
    assert completion["object"] == "chat.completion"
    assert completion["model"] == "gpt-4o"
    choice = completion["choices"][0]
    assert choice["message"]["content"] == "Hello world"
    assert choice["message"]["reasoning_content"] == "Let me think..."
    assert choice["finish_reason"] == "stop"
    usage = completion["usage"]
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 30
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 8


def test_build_openai_completion_tool_calls() -> None:
    response = _gemini_response(
        [
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="get_weather", args={"location": "Tokyo"}, id="call_xyz"
                )
            )
        ],
        finish_reason="STOP",
    )
    completion = cast(dict[str, Any], build_openai_completion(response, "gpt-4o"))
    message = completion["choices"][0]["message"]
    assert completion["choices"][0]["finish_reason"] == "tool_calls"
    tool_calls = message["tool_calls"]
    assert tool_calls[0]["id"] == "call_xyz"
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"location": "Tokyo"}


def test_build_openai_completion_max_tokens_and_safety() -> None:
    response = _gemini_response(
        [GeminiPartResponse(text="partial")], finish_reason="MAX_TOKENS"
    )
    assert cast(dict[str, Any], build_openai_completion(response, "m"))["choices"][0][
        "finish_reason"
    ] == ("length")
    response = _gemini_response([GeminiPartResponse(text="")], finish_reason="SAFETY")
    assert cast(dict[str, Any], build_openai_completion(response, "m"))["choices"][0][
        "finish_reason"
    ] == ("content_filter")


# ---------------------------------------------------------------------------
# 流式适配
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_stream_adapter_full_sequence() -> None:
    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Thinking...", "thought": True}],
                    },
                    "index": 0,
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "Hello "}]},
                    "index": 0,
                }
            ]
        },
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "world"}]}, "index": 0}
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "f1",
                                    "args": {"a": 1},
                                    "id": "call_1",
                                }
                            }
                        ],
                    },
                    "index": 0,
                }
            ]
        },
        {
            "candidates": [
                {"content": {"parts": []}, "finishReason": "STOP", "index": 0}
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 7,
                "thoughtsTokenCount": 2,
                "totalTokenCount": 12,
            },
        },
    ]
    chunks: list[str] = []
    async for sse in openai_stream_adapter(
        _sse_frames(frames), requested_model="gpt-4o", include_usage=True
    ):
        chunks.append(sse)

    payloads = [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in chunks
        if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]"
    ]
    # 首块 role、thinking、正文两段、工具调用、finish、usage、[DONE]
    assert chunks[-1] == "data: [DONE]\n\n"
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert payloads[1]["choices"][0]["delta"] == {"reasoning_content": "Thinking..."}
    assert payloads[2]["choices"][0]["delta"] == {"content": "Hello "}
    assert payloads[3]["choices"][0]["delta"] == {"content": "world"}
    tool_delta = payloads[4]["choices"][0]["delta"]
    assert tool_delta["tool_calls"][0]["function"]["name"] == "f1"
    assert json.loads(tool_delta["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
    assert payloads[5]["choices"][0]["finish_reason"] == "tool_calls"
    usage_chunk = payloads[-1]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["prompt_tokens"] == 5
    assert usage_chunk["usage"]["completion_tokens"] == 7
    assert usage_chunk["usage"]["total_tokens"] == 12


@pytest.mark.asyncio
async def test_openai_stream_adapter_error_frame() -> None:
    frames: list[dict[str, object]] = [
        {
            "error": {
                "code": 429,
                "message": "quota exceeded",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    ]
    chunks: list[str] = []
    async for sse in openai_stream_adapter(
        _sse_frames(frames), requested_model="gpt-4o", include_usage=False
    ):
        chunks.append(sse)
    error_payload = json.loads(chunks[1].removeprefix("data: ").strip())
    assert error_payload["error"]["type"] == "rate_limit_error"
    assert error_payload["error"]["code"] == 429
    # 错误帧必须是合法 ChatCompletionChunk（SDK 会 model_validate 每个 chunk）：
    # 带完整 id/object/created/model/choices + 终止 finish_reason。
    assert error_payload["object"] == "chat.completion.chunk"
    assert error_payload["id"].startswith("chatcmpl-")
    assert error_payload["model"] == "gpt-4o"
    assert error_payload["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_openai_stream_adapter_no_usage_when_include_usage_false() -> None:
    """include_usage=False 时任何 chunk 都不得携带 usage 字段（OpenAI 协议）。"""
    frames: list[dict[str, object]] = [
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "hi"}]},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 7,
                "totalTokenCount": 12,
            },
        }
    ]
    payloads: list[dict[str, Any]] = []
    async for sse in openai_stream_adapter(
        _sse_frames(frames), requested_model="gpt-4o", include_usage=False
    ):
        if sse.startswith("data: ") and sse.strip() != "data: [DONE]":
            payloads.append(json.loads(sse.removeprefix("data: ").strip()))
    assert payloads
    assert all("usage" not in p for p in payloads)
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_openai_stream_adapter_reassembles_split_sse_frames() -> None:
    """单个 SSE 事件被网络拆成多个 chunk 时必须重组，而非静默丢整帧。"""
    frame = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "index": 0}]}
    full = "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"

    async def split_source() -> AsyncGenerator[str, None]:
        # 在 JSON 中间、data 行中间、事件边界处任意切分
        for i in range(0, len(full), 7):
            yield full[i : i + 7]

    texts: list[str] = []
    async for sse in openai_stream_adapter(
        split_source(), requested_model="gpt-4o", include_usage=False
    ):
        if sse.startswith("data: ") and sse.strip() != "data: [DONE]":
            p = json.loads(sse.removeprefix("data: ").strip())
            delta = p["choices"][0]["delta"]
            if "content" in delta:
                texts.append(delta["content"])
    assert "".join(texts) == "hello"


@pytest.mark.asyncio
async def test_openai_stream_adapter_multiline_data() -> None:
    """同一 SSE 事件内多行 data: 需合并后再 json.loads。"""
    parts = [
        '{"candidates": [{"content": {"parts": [{"text": "A"}]},',
        '"index": 0}]}',
    ]
    event = "\n".join("data: " + p for p in parts) + "\n\n"

    async def source() -> AsyncGenerator[str, None]:
        yield event

    texts: list[str] = []
    async for sse in openai_stream_adapter(
        source(), requested_model="gpt-4o", include_usage=False
    ):
        if sse.startswith("data: ") and sse.strip() != "data: [DONE]":
            p = json.loads(sse.removeprefix("data: ").strip())
            delta = p["choices"][0]["delta"]
            if delta.get("content"):
                texts.append(delta["content"])
    assert texts == ["A"]


@pytest.mark.asyncio
async def test_openai_stream_adapter_tolerates_indented_data_lines() -> None:
    """SSE 允许行首空白；`line[5:]` 式硬切会把 payload 首字符吃掉。"""
    frame = {"candidates": [{"content": {"parts": [{"text": "B"}]}, "index": 0}]}
    payload = json.dumps(frame, ensure_ascii=False)
    # 在 JSON 的逗号后断开（SSE 多行 data 以换行重新拼接，逗号后的换行是
    # 合法 JSON 空白），同时给两行加不同的行首空白与 CRLF 行尾。
    head, tail = payload.split(', "index"', 1)
    event = f'  data: {head},\n\tdata:  "index"{tail}\r\n\r\n'

    async def source() -> AsyncGenerator[str, None]:
        yield event

    texts: list[str] = []
    async for sse in openai_stream_adapter(
        source(), requested_model="gpt-4o", include_usage=False
    ):
        if sse.startswith("data: ") and sse.strip() != "data: [DONE]":
            p = json.loads(sse.removeprefix("data: ").strip())
            delta = p["choices"][0]["delta"]
            if delta.get("content"):
                texts.append(delta["content"])
    assert texts == ["B"]


@pytest.mark.asyncio
async def test_iter_gemini_sse_frames_drops_oversized_buffer() -> None:
    """上游持续不发事件边界时，缓冲区必须有上限（否则内存被拖垮）。"""
    from aistudio_api.application import compat_service

    async def source() -> AsyncGenerator[str, None]:
        # 无空行的超大片段
        for _ in range(4):
            yield "data: " + ("x" * (8 * 1024 * 1024))

    frames = [
        frame
        async for frame in compat_service._iter_gemini_sse_frames(source())  # pyright: ignore[reportPrivateUsage]
    ]
    assert frames == []


def test_openai_finish_reason_safety_beats_tool_calls() -> None:
    """同一帧带 functionCall 但被安全拦截时必须报 content_filter。"""
    from aistudio_api.application.compat_service import _openai_finish_reason

    assert _openai_finish_reason("SAFETY", has_tool_calls=True) == "content_filter"
    assert _openai_finish_reason("MISSING_THOUGHT_SIGNATURE", has_tool_calls=True) == (
        "content_filter"
    )
    assert _openai_finish_reason("STOP", has_tool_calls=True) == "tool_calls"


def test_resolve_compat_model_family_boundaries() -> None:
    """家族回退覆盖 ft:/Bedrock 前缀，且前缀映射不误命中无边界后缀。"""
    assert (
        resolve_compat_model("ft:gpt-4o-mini", channel="openai") == "gemini-3.8-flash"
    )
    assert (
        resolve_compat_model("us.anthropic.claude-sonnet-4-5", channel="claude")
        == "gemini-3.8-flash"
    )
    # "claude-opus" 是 conftest 前缀映射，不应误命中 "claude-opusx-foo" 之外，
    # 但 "claude-opus-4-1" 仍应命中映射（原有测试已覆盖）。
    assert resolve_compat_model("claude-opus-4-1", channel="claude") == "gemini-4-argon"


# ---------------------------------------------------------------------------
# E2E（ASGITransport + mock 客户端）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_openai_non_streaming(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="Hello from compat!")],
            usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        )
    )
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "gpt-4o"
        assert body["choices"][0]["message"]["content"] == "Hello from compat!"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] == 8
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_openai_streaming(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    async def fake_stream(
        *_args: object, **_kwargs: object
    ) -> AsyncGenerator[tuple[str, object], None]:
        yield ("thinking", "pondering")
        yield ("body", "Streamed ")
        yield ("body", "reply")
        yield ("usage", {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6})

    mock_client.stream_generate_content = fake_stream
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        text = response.text
        assert "data: [DONE]" in text
        assert '"content": "Streamed "' in text
        assert '"content": "reply"' in text
        assert '"reasoning_content": "pondering"' in text
        assert '"total_tokens": 6' in text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_openai_models_list_and_error_format(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    mock_client.generate_content = AsyncMock(side_effect=ValueError("boom-upstream"))
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        models_response = await http_client.get("/v1/models")
        assert models_response.status_code == 200
        body = models_response.json()
        assert body["object"] == "list"
        assert all(m["object"] == "model" for m in body["data"])

        # 上游错误 → OpenAI 格式错误体
        error_response = await http_client.post(
            "/v1/chat/completions",
            json={"model": "gemini-3.8-flash", "messages": []},
        )
        assert error_response.status_code == 400
        error_body = error_response.json()
        assert error_body["error"]["type"] == "invalid_request_error"
        assert "error" in error_body
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_openai_validation_error_format(
    http_client: httpx.AsyncClient,
) -> None:
    app.dependency_overrides[get_client] = lambda: MagicMock()
    try:
        response = await http_client.post(
            "/v1/chat/completions", json={"model": "gpt-4o"}
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["message"]
    finally:
        app.dependency_overrides.clear()
