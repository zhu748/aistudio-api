"""Anthropic Claude Messages 兼容层测试。"""

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
    GeminiInlineDataResponse,
    GeminiPartResponse,
    GeminiUsageMetadata,
)
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application.compat_service import (
    _claude_tools_to_gemini,
    build_claude_message,
    claude_stream_adapter,
    claude_to_gemini_request,
)
from aistudio_api.domain.models import Candidate, ModelOutput


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
# 请求转换
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_request_basic_conversion() -> None:
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "system": "You are concise.",
            "messages": [
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "Hi."},
                {"role": "user", "content": "Explain SSE."},
            ],
            "temperature": 0.5,
            "top_p": 0.9,
            "top_k": 40,
            "stop_sequences": ["END"],
            "thinking": {"type": "enabled", "budget_tokens": 4096},
        }
    )
    gemini_req, resolved = await claude_to_gemini_request(req)

    assert resolved == "gemini-3.8-flash"
    assert len(gemini_req.contents) == 3
    assert [c.role for c in gemini_req.contents] == ["user", "model", "user"]
    assert gemini_req.systemInstruction is not None
    assert gemini_req.systemInstruction.parts[0].text == "You are concise."
    gc = gemini_req.generationConfig
    assert gc is not None
    assert gc.maxOutputTokens == 1024
    assert gc.temperature == 0.5
    assert gc.topP == 0.9
    assert gc.topK == 40
    assert gc.stopSequences == ["END"]
    assert gc.thinkingConfig == {"thinkingLevel": "MEDIUM"}


@pytest.mark.asyncio
async def test_claude_request_tool_roundtrip() -> None:
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll check the weather."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01ABC",
                            "name": "get_weather",
                            "input": {"location": "Tokyo"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01ABC",
                            "content": [
                                {"type": "text", "text": "Sunny, 22C"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "iVBORw0KGgo=",
                                    },
                                },
                            ],
                            "is_error": False,
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                }
            ],
            "tool_choice": {"type": "auto"},
        }
    )
    gemini_req, resolved = await claude_to_gemini_request(req)

    assert resolved == "gemini-3.8-flash"
    # assistant: text + tool_use
    assistant_parts = gemini_req.contents[0].parts
    assert assistant_parts[0].text == "I'll check the weather."
    fc = assistant_parts[1].functionCall
    assert fc is not None
    assert fc.name == "get_weather"
    assert fc.args == {"location": "Tokyo"}
    assert fc.id == "toolu_01ABC"
    # user: tool_result（含图片展开）
    user_parts = gemini_req.contents[1].parts
    fr = user_parts[0].functionResponse
    assert fr is not None
    assert fr.name == "get_weather"
    assert fr.id == "toolu_01ABC"
    assert fr.response == {"result": "Sunny, 22C"}
    assert user_parts[1].inlineData is not None
    assert user_parts[1].inlineData.mimeType == "image/png"
    # tools
    assert gemini_req.tools is not None
    declarations = gemini_req.tools[0].functionDeclarations
    assert declarations is not None
    assert cast(dict[str, Any], declarations[0])["name"] == "get_weather"
    assert cast(dict[str, Any], cast(dict[str, Any], declarations[0])["parameters"])[
        "required"
    ] == ["location"]
    assert gemini_req.toolConfig == {"functionCallingConfig": {"mode": "AUTO"}}


@pytest.mark.asyncio
async def test_claude_request_thinking_blocks_dropped_signature() -> None:
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Internal reasoning",
                            "signature": "fake-signature",
                        },
                        {"type": "text", "text": "Answer"},
                    ],
                }
            ],
        }
    )
    gemini_req, _ = await claude_to_gemini_request(req)
    parts = gemini_req.contents[0].parts
    assert parts[0].text == "Internal reasoning"
    assert parts[0].thought is True
    # 伪造 signature 不透传上游
    assert parts[0].thoughtSignature is None
    assert parts[1].text == "Answer"


@pytest.mark.asyncio
async def test_claude_request_tool_choice_tool_mode() -> None:
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "f1",
                    "input_schema": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "tool", "name": "f1"},
        }
    )
    gemini_req, _ = await claude_to_gemini_request(req)
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


def test_build_claude_message_blocks_and_stop_reason() -> None:
    response = _gemini_response(
        [
            GeminiPartResponse(text="Thinking hard", thought=True),
            GeminiPartResponse(text="The answer"),
        ],
        usage=GeminiUsageMetadata(
            promptTokenCount=7, candidatesTokenCount=11, totalTokenCount=18
        ),
    )
    message = cast(
        dict[str, Any], build_claude_message(response, "claude-sonnet-4-5", None)
    )
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["model"] == "claude-sonnet-4-5"
    assert message["content"][0]["type"] == "thinking"
    assert message["content"][0]["thinking"] == "Thinking hard"
    assert message["content"][0]["signature"]
    assert message["content"][1] == {"type": "text", "text": "The answer"}
    assert message["stop_reason"] == "end_turn"
    assert message["usage"] == {"input_tokens": 7, "output_tokens": 11}


