"""harness 接入链路优化回归测试（2026-11-26 审查）。

覆盖四项优化：

1. 非流式合成 tool id（toolu_/call_）后补记签名缓存——对齐流式路径
   （coalesce 先定 id、recorder 后记忆化），客户端丢失 extra_content
   载体时按 id 回查仍可命中；
2. Claude SSE message_start 携带真实 input_tokens（等首帧 usageMetadata，
   Anthropic 官方语义；旧版 SDK 不合并 message_delta 的 input 字段）；
3. build_claude_message 合并连续 thought part 为单 thinking 块——对齐
   流式形态；
4. tool_choice.disable_parallel_tool_use 忽略告警。
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiFunctionCallPayload,
    GeminiGenerateContentResponse,
    GeminiPartResponse,
)
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
)
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.compat_claude import (
    _claude_blocks_to_parts,
    _claude_tool_choice_to_config,
    build_claude_message,
    claude_stream_adapter,
    claude_to_gemini_request,
)
from aistudio_api.application.compat_openai import build_openai_completion
from aistudio_api.domain import tool_signatures
from aistudio_api.infrastructure.gateway.model_turns import ModelTurnHistory

_SIG = base64.b64encode(b"upstream-signature-fixture-0123456789").decode()
_SIG2 = base64.b64encode(b"second-signature-fixture-abcdefghij").decode()


@pytest.fixture
def fresh_signature_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):
    cache = tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json")
    monkeypatch.setattr(tool_signatures, "get_signature_cache", lambda: cache)
    return cache


def _gemini_response(parts: list[GeminiPartResponse]) -> GeminiGenerateContentResponse:
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(parts=parts),
                finishReason="STOP",
                index=0,
            )
        ],
    )


def _blocks(message: dict[str, object]) -> list[dict[str, Any]]:
    """build_claude_message 返回 dict[str, object]：content 块列表收窄类型。"""
    return cast(list[dict[str, Any]], message["content"])


# ---------------------------------------------------------------------------
# Fix 1：非流式合成 tool id 补记签名缓存
# ---------------------------------------------------------------------------


def test_claude_synthetic_toolu_id_cached(fresh_signature_cache) -> None:
    """上游未带 call id：合成 toolu_ id 也必须可查到签名缓存。

    旧实现合成 id 发生在 _iter_response_parts 记忆化之后，缓存无此条目
    ——客户端（Claude Code TS 侧可能剥离未知字段）不回传 extra_content
    载体时按 id 回查 miss，签名退化为 SKIP 占位。
    """
    response = _gemini_response(
        [
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="Bash", args={"command": "ls"}
                ),
                thoughtSignature=_SIG,
            )
        ]
    )
    message = build_claude_message(response, "claude-sonnet-4-5", None)
    tool_use = next(b for b in _blocks(message) if b["type"] == "tool_use")
    assert tool_use["id"].startswith("toolu_")
    assert (
        fresh_signature_cache.lookup(tool_use["id"], "Bash", {"command": "ls"}) == _SIG
    )


def test_claude_upstream_toolu_id_not_duplicated(fresh_signature_cache) -> None:
    """上游已带 call id：_iter_response_paths 已记忆化，无需也不重复补记。"""
    response = _gemini_response(
        [
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="Bash", args={"command": "ls"}, id="toolu_upstream1"
                ),
                thoughtSignature=_SIG,
            )
        ]
    )
    build_claude_message(response, "claude-sonnet-4-5", None)
    assert (
        fresh_signature_cache.lookup("toolu_upstream1", "Bash", {"command": "ls"})
        == _SIG
    )


def test_openai_synthetic_call_id_cached(fresh_signature_cache) -> None:
    """OpenAI 非流式同款：合成 call_ id 补记签名缓存。"""
    response = _gemini_response(
        [
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="get_weather", args={"city": "Paris"}
                ),
                thoughtSignature=_SIG,
            )
        ]
    )
    completion = build_openai_completion(response, "gpt-5.1")
    choice = cast(dict[str, Any], cast(list[object], completion["choices"])[0])
    completion_message = cast(dict[str, Any], choice["message"])
    tool_call = cast(
        dict[str, Any], cast(list[object], completion_message["tool_calls"])[0]
    )
    assert tool_call["id"].startswith("call_")
    assert (
        fresh_signature_cache.lookup(tool_call["id"], "get_weather", {"city": "Paris"})
        == _SIG
    )


def test_synthetic_id_cache_skips_invalid_signature(fresh_signature_cache) -> None:
    """part 签名非法（伪造/占位）时补记静默跳过（remember 内部校验）。"""
    response = _gemini_response(
        [
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="Bash", args={"command": "ls"}
                ),
                thoughtSignature="not-a-valid-base64!!!",
            )
        ]
    )
    message = build_claude_message(response, "claude-sonnet-4-5", None)
    tool_use = next(b for b in _blocks(message) if b["type"] == "tool_use")
    assert (
        fresh_signature_cache.lookup(tool_use["id"], "Bash", {"command": "ls"}) is None
    )


@pytest.mark.parametrize("keep_signature", [False, True])
async def test_synthetic_tool_id_round_trip_to_upstream(
    fresh_signature_cache, keep_signature
) -> None:
    """A synthetic ID alone cannot turn a legacy record into replay evidence."""
    response = _gemini_response(
        [
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="Bash", args={"command": "ls"}
                ),
                thoughtSignature=_SIG,
            )
        ]
    )
    message = build_claude_message(response, "claude-sonnet-4-5", None)
    tool_use = next(b for b in _blocks(message) if b["type"] == "tool_use")
    replayed = dict(tool_use)
    if not keep_signature:
        replayed.pop("extra_content", None)
    parts = await _claude_blocks_to_parts([replayed], tool_name_by_id={})
    assert parts[0].functionCall is not None
    assert parts[0].functionCall.id == tool_use["id"]
    assert parts[0].thoughtSignature == (_SIG if keep_signature else None)
    request = GeminiGenerateContentRequest(
        contents=[
            GeminiContent(role="model", parts=parts),
            GeminiContent(role="user", parts=[GeminiPart(text="continue")]),
        ]
    )
    normalized = normalize_gemini_request(request, "gemini-3.8-flash")
    outbound, _, _ = ModelTurnHistory().prepare("fixture", normalized.contents)
    assert (outbound[0].parts[0].thought_signature == _SIG) is keep_signature


# ---------------------------------------------------------------------------
# Fix 2：message_start 携带真实 input tokens
# ---------------------------------------------------------------------------


def _sse_frames(frames: list[dict[str, object]]) -> AsyncGenerator[str, None]:
    async def gen() -> AsyncGenerator[str, None]:
        for frame in frames:
            yield "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"

    return gen()


async def _collect_events(frames: list[dict[str, object]]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    async for sse in claude_stream_adapter(
        _sse_frames(frames), requested_model="m", stop_sequences=None
    ):
        for line in sse.splitlines():
            if line.startswith("data: "):
                parsed.append(json.loads(line.removeprefix("data: ")))
    return parsed


async def test_message_start_carries_real_input_tokens() -> None:
    """首帧携带 usageMetadata 时 message_start 的 usage 带真实 input_tokens。"""
    events = await _collect_events(
        [
            {
                "usageMetadata": {
                    "promptTokenCount": 150,
                    "cachedContentTokenCount": 50,
                    "candidatesTokenCount": 0,
                },
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "hi"}]}}
                ],
            },
            {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
        ]
    )
    start = events[0]
    assert start["type"] == "message_start"
    usage = start["message"]["usage"]
    # input 剔除缓存命中部分（与 message_delta 同口径），cache_read 单列
    assert usage["input_tokens"] == 100
    assert usage["cache_read_input_tokens"] == 50
    # Anthropic 惯例：message_start 的 output_tokens 至少为 1
    assert usage["output_tokens"] == 1
    # 事件顺序不变：message_start 仍是首事件，message_delta 收尾带终值
    assert events[1]["type"] == "ping"
    delta = next(e for e in events if e["type"] == "message_delta")
    assert delta["usage"]["input_tokens"] == 100
    assert events[-1]["type"] == "message_stop"


async def test_message_start_falls_back_without_first_frame_usage() -> None:
    """首帧无 usageMetadata / 流为空：退回 0/1 占位，终值仍由 delta 覆盖。"""
    events = await _collect_events(
        [
            {"candidates": [{"content": {"role": "model", "parts": []}}]},
            {
                "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 7},
                "candidates": [{"finishReason": "STOP", "content": {"parts": []}}],
            },
        ]
    )
    start_usage = events[0]["message"]["usage"]
    assert start_usage == {"input_tokens": 0, "output_tokens": 1}
    delta = next(e for e in events if e["type"] == "message_delta")
    assert delta["usage"]["input_tokens"] == 42
    assert delta["usage"]["output_tokens"] == 7


async def test_message_start_error_frame_first_still_starts_message() -> None:
    """首帧即错误帧：message_start 仍先行，随后 error 事件（协议顺序不变）。"""
    events = await _collect_events(
        [{"error": {"code": 429, "message": "rate limited"}}]
    )
    assert events[0]["type"] == "message_start"
    assert any(e["type"] == "error" for e in events)


# ---------------------------------------------------------------------------
# Fix 3：连续 thought part 合并为单 thinking 块
# ---------------------------------------------------------------------------


def test_contiguous_thought_parts_merge_into_one_block() -> None:
    """连续 thought part 合并为单 thinking 块（对齐流式与 Anthropic 形态）。"""
    response = _gemini_response(
        [
            GeminiPartResponse(text="Thinking ", thought=True),
            GeminiPartResponse(text="hard", thought=True),
            GeminiPartResponse(text="The answer"),
        ]
    )
    message = build_claude_message(response, "m", None)
    thinking_blocks = [b for b in _blocks(message) if b["type"] == "thinking"]
    assert len(thinking_blocks) == 1
    assert thinking_blocks[0]["thinking"] == "Thinking hard"


def test_signed_thought_keeps_its_own_part() -> None:
    """Consecutive thinking text must not inherit its neighbor’s signature."""
    response = _gemini_response(
        [
            GeminiPartResponse(text="part one", thought=True),
            GeminiPartResponse(text="part two", thought=True, thoughtSignature=_SIG2),
            GeminiPartResponse(text="The answer"),
        ]
    )
    message = build_claude_message(response, "m", None)
    blocks = [b for b in _blocks(message) if b["type"] == "thinking"]
    from aistudio_api.application.compat_errors import _read_claude_signature

    assert [block["thinking"] for block in blocks] == ["part one", "part two"]
    assert [_read_claude_signature(block["signature"]) for block in blocks] == [
        None,
        _SIG2,
    ]


# ---------------------------------------------------------------------------
# Fix 4：disable_parallel_tool_use 告警
# ---------------------------------------------------------------------------


def test_tool_choice_disable_parallel_tool_use_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """disable_parallel_tool_use 无法在上游强制串行：忽略且必须告警。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="compat.claude"):
        config = _claude_tool_choice_to_config(
            {"type": "auto", "disable_parallel_tool_use": True}
        )
    assert config == {"functionCallingConfig": {"mode": "AUTO"}}
    assert any(
        "disable_parallel_tool_use" in record.message for record in caplog.records
    )


