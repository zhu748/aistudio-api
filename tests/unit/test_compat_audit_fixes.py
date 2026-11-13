"""协议转换层深审修复的回归测试。

覆盖 2026-11 协议转换层深审（4 个子代理并行审查）确认并修复的问题：

- transport：白名单大小写折叠（WAA Go canonical 头与 Python 字面量
  大小写不一致导致上游收到值冲突的重复头）
- Responses：output_index 语义（交错流中关闭/delta 事件报错 index）、
  assistant 历史回传 output_text 块、tools 双形态（Chat 子对象 /
  Responses 顶层）、SDK 必填字段、快照按打开顺序排序
- Claude：流式分片 functionCall 跨帧拼接（args 字符串分片/全量重发/
  名称分片）、stop sequence 中间命中截断、count_tokens 计入 tools
- OpenAI：OTHER 截断映射 length、role=function 旧消息、stop 空串过滤
- model_mapping：通配符键 "xxx-*" 前缀匹配命中
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiFunctionCallPayload,
    GeminiGenerateContentResponse,
    GeminiPartResponse,
)
from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application.compat_service import (
    _openai_finish_reason,
    _openai_tools_to_gemini,
    build_claude_message,
    build_openai_completion,
    claude_stream_adapter,
    openai_to_gemini_request,
    resolve_compat_model,
)
from aistudio_api.application.responses_service import (
    responses_stream_adapter,
    responses_to_gemini_request,
)


def _sse_frames(frames: list[dict[str, object]]) -> AsyncGenerator[str, None]:
    async def gen() -> AsyncGenerator[str, None]:
        for frame in frames:
            yield "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"

    return gen()


def _parse_sse_events(raw: str) -> list[dict[str, object]]:
    """解析适配器产出的 SSE 文本流为 (event, data) 字典列表。"""
    events: list[dict[str, object]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        data_lines = [
            line[len("data:") :].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            payload = "\n".join(data_lines)
            if payload == "[DONE]":
                continue
            parsed = json.loads(payload)
            events.append(parsed if isinstance(parsed, dict) else {})
    return events


def _gemini_response(
    parts: list[GeminiPartResponse], finish: str | None = "STOP"
) -> GeminiGenerateContentResponse:
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(role="model", parts=parts),
                finishReason=finish,
            )
        ]
    )


# ---------------------------------------------------------------------------
# transport：白名单大小写折叠
# ---------------------------------------------------------------------------


def test_ensure_authorization_header_folds_case_variants() -> None:
    """大小写混合的 Authorization/Cookie 变体折叠为单键，不产生重复头。"""
    from aistudio_api.infrastructure.gateway.transport import (
        _ensure_authorization_header,
    )

    headers = {
        "AUTHORIZATION": "SAPISIDHASH old",
        "authorization": "SAPISIDHASH new",
        "User-Agent": "Mozilla/5.0",
        "X-Goog-Authuser": "u5",  # Go canonical（waa-signer）
    }
    result = _ensure_authorization_header(headers, auth_user="0")
    auth_keys = [k for k in result if k.lower() == "authorization"]
    assert auth_keys == ["Authorization"], f"Authorization 变体未折叠: {result}"
    # 无本地 Cookie 时 incoming 兜底（后写入的小写变体胜出）
    assert result["Authorization"] == "SAPISIDHASH new"
    # Go canonical 与 Python canonical 折叠为一个 X-Goog-AuthUser
    authuser_keys = [k for k in result if k.lower() == "x-goog-authuser"]
    assert authuser_keys == ["X-Goog-AuthUser"]
    assert result["X-Goog-AuthUser"] == "0"  # 本地 auth_user 重建


def test_ensure_authorization_header_local_cookies_win() -> None:
    """本地账号 Cookie 存在时总是重建凭据头（压制 signer 缓存的旧值）。"""
    from aistudio_api.infrastructure.gateway.transport import (
        _ensure_authorization_header,
    )

    headers = {
        "Cookie": "SAPISID=stale_from_signer_cache",
        "Authorization": "SAPISIDHASH stale",
    }
    result = _ensure_authorization_header(
        headers, cookies={"SAPISID": "fresh", "HSID": "h1"}
    )
    cookie_keys = [k for k in result if k.lower() == "cookie"]
    assert cookie_keys == ["Cookie"]
    assert "SAPISID=fresh" in result["Cookie"]
    assert "stale" not in result["Cookie"]
    assert "stale" not in result["Authorization"]


# ---------------------------------------------------------------------------
# OpenAI：finish_reason / role=function / stop 空串 / tools 空列表
# ---------------------------------------------------------------------------


def test_openai_finish_reason_other_maps_to_length() -> None:
    """OTHER（流截断信号）不得伪装成正常 stop。"""
    assert _openai_finish_reason("OTHER", has_tool_calls=False) == "length"
    # 未知枚举也不静默变 stop
    assert _openai_finish_reason("SOME_FUTURE_REASON", has_tool_calls=False) == "length"
    assert _openai_finish_reason("MAX_TOKENS", has_tool_calls=False) == "length"
    assert _openai_finish_reason("STOP", has_tool_calls=False) == "stop"


def test_openai_completion_truncated_finish_is_length() -> None:
    """截断流（OTHER）构造的 completion 必须 finish_reason=length。"""
    resp = _gemini_response([GeminiPartResponse(text="被截断的回答")], finish="OTHER")
    payload = build_openai_completion(resp, "gpt-4o")
    assert payload["choices"][0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_openai_role_function_message_converted() -> None:
    """旧版 role=function 消息转成 functionResponse（不再降级为 user 文本）。"""
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "查天气"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "function", "name": "get_weather", "content": '{"ok": 1}'},
            ],
        }
    )
    gemini_req, _ = await openai_to_gemini_request(req)
    last = gemini_req.contents[-1]
    assert last.role == "user"
    fr = last.parts[0].functionResponse
    assert fr is not None
    assert fr.name == "get_weather"
    assert fr.response == {"ok": 1}


@pytest.mark.asyncio
async def test_openai_empty_stop_string_filtered() -> None:
    """空串停止词被过滤，不产出 [""] 形态的 stopSequences。"""
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stop": "",
        }
    )
    gemini_req, _ = await openai_to_gemini_request(req)
    assert gemini_req.generationConfig is None or not getattr(
        gemini_req.generationConfig, "stopSequences", None
    )


def test_openai_tools_responses_top_level_shape() -> None:
    """Responses 顶层形态的 function tool 保留 description/parameters。"""
    tools = [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get current weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    result = _openai_tools_to_gemini(tools)
    assert result is not None
    decls = result[0].functionDeclarations
    assert isinstance(decls, list) and len(decls) == 1
    decl = decls[0]
    assert decl["name"] == "get_weather"
    assert decl["description"] == "Get current weather"
    assert decl["parameters"]["required"] == ["city"]
    # Chat 形态（function 子对象）仍兼容
    chat_tools = [
        {
            "type": "function",
            "function": {
                "name": "f",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    chat_result = _openai_tools_to_gemini(chat_tools)
    assert chat_result is not None
    assert chat_result[0].functionDeclarations[0]["parameters"] == {
        "type": "object",
        "properties": {},
    }


@pytest.mark.asyncio
async def test_openai_explicit_empty_tools_not_injected() -> None:
    """显式 tools: [] 表示禁用工具，不触发 default_tools 注入。"""
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [],
        }
    )
    gemini_req, _ = await openai_to_gemini_request(req)
    # 空列表 → 空 tools（[] 而非 None），normalize 层跳过 default_tools 注入
    assert gemini_req.tools == []


# ---------------------------------------------------------------------------
# Claude：分片 functionCall 跨帧拼接 / stop sequence 截断
# ---------------------------------------------------------------------------


async def _run_claude_stream(
    frames: list[dict[str, object]], stop_sequences: list[str] | None = None
) -> list[dict[str, object]]:
    raw = ""
    async for chunk in claude_stream_adapter(
        _sse_frames(frames),
        requested_model="claude-sonnet-4-5",
        stop_sequences=stop_sequences,
    ):
        raw += chunk
    return _parse_sse_events(raw)


@pytest.mark.asyncio
async def test_claude_stream_sharded_args_merged() -> None:
    """args 字符串分片合并为单个 tool_use 块，input 完整可解析。"""
    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "c1",
                                    "name": "get_weather",
                                    "args": '{"city": "Tok',
                                }
                            }
                        ]
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "c1",
                                    "name": "get_weather",
                                    "args": 'yo", "unit": "c"}',
                                }
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        },
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        },
    ]
    events = await _run_claude_stream(frames)
    starts = [
        e
        for e in events
        if e.get("type") == "content_block_start"
        and e.get("content_block", {}).get("type") == "tool_use"
    ]
    assert len(starts) == 1, f"分片被拆成多个 tool_use 块: {starts}"
    deltas = [
        e
        for e in events
        if e.get("type") == "content_block_delta"
        and e.get("delta", {}).get("type") == "input_json_delta"
    ]
    assert len(deltas) == 1
    merged: dict[str, Any] = json.loads(deltas[0]["delta"]["partial_json"])
    assert merged == {"city": "Tokyo", "unit": "c"}
    # stop_reason = tool_use
    message_delta = [e for e in events if e.get("type") == "message_delta"][-1]
    assert message_delta["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_claude_stream_full_resend_deduped() -> None:
    """同 call_id 全量重发帧合并为单个块，不产生重复 id。"""
    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "c1",
                                    "name": "write_file",
                                    "args": {"content": "abc"},
                                }
                            }
                        ]
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "c1",
                                    "name": "write_file",
                                    "args": {"content": "abcdef"},
                                }
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        },
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        },
    ]
    events = await _run_claude_stream(frames)
    starts = [
        e
        for e in events
        if e.get("type") == "content_block_start"
        and e.get("content_block", {}).get("type") == "tool_use"
    ]
    assert len(starts) == 1
    deltas = [
        e
        for e in events
        if e.get("type") == "content_block_delta"
        and e.get("delta", {}).get("type") == "input_json_delta"
    ]
    assert len(deltas) == 1
    merged: dict[str, Any] = json.loads(deltas[0]["delta"]["partial_json"])
    assert merged == {"content": "abcdef"}  # 后到全量覆盖


@pytest.mark.asyncio
async def test_claude_stream_text_then_tool_then_text_indexes() -> None:
    """文本 → 工具 → 文本交错时 block index 连续且三事件配对。"""

    def text_frame(t: str) -> dict[str, object]:
        return {"candidates": [{"content": {"parts": [{"text": t}]}}]}

    def fc_frame(name: str, args: object) -> dict[str, object]:
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"functionCall": {"id": "x", "name": name, "args": args}}
                        ]
                    }
                }
            ]
        }

    frames = [
        text_frame("Hello "),
        fc_frame("get_weather", {"city": "Tokyo"}),
        text_frame(" done"),
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        },
    ]
    events = await _run_claude_stream(frames)
    starts = [e for e in events if e.get("type") == "content_block_start"]
    assert [s["index"] for s in starts] == [0, 1, 2]
    assert starts[0]["content_block"]["type"] == "text"
    assert starts[1]["content_block"]["type"] == "tool_use"
    assert starts[2]["content_block"]["type"] == "text"
    stops = [e for e in events if e.get("type") == "content_block_stop"]
    assert [s["index"] for s in stops] == [0, 1, 2]


@pytest.mark.asyncio
async def test_claude_stream_stop_sequence_mid_text_truncated() -> None:
    """stop sequence 命中在文本中间：截断其后内容并报 stop_sequence。"""
    frames = [
        {"candidates": [{"content": {"parts": [{"text": "Hello END more text"}]}}]},
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 2,
                "totalTokenCount": 5,
            },
        },
    ]
    events = await _run_claude_stream(frames, stop_sequences=["END"])
    text_deltas = [
        e["delta"]["text"]
        for e in events
        if e.get("type") == "content_block_delta"
        and e.get("delta", {}).get("type") == "text_delta"
    ]
    assert "".join(text_deltas) == "Hello ", "stop seq 之后的文本必须截断"
    message_delta = [e for e in events if e.get("type") == "message_delta"][-1]
    assert message_delta["delta"]["stop_reason"] == "stop_sequence"
    assert message_delta["delta"]["stop_sequence"] == "END"


@pytest.mark.asyncio
async def test_claude_stream_other_maps_to_max_tokens() -> None:
    """OTHER（截断）在 Claude 侧报 max_tokens 而非 end_turn。"""
    frames = [
        {"candidates": [{"content": {"parts": [{"text": "部分回答"}]}}]},
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "OTHER"}],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 2,
                "totalTokenCount": 5,
            },
        },
    ]
    events = await _run_claude_stream(frames)
    message_delta = [e for e in events if e.get("type") == "message_delta"][-1]
    assert message_delta["delta"]["stop_reason"] == "max_tokens"


def test_build_claude_message_str_args_coerced() -> None:
    """非流式 tool_use.input 收到字符串分片时兜底为合法对象。"""
    resp = _gemini_response(
        [
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="get_weather", args='{"city": "Tok'
                )
            )
        ],
        finish="STOP",
    )
    payload = build_claude_message(resp, "claude-sonnet-4-5", None)
    tool_blocks = [b for b in payload["content"] if b["type"] == "tool_use"]
    assert len(tool_blocks) == 1
    assert isinstance(tool_blocks[0]["input"], dict)
    assert tool_blocks[0]["input"] == {"input": '{"city": "Tok'}
    assert payload["stop_reason"] == "tool_use"


# ---------------------------------------------------------------------------
# Responses：output_index / output_text 历史 / 必填字段 / 快照顺序
# ---------------------------------------------------------------------------


async def _run_responses_stream(
    frames: list[dict[str, object]],
) -> list[dict[str, object]]:
    raw = ""
    async for chunk in responses_stream_adapter(
        _sse_frames(frames), requested_model="gpt-5"
    ):
        raw += chunk
    return _parse_sse_events(raw)


@pytest.mark.asyncio
async def test_responses_stream_reasoning_then_text_output_index() -> None:
    """reasoning(0) → message(1) 交错流：reasoning 关闭事件报自己的 index 0。"""
    frames = [
        {
            "candidates": [
                {"content": {"parts": [{"text": "thinking...", "thought": True}]}}
            ]
        },
        {"candidates": [{"content": {"parts": [{"text": "Answer"}]}}]},
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 4,
                "totalTokenCount": 9,
            },
        },
    ]
    events = await _run_responses_stream(frames)
    added = [e for e in events if e.get("type") == "response.output_item.added"]
    assert [(a["output_index"], a["item"]["type"]) for a in added] == [
        (0, "reasoning"),
        (1, "message"),
    ]
    # reasoning 的 done 事件必须报 0（此前误报 1）
    rs_done = [
        e for e in events if e.get("type") == "response.reasoning_summary_text.done"
    ]
    assert rs_done and rs_done[0]["output_index"] == 0
    part_done = [
        e for e in events if e.get("type") == "response.reasoning_summary_part.done"
    ]
    assert part_done and part_done[0]["output_index"] == 0
    item_done = [
        e
        for e in events
        if e.get("type") == "response.output_item.done"
        and e.get("item", {}).get("type") == "reasoning"
    ]
    assert item_done and item_done[0]["output_index"] == 0
    # message 的 delta/done 报 1
    msg_deltas = [e for e in events if e.get("type") == "response.output_text.delta"]
    assert msg_deltas and msg_deltas[0]["output_index"] == 1
    # sequence_number 连续从 0 递增
    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(len(seqs)))


@pytest.mark.asyncio
async def test_responses_stream_text_then_reasoning_delta_index() -> None:
    """message(0) 先开、reasoning(1) 后开：message 的 delta 仍报 0。"""
    frames = [
        {"candidates": [{"content": {"parts": [{"text": "Answer"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "hmm", "thought": True}]}}]},
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 4,
                "totalTokenCount": 9,
            },
        },
    ]
    events = await _run_responses_stream(frames)
    msg_deltas = [e for e in events if e.get("type") == "response.output_text.delta"]
    assert msg_deltas and msg_deltas[0]["output_index"] == 0
    rs_deltas = [
        e for e in events if e.get("type") == "response.reasoning_summary_text.delta"
    ]
    assert rs_deltas and rs_deltas[0]["output_index"] == 1


@pytest.mark.asyncio
async def test_responses_stream_completed_snapshot_ordered() -> None:
    """completed 快照 output 顺序与流中 output_index 对应。"""
    fc = {"functionCall": {"id": "call_1", "name": "f", "args": {"x": 1}}}
    frames = [
        {"candidates": [{"content": {"parts": [fc]}}]},
        {"candidates": [{"content": {"parts": [{"text": "Answer"}]}}]},
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 4,
                "totalTokenCount": 9,
            },
        },
    ]
    events = await _run_responses_stream(frames)
    completed = [e for e in events if e.get("type") == "response.completed"][-1]
    output = completed["response"]["output"]
    assert [item["type"] for item in output] == ["function_call", "message"]
    # SDK 必填字段
    resp_obj = completed["response"]
    assert resp_obj["tools"] == []
    assert resp_obj["tool_choice"] == "auto"
    assert resp_obj["parallel_tool_calls"] is False
    assert resp_obj["usage"]["input_tokens_details"]["cache_write_tokens"] == 0


@pytest.mark.asyncio
async def test_responses_stream_error_closes_open_items() -> None:
    """中途错误：先关闭已打开的 message item 再发 response.failed。"""
    frames = [
        {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]},
        {"error": {"code": 429, "message": "rate limited"}},
    ]
    events = await _run_responses_stream(frames)
    types = [e.get("type") for e in events]
    assert "response.failed" in types
    # message item 必须在 failed 之前 done
    msg_done_idx = next(
        (
            i
            for i, e in enumerate(events)
            if e.get("type") == "response.output_item.done"
            and e.get("item", {}).get("type") == "message"
        ),
        None,
    )
    failed_idx = types.index("response.failed")
    assert msg_done_idx is not None and msg_done_idx < failed_idx


@pytest.mark.asyncio
async def test_responses_request_output_text_history_preserved() -> None:
    """assistant 历史回传 output_text 块不再被丢弃。"""
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gpt-5",
            "input": [
                {"role": "user", "content": "hi"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Hello! How can I help?"}
                    ],
                },
                {"role": "user", "content": "more"},
            ],
        }
    )
    gemini_req, _ = await responses_to_gemini_request(req)
    assistant_turn = gemini_req.contents[1]
    assert assistant_turn.role == "model"
    text = assistant_turn.parts[0].text
    assert text == "Hello! How can I help?"


@pytest.mark.asyncio
async def test_responses_request_system_content_array() -> None:
    """system/developer 消息 content 为块数组时不再被整条丢弃。"""
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gpt-5",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {"type": "input_text", "text": "You are a calculator."}
                    ],
                },
                {"role": "user", "content": "1+1=?"},
            ],
        }
    )
    gemini_req, _ = await responses_to_gemini_request(req)
    first = gemini_req.contents[0]
    assert first.role == "user"
    assert "You are a calculator." in first.parts[0].text


# ---------------------------------------------------------------------------
# model_mapping 通配符
# ---------------------------------------------------------------------------


def test_model_mapping_wildcard_key_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """通配符键 "claude-sonnet-*" 能命中 "claude-sonnet-4-5-20250929"。"""
    monkeypatch.setattr(
        "aistudio_api.application.compat_service.get_compat_settings",
        lambda: {
            "claude_default_model": "gemini-fallback",
            "claude_model_mapping": {"claude-sonnet-*": "gemini-4-argon"},
        },
    )
    resolved = resolve_compat_model("claude-sonnet-4-5-20250929", channel="claude")
    assert resolved == "gemini-4-argon"


# ---------------------------------------------------------------------------
# count_tokens：tools 计入 + data URL shrink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_tokens_includes_tools_and_data_url_shrink() -> None:
    """count_tokens 计入 tools 声明；data URL 形式媒体被 shrink。

    直接验证估算管线的输入构造（routes 的 payload 组装 + shrink + 估算
    函数），不启动完整 app（TestClient 会拉起后台任务污染其他测试）。
    """
    from aistudio_api.api.routes_compat import _estimate_tokens, _shrink_media_payloads

    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 100,
            "tools": [
                {
                    "name": f"tool_{i}",
                    "description": "x" * 20,
                    "input_schema": {"type": "object", "properties": {}},
                }
                for i in range(20)
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "data:image/png;base64," + "E" * 400000,
                            },
                        },
                        {"type": "text", "text": "hi"},
                    ],
                }
            ],
        }
    )
    # 与 claude_count_tokens 路由相同的 payload 组装逻辑
    payload = {
        "system": _shrink_media_payloads(req.system),
        "tools": _shrink_media_payloads(req.tools),
        "messages": [
            {"role": m.role, "content": _shrink_media_payloads(m.content)}
            for m in req.messages
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    estimate = max(1, _estimate_tokens(serialized))
    # 20 个工具声明 ~数百 token；若 tools 被漏计只剩 ~30
    assert estimate > 200, f"tools 未计入估算: {estimate}"
    # data URL 未 shrink 时估算会到 ~10 万
    assert estimate < 3000, f"data URL 未被 shrink: {estimate}"


# ---------------------------------------------------------------------------
# 兼容性基线：既有核心行为不回归
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_multi_tool_stream_indexes_stable() -> None:
    """多 functionCall 帧的 tool_calls index 稳定递增（OpenAI 流式）。"""
    from aistudio_api.application.compat_service import openai_stream_adapter

    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "a",
                                    "name": "f1",
                                    "args": {"x": 1},
                                }
                            }
                        ]
                    }
                }
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "b",
                                    "name": "f2",
                                    "args": {"y": 2},
                                }
                            }
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        },
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        },
    ]
    raw = ""
    async for chunk in openai_stream_adapter(
        _sse_frames(frames), requested_model="gpt-4o", include_usage=True
    ):
        raw += chunk
    chunks = _parse_sse_events(raw)
    tool_chunks = [
        c
        for c in chunks
        if c.get("choices") and c["choices"][0].get("delta", {}).get("tool_calls")
    ]
    assert [
        c["choices"][0]["delta"]["tool_calls"][0]["index"] for c in tool_chunks
    ] == [
        0,
        1,
    ]
    final = [c for c in chunks if c.get("choices") and c["choices"][0]["finish_reason"]]
    assert final[0]["choices"][0]["finish_reason"] == "tool_calls"