def test_build_claude_message_tool_use_and_stop_sequence() -> None:
    response = _gemini_response(
        [
            GeminiPartResponse(text="Result: DONE"),
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="f1", args={"x": 1}, id="toolu_01XYZ"
                )
            ),
        ]
    )
    message = cast(dict[str, Any], build_claude_message(response, "m", ["DONE"]))
    assert message["stop_reason"] == "stop_sequence"
    assert message["stop_sequence"] == "DONE"
    # The stop sequence ends the response, including subsequent tool actions.
    assert message["content"] == [{"type": "text", "text": "Result: "}]


def test_build_claude_message_image_and_refusal() -> None:
    response = _gemini_response(
        [
            GeminiPartResponse(
                inlineData=GeminiInlineDataResponse(
                    mimeType="image/png", data="iVBORw0KGgo="
                )
            )
        ],
        finish_reason="SAFETY",
    )
    message = cast(dict[str, Any], build_claude_message(response, "m", None))
    assert message["content"][0]["type"] == "image"
    assert message["content"][0]["source"]["media_type"] == "image/png"
    assert message["stop_reason"] == "refusal"


# ---------------------------------------------------------------------------
# 流式适配
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_stream_adapter_full_sequence() -> None:
    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Deep thought", "thought": True}],
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
                                    "id": "toolu_1",
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
                "candidatesTokenCount": 9,
                "totalTokenCount": 14,
            },
        },
    ]
    events: list[str] = []
    async for sse in claude_stream_adapter(
        _sse_frames(frames), requested_model="claude-sonnet-4-5", stop_sequences=None
    ):
        events.append(sse)

    text = "".join(events)
    # 事件序列骨架
    assert "event: message_start" in text
    assert "event: ping" in text
    assert "event: content_block_start" in text
    assert "event: content_block_delta" in text
    assert "event: content_block_stop" in text
    assert "event: message_delta" in text
    assert "event: message_stop" in text

    parsed = []
    for sse in events:
        data_line = next(line for line in sse.splitlines() if line.startswith("data: "))
        parsed.append(json.loads(data_line.removeprefix("data: ")))

    assert parsed[0]["type"] == "message_start"
    assert parsed[0]["message"]["model"] == "claude-sonnet-4-5"
    assert parsed[1]["type"] == "ping"
    # thinking 块（index 0）→ text 块（index 1）→ tool_use 块（index 2）
    thinking_start = parsed[2]
    assert thinking_start["type"] == "content_block_start"
    assert thinking_start["index"] == 0
    assert thinking_start["content_block"]["type"] == "thinking"
    thinking_delta = parsed[3]
    assert thinking_delta["delta"]["type"] == "thinking_delta"
    assert thinking_delta["delta"]["thinking"] == "Deep thought"
    # thinking 块关闭前补 signature_delta
    signature_delta = parsed[4]
    assert signature_delta["delta"]["type"] == "signature_delta"
    assert signature_delta["delta"]["signature"]
    assert parsed[5]["type"] == "content_block_stop"

    text_start = parsed[6]
    assert text_start["index"] == 1
    assert text_start["content_block"]["type"] == "text"
    assert parsed[7]["delta"] == {"type": "text_delta", "text": "Hello "}
    assert parsed[8]["delta"] == {"type": "text_delta", "text": "world"}
    assert parsed[9]["type"] == "content_block_stop"

    tool_start = parsed[10]
    assert tool_start["index"] == 2
    assert tool_start["content_block"]["type"] == "tool_use"
    assert tool_start["content_block"]["id"] == "toolu_1"
    json_delta = parsed[11]
    assert json_delta["delta"]["type"] == "input_json_delta"
    assert json.loads(json_delta["delta"]["partial_json"]) == {"a": 1}
    assert parsed[12]["type"] == "content_block_stop"

    message_delta = parsed[-2]
    assert message_delta["type"] == "message_delta"
    assert message_delta["delta"]["stop_reason"] == "tool_use"
    assert message_delta["usage"]["output_tokens"] == 9
    assert parsed[-1]["type"] == "message_stop"


@pytest.mark.asyncio
async def test_claude_stream_adapter_error_event() -> None:
    frames: list[dict[str, object]] = [
        {
            "error": {
                "code": 429,
                "message": "quota exceeded",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    ]
    events: list[str] = []
    async for sse in claude_stream_adapter(
        _sse_frames(frames), requested_model="claude-sonnet-4-5", stop_sequences=None
    ):
        events.append(sse)
    text = "".join(events)
    assert "event: error" in text
    data_line = next(
        line for line in events[-1].splitlines() if line.startswith("data: ")
    )
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "rate_limit_error"
    # 错误后不再发送 message_stop
    assert "event: message_stop" not in text


# ---------------------------------------------------------------------------
# E2E（ASGITransport + mock 客户端）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_request_rejects_oversized_inline_media() -> None:
    """超大 base64 图片在入口即拒绝（ValueError → 400），不透传上游。"""
    huge_b64 = "A" * (28 * 1024 * 1024)  # ≈21MB 原始字节，超过 20MB 限额
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": huge_b64,
                            },
                        },
                    ],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="exceeds"):
        await claude_to_gemini_request(req)