def test_tool_choice_without_flag_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="compat.claude"):
        _claude_tool_choice_to_config({"type": "auto"})
    assert not caplog.records


# ---------------------------------------------------------------------------
# 端到端：Claude Code 形态的完整工具轮回放
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("signed_thought", [False, True])
@pytest.mark.parametrize("keep_tool_signature", [False, True])
async def test_claude_code_tool_turn_replay_keeps_only_known_owners(
    fresh_signature_cache,
    signed_thought,
    keep_tool_signature,
) -> None:
    """An unsigned thought never takes a tool signature; an explicit one survives.

    Without the echoed tool signature the lead FC gets the SKIP sentinel
    (upstream rejects a bare lead FC); a client-provided thought signature is
    forwarded untouched and never borrowed onto the FC.
    """
    expected_thought_signature = _SIG2 if signed_thought else None
    response = _gemini_response(
        [
            GeminiPartResponse(
                text="Thinking hard",
                thought=True,
                thoughtSignature=expected_thought_signature,
            ),
            GeminiPartResponse(
                functionCall=GeminiFunctionCallPayload(
                    name="Bash", args={"command": "ls"}
                ),
                thoughtSignature=_SIG,
            ),
            GeminiPartResponse(text="", thoughtSignature=_SIG),
        ]
    )
    message = build_claude_message(response, "claude-sonnet-4-5", None)
    tool_use = next(b for b in _blocks(message) if b["type"] == "tool_use")
    thinking_block = next(b for b in _blocks(message) if b["type"] == "thinking")
    tool_echo = dict(tool_use)
    if not keep_tool_signature:
        tool_echo.pop("extra_content", None)

    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": [
                        # The real thinking signature, when present, is independent.
                        {
                            "type": "thinking",
                            "thinking": thinking_block["thinking"],
                            "signature": thinking_block["signature"],
                        },
                        tool_echo,
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use["id"],
                            "content": "file1\nfile2",
                        }
                    ],
                },
            ],
        }
    )
    gemini_req, _ = await claude_to_gemini_request(req)
    normalized = normalize_gemini_request(gemini_req, "gemini-2.5-pro")
    outbound, _, _ = ModelTurnHistory().prepare("fixture", normalized.contents)
    model_turn = outbound[1]
    thought_part = next(p for p in model_turn.parts if p.thought)
    assert thought_part.thought_signature == expected_thought_signature
    fc_part = next(p for p in model_turn.parts if p.function_call)
    fc = fc_part.function_call
    assert fc is not None
    assert fc[0] == "Bash"
    if keep_tool_signature:
        assert fc_part.thought_signature == _SIG
    else:
        # 客户端没回传 tool 签名：无论 thought 带不带真实签名，首个 FC 都补 SKIP
        # 哨兵；显式 thought 签名留在 thought part 上（上面已断言），绝不借到 FC
        assert fc_part.thought_signature == tool_signatures.SKIP_THOUGHT_SIGNATURE
    # tool_result 反查名字正确（id 匹配）
    fr_part = next(p for p in outbound[2].parts if p.function_response)
    fr = fr_part.function_response
    assert fr is not None
    assert fr[0] == "Bash"
