"""工具调用链路审计修复的回归测试（B1-B4，2026-11-15）。

对应修复：
- B1 thoughtSignature 全路径校验（纯签名 part / FR 子部件 / executableCode /
  codeExecutionResult / systemInstruction 此前裸传，伪签名触发上游
  400 "Corrupted thought signature."）
- B2 流式参数分片被独立文本帧打断 → 作废调用+告警而非整流硬失败；
  终结时若全流无任何已投递内容仍报错（空回防护同哲学）
- B3 签名缓存 remember 挂钩补全（Responses 非流式 / 原生 Gemini 非流式与
  流式，此前只取不存）
- B4 parallel_tool_calls=false 从 400 拒绝改为接受+告警忽略
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from aistudio_api.api.schemas import (
    GeminiCodeExecutionResult,
    GeminiContent,
    GeminiExecutableCode,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiInlineData,
    GeminiPart,
)
from aistudio_api.api.schemas_compat import (
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application import api_service_gemini as gemini_service
from aistudio_api.application.compat_service import openai_to_gemini_request
from aistudio_api.application.responses_service import (
    build_responses_object,
    responses_to_gemini_request,
)
from aistudio_api.application.stream_utils import coalesce_tool_frames
from aistudio_api.domain.models import Candidate, ModelOutput

VALID_SIG = base64.b64encode(b"native_sig_payload").decode()
INVALID_SIG = "not-a-valid-base64!!!"


def _norm(contents, model="gemini-3.8-flash", **kwargs):
    return gemini_service.normalize_gemini_request(
        GeminiGenerateContentRequest(contents=contents, **kwargs), model
    )


def _wire_signatures(normalized) -> list[object]:
    return [
        part[14]
        for content in normalized.contents
        for part in content.to_wire()[0]
        if len(part) > 14 and part[14] is not None
    ]


# ---------------------------------------------------------------------------
# B1: thoughtSignature 校验覆盖所有直达 wire 的路径
# ---------------------------------------------------------------------------


def test_b1_invalid_signature_only_part_is_dropped():
    """纯签名 part 的伪签名被过滤，不再直达 wire 槽位 14。"""
    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="hi")]),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        functionCall=GeminiFunctionCall(name="f", args={"a": 1}),
                        thoughtSignature=INVALID_SIG,
                    ),
                    GeminiPart(thoughtSignature="%%%corrupted%%%"),
                ],
            ),
            GeminiContent(
                role="user",
                parts=[
                    GeminiPart(
                        functionResponse=GeminiFunctionResponse(
                            name="f", response={"ok": 1}
                        )
                    )
                ],
            ),
        ]
    )
    sigs = _wire_signatures(normalized)
    assert INVALID_SIG not in sigs
    assert "%%%corrupted%%%" not in sigs
    assert not sigs  # 规范化只保留明确提供的合规签名。


def test_b1_valid_signature_only_part_is_preserved():
    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="hi")]),
            GeminiContent(
                role="model",
                parts=[GeminiPart(text="", thoughtSignature=VALID_SIG)],
            ),
            # 尾部 user 轮：纯签名 model 轮在中间，不被续写改写剥离
            GeminiContent(role="user", parts=[GeminiPart(text="go on")]),
        ]
    )
    assert VALID_SIG in _wire_signatures(normalized)


def test_b1_function_response_subpart_signature_validated():
    """FR 子部件（工具结果附图）的伪签名被过滤，图片本身保留。"""
    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="hi")]),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        functionCall=GeminiFunctionCall(name="f", args={"a": 1}),
                        thoughtSignature=VALID_SIG,
                    )
                ],
            ),
            GeminiContent(
                role="user",
                parts=[
                    GeminiPart(
                        functionResponse=GeminiFunctionResponse(
                            name="f",
                            response={"ok": 1},
                            parts=[
                                GeminiPart(
                                    inlineData=GeminiInlineData(
                                        mimeType="image/png", data="aGk="
                                    ),
                                    thoughtSignature="@@@invalid@@@",
                                )
                            ],
                        )
                    )
                ],
            ),
        ]
    )
    sigs = _wire_signatures(normalized)
    assert not any(isinstance(s, str) and "@@@" in s for s in sigs)
    # 附图载荷保留
    inlines = [
        part.inline_data
        for content in normalized.contents
        for part in content.parts
        if part.inline_data is not None
    ]
    assert inlines == [("image/png", "aGk=")]


def test_b1_executable_code_signatures_validated():
    """executableCode / codeExecutionResult 部件的伪签名被过滤，代码保留。"""
    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="run")]),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        executableCode=GeminiExecutableCode(
                            language="PYTHON", code="print(1)"
                        ),
                        thoughtSignature=INVALID_SIG,
                    ),
                    GeminiPart(
                        codeExecutionResult=GeminiCodeExecutionResult(
                            outcome="OUTCOME_OK", output="1"
                        ),
                        thoughtSignature=INVALID_SIG,
                    ),
                ],
            ),
            # 尾部 user 轮：model 轮在中间，代码部件不被续写改写拼前缀
            GeminiContent(role="user", parts=[GeminiPart(text="go on")]),
        ]
    )
    for content in normalized.contents:
        for part in content.parts:
            assert part.thought_signature is None
    assert normalized.contents[1].parts[0].executable_code == ("PYTHON", "print(1)")
    assert normalized.contents[1].parts[1].code_execution_result == (
        "OUTCOME_OK",
        "1",
    )


def test_b1_system_instruction_signature_validated():
    """systemInstruction 的伪签名被过滤，合法签名保留。"""
    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            "systemInstruction": {
                "role": "user",
                "parts": [
                    {"text": "sys prompt", "thoughtSignature": INVALID_SIG},
                    {"text": "more", "thoughtSignature": VALID_SIG},
                ],
            },
        }
    )
    normalized = gemini_service.normalize_gemini_request(req, "gemini-3.8-flash")
    assert normalized.system_instruction is not None
    parts = normalized.system_instruction.parts
    assert [p.thought_signature for p in parts] == [None, VALID_SIG]
    assert [p.text for p in parts] == ["sys prompt", "more"]


# ---------------------------------------------------------------------------
# B2: 分片被内容边界打断 → 降级作废，不再整流硬失败
# ---------------------------------------------------------------------------


def _frames(*frames: dict) -> AsyncGenerator[dict[str, object], None]:
    async def source() -> AsyncGenerator[dict[str, object], None]:
        for item in frames:
            yield item

    return source()


def _parts_of(frames: list[dict]) -> list[dict]:
    return [
        part
        for frame in frames
        for candidate in frame.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    ]


async def test_b2_text_frame_interrupting_shards_degrades_instead_of_raising():
    """独立文本帧插入参数分片中间：调用作废+告警，文本帧照常下发。"""
    frames = [
        frame
        async for frame in coalesce_tool_frames(
            _frames(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "get_weather",
                                            "args": '{"ci',
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": [{"text": "Let me check."}]}}]},
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"functionCall": {"args": 'ty":"Tokyo"}'}}]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
            )
        )
    ]
    texts = [p.get("text") for p in _parts_of(frames) if "text" in p]
    assert "Let me check." in texts
    calls = [p for p in _parts_of(frames) if "functionCall" in p]
    assert calls == []


async def test_b2_terminal_incomplete_with_delivered_text_degrades():
    """已有正文投递后，终结时的残缺调用同样降级（不连累已投递正文）。"""
    frames = [
        frame
        async for frame in coalesce_tool_frames(
            _frames(
                {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]},
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "w",
                                            "args": '{"a":',
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
            )
        )
    ]
    assert "answer" in [p.get("text") for p in _parts_of(frames)]


async def test_b2_terminal_incomplete_without_any_content_still_raises():
    """全流唯一载荷是残缺调用（无任何已投递内容）时保持报错，防空回。"""
    with pytest.raises(ValueError, match="Incomplete tool arguments"):
        [
            frame
            async for frame in coalesce_tool_frames(
                _frames(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "id": "c",
                                                "name": "write",
                                                "args": '{"path":"partial',
                                            }
                                        }
                                    ]
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                )
            )
        ]


# ---------------------------------------------------------------------------
# 混合 id 分片链（2026-11-26 harness 审查）：首帧无 id、续帧携带真实 id
# （JSPB 渐进填充形态，anti_truncation 的 call_id 补填分支同源）。旧行为
# 把链拆成两条都不完整的调用双双作废；修复后并入合成 id 链并采纳真实 id。
# ---------------------------------------------------------------------------


async def test_mixed_id_shard_chain_merges_into_one_call():
    """首帧无 id、续帧带真实 id：合并为单个完整调用，id 为真实 id。"""
    frames = [
        frame
        async for frame in coalesce_tool_frames(
            _frames(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "get_weather",
                                            "args": '{"city":',
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
                                            "id": "call_1786466761448",
                                            "name": "get_weather",
                                            "args": ' "Tokyo"}',
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
            )
        )
    ]
    calls = [p["functionCall"] for p in _parts_of(frames) if "functionCall" in p]
    assert len(calls) == 1, f"混合 id 分片链断裂: {calls}"
    assert calls[0]["id"] == "call_1786466761448"
    assert calls[0]["args"] == {"city": "Tokyo"}


async def test_mixed_id_chain_continues_with_same_id_fragments():
    """真实 id 采纳后：后续同 id 分片经 by_id 命中继续累积。"""
    frames = [
        frame
        async for frame in coalesce_tool_frames(
            _frames(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"functionCall": {"name": "w", "args": '{"a":'}}
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
                                    {"functionCall": {"id": "real_1", "args": '"x",'}}
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
                                    {"functionCall": {"id": "real_1", "args": '"b":1}'}}
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
            )
        )
    ]
    calls = [p["functionCall"] for p in _parts_of(frames) if "functionCall" in p]
    assert len(calls) == 1
    assert calls[0]["id"] == "real_1"
    assert calls[0]["args"] == {"a": "x", "b": 1}


async def test_mixed_id_adoption_never_joins_real_id_chains():
    """并行同名函数分片：真实 id 的未完成链不被后续不同 id 的分片并入。"""
    frames = [
        frame
        async for frame in coalesce_tool_frames(
            _frames(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "call_A",
                                            "name": "weather",
                                            "args": '{"city":"Par',
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
                                            "id": "call_B",
                                            "name": "weather",
                                            "args": '{"city":"Tok',
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
                                            "id": "call_A",
                                            "args": 'is"}',
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
                                            "id": "call_B",
                                            "args": 'yo"}',
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
            )
        )
    ]
    calls = [p["functionCall"] for p in _parts_of(frames) if "functionCall" in p]
    assert len(calls) == 2
    by_id = {call["id"]: call for call in calls}
    assert by_id["call_A"]["args"] == {"city": "Paris"}
    assert by_id["call_B"]["args"] == {"city": "Tokyo"}


async def test_mixed_id_dict_snapshot_completes_chain():
    """混合链的续帧是完整 dict 快照：整体替换分片，调用以真实 id 完成。"""
    frames = [
        frame
        async for frame in coalesce_tool_frames(
            _frames(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "get_weather",
                                            "args": '{"city": "sta',
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
                                            "id": "call_dict",
                                            "name": "get_weather",
                                            "args": {"city": "Tokyo"},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
            )
        )
    ]
    calls = [p["functionCall"] for p in _parts_of(frames) if "functionCall" in p]
    assert len(calls) == 1
    assert calls[0]["id"] == "call_dict"
    assert calls[0]["args"] == {"city": "Tokyo"}


async def test_stable_id_uses_latest_snapshot_before_publication():
    """No call was published yet, so a later same-ID correction can win."""
    frames = [
        frame
        async for frame in coalesce_tool_frames(
            _frames(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "stable",
                                            "name": "weather",
                                            "args": {"city": "old"},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": [{"text": "checking"}]}}]},
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "stable",
                                            "name": "weather",
                                            "args": {"city": "new"},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]},
            )
        )
    ]
    calls = [p["functionCall"] for p in _parts_of(frames) if "functionCall" in p]
    assert len(calls) == 1
    assert calls[0]["args"] == {"city": "new"}


# ---------------------------------------------------------------------------
# B3: 签名缓存 remember 挂钩覆盖非流式 Responses 与原生 Gemini 路径
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_signature_cache(monkeypatch: pytest.MonkeyPatch):
    from aistudio_api.domain import tool_signatures

    cache = tool_signatures.ThoughtSignatureCache(path=None)
    monkeypatch.setattr(tool_signatures, "get_signature_cache", lambda: cache)
    return cache


def test_b3_responses_non_streaming_remembers_signature(fresh_signature_cache):
    """非流式 /v1/responses 的 functionCall 签名回填缓存（此前只取不存）。"""
    from aistudio_api.api.response_models import GeminiGenerateContentResponse

    response = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "get_weather",
                                    "args": {"city": "Paris"},
                                    "id": "call_1",
                                },
                                "thoughtSignature": VALID_SIG,
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    payload = build_responses_object(response, "gemini-3.8-flash")
    assert payload["status"] == "completed"
    assert (
        fresh_signature_cache.lookup("call_1", "get_weather", {"city": "Paris"})
        == VALID_SIG
    )


def test_b3_native_non_streaming_remembers_signature(fresh_signature_cache):
    """原生 /v1beta 非流式（含抗截断缓冲变体）的签名回填缓存。"""
    output = ModelOutput(
        candidates=[
            Candidate(
                function_calls=[
                    {
                        "type": "functionCall",
                        "name": "get_weather",
                        "args": {"city": "Paris"},
                        "call_id": "call_2",
                        "thought_signature": VALID_SIG,
                    }
                ],
                finish_reason=1,
            )
        ]
    )
    resp = gemini_service._build_gemini_response_from_output(output, "gemini-3.8-flash")
    assert resp.candidates[0].content is not None
    assert (
        fresh_signature_cache.lookup("call_2", "get_weather", {"city": "Paris"})
        == VALID_SIG
    )


async def test_b3_native_streaming_remembers_signature(
    monkeypatch: pytest.MonkeyPatch, fresh_signature_cache
):
    """原生流式 tool_calls 事件的签名回填缓存。"""
    from types import SimpleNamespace

    monkeypatch.setattr(gemini_service, "ensure_active_account", AsyncMock())

    async def generate(**kwargs):
        yield (
            "tool_calls",
            [
                {
                    "type": "functionCall",
                    "name": "get_weather",
                    "args": {"city": "Paris"},
                    "call_id": "call_3",
                    "thought_signature": VALID_SIG,
                }
            ],
        )
        yield "finish_reason", 1
        yield "usage", None

    response = gemini_service._build_gemini_streaming_response(
        client=cast(Any, SimpleNamespace(stream_generate_content=generate)),
        req=GeminiGenerateContentRequest.model_validate(
            {"contents": [{"parts": [{"text": "hi"}]}]}
        ),
        model_path="gemini-3.8-flash",
        anti_truncation=False,
    )
    frames = [frame async for frame in response.frames]
    calls = [
        part.get("functionCall")
        for frame in frames
        for candidate in cast(list[dict], frame.get("candidates", []))
        for part in candidate["content"]["parts"]
        if "functionCall" in part
    ]
    assert calls and calls[0]["name"] == "get_weather"
    assert (
        fresh_signature_cache.lookup("call_3", "get_weather", {"city": "Paris"})
        == VALID_SIG
    )


# ---------------------------------------------------------------------------
# B4: parallel_tool_calls=false 接受+告警，不再 400
# ---------------------------------------------------------------------------


async def test_b4_openai_parallel_tool_calls_false_accepted():
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "parallel_tool_calls": False,
        }
    )
    gemini_req, resolved = await openai_to_gemini_request(req)
    assert gemini_req.contents
    assert resolved


async def test_b4_responses_parallel_tool_calls_false_accepted():
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gpt-4o-mini",
            "input": "hi",
            "parallel_tool_calls": False,
        }
    )
    gemini_req, resolved = await responses_to_gemini_request(req)
    assert gemini_req.contents
    assert resolved