def test_claude_tools_skip_server_side_tools() -> None:
    """web_search 等服务端工具不误声明为 Gemini 函数，仅保留自定义工具。"""
    tools = _claude_tools_to_gemini(
        [
            {"type": "web_search_20250305", "name": "web_search"},
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {"type": "object"},
            },
            {
                "type": "custom",
                "name": "lookup",
                "input_schema": {"type": "object"},
            },
        ]
    )
    assert tools is not None
    declarations = tools[0].functionDeclarations
    assert declarations is not None
    assert [d["name"] for d in declarations] == ["get_weather", "lookup"]


@pytest.mark.asyncio
async def test_e2e_claude_count_tokens_shrinks_base64_media(
    http_client: httpx.AsyncClient,
) -> None:
    """count_tokens 不把 base64 图片计入字符数，避免估算虚高千倍。"""
    big_img = "iVBORw0KGgo" + "A" * 400_000  # ~400KB base64
    app.dependency_overrides[get_client] = lambda: MagicMock()
    try:
        response = await http_client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-sonnet-4-5",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": big_img,
                                },
                            },
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200
        estimate = response.json()["input_tokens"]
        # 修复前 ≈ 100000+；占位符后应回到文本量级
        assert estimate < 100
        assert estimate >= 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_claude_non_streaming(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="Claude compat reply")],
            usage={"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
        )
    )
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["model"] == "claude-sonnet-4-5"
        assert body["content"] == [{"type": "text", "text": "Claude compat reply"}]
        assert body["stop_reason"] == "end_turn"
        assert body["usage"]["output_tokens"] == 6
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_claude_streaming(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    async def fake_stream(
        *_args: object, **_kwargs: object
    ) -> AsyncGenerator[tuple[str, object], None]:
        yield ("body", "Streamed ")
        yield ("body", "answer")
        yield ("usage", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})

    mock_client.stream_generate_content = fake_stream
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.post(
            "/v1/messages",
            json={
                "model": "gemini-3.8-flash",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        text = response.text
        assert "event: message_start" in text
        assert '"text": "Streamed "' in text
        assert '"text": "answer"' in text
        assert "event: message_delta" in text
        assert "event: message_stop" in text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_claude_models_and_count_tokens(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.get(
            "/v1/models", headers={"anthropic-version": "2023-06-01"}
        )
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert body["has_more"] is False
        if body["data"]:
            assert body["data"][0]["type"] == "model"
            assert "display_name" in body["data"][0]

        count_response = await http_client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "Hello world"}],
            },
        )
        assert count_response.status_code == 200
        count_body = count_response.json()
        assert count_body["input_tokens"] >= 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_claude_error_format(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    mock_client.generate_content = AsyncMock(side_effect=ValueError("bad input"))
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.post(
            "/v1/messages",
            json={"model": "gemini-3.8-flash", "messages": []},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["message"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_claude_paid_model_gate_error_format(
    mock_client: MagicMock, http_client: httpx.AsyncClient
) -> None:
    """付费模型门控 403 也应按 Claude 错误格式下发。"""
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        response = await http_client.post(
            "/v1/messages",
            json={
                "model": "claude-opus-4-1",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 403
        body = response.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "permission_error"
    finally:
        app.dependency_overrides.clear()


def test_build_claude_message_reports_cache_read_tokens() -> None:
    """上游缓存命中数必须映射为 Anthropic 的 cache_read_input_tokens。

    回归点：旧实现只发 input_tokens/output_tokens，缓存命中数全链路丢失，
    客户端据此算成本会偏高。
    """
    response = _gemini_response(
        [GeminiPartResponse(text="cached answer")],
        usage=GeminiUsageMetadata(
            promptTokenCount=100,
            candidatesTokenCount=20,
            totalTokenCount=120,
            cachedContentTokenCount=64,
        ),
    )
    message = cast(dict[str, Any], build_claude_message(response, "m", None))
    usage = cast(dict[str, Any], message["usage"])
    assert usage["input_tokens"] == 36
    assert usage["output_tokens"] == 20
    assert usage["cache_read_input_tokens"] == 64


def test_claude_stop_reason_refusal_beats_tool_use() -> None:
    """安全拦截必须压过 tool_use：否则客户端会把被拦断的回复当成正常工具调用。"""
    from aistudio_api.application.compat_service import _claude_stop_reason

    def reason(finish: str, *, tool_use: bool) -> str:
        return _claude_stop_reason(
            finish, has_tool_use=tool_use, full_text="", stop_sequences=None
        )[0]

    assert reason("SAFETY", tool_use=True) == "refusal"
    assert reason("PROHIBITED_CONTENT", tool_use=True) == "refusal"
    assert reason("STOP", tool_use=True) == "tool_use"
    assert reason("STOP", tool_use=False) == "end_turn"
    assert reason("MAX_TOKENS", tool_use=False) == "max_tokens"
