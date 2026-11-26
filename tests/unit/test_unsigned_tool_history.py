"""Unsigned preset histories must retain context before and after cache warm-up."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
)
from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.compat_service import (
    claude_to_gemini_request,
    openai_to_gemini_request,
)
from aistudio_api.application.responses_service import responses_to_gemini_request
from aistudio_api.domain import tool_signatures
from aistudio_api.infrastructure.gateway.model_turns import ModelTurnHistory
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

SIGNATURE = base64.b64encode(b"fixture-only-signature").decode()
SENTINEL = "skip_thought_signature_validator"
HISTORY = [
    ("init", "context", {"query": "init_world"}, "WORLD_CONTEXT_世界设定" * 2000),
    (
        "history",
        "reply",
        {"game_content": "PREVIOUS_REPLY_完整历史" * 3000},
        "USER_INPUT_继续",
    ),
    ("sync", "context", {"query": "sync"}, "LATEST_CONTEXT_本轮状态" * 1000),
]


@pytest.fixture(autouse=True)
def isolated_signature_cache(monkeypatch, tmp_path):
    cache = tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json")
    monkeypatch.setattr(tool_signatures, "_cache_instance", cache)
    return cache


@pytest.mark.parametrize("protocol", ["openai", "claude", "responses", "gemini"])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("signature_mode", ["missing", "sentinel"])
async def test_preset_context_survives_cache_misses(
    protocol, warm, signature_mode, isolated_signature_cache
):
    if warm:
        isolated_signature_cache.remember(
            "init", "context", {"query": "init_world"}, SIGNATURE
        )
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "Use the supplied context."}
    ]
    gemini_contents: list[dict[str, object]] = [
        {"role": "user", "parts": [{"text": "Use the supplied context."}]}
    ]
    inputs: list[dict[str, object]] = [
        {"role": "user", "content": "Use the supplied context."}
    ]
    for call_id, name, args, result in HISTORY:
        supplied = SENTINEL if signature_mode == "sentinel" else None
        extra = {"google": {"thoughtSignature": supplied}}
        if protocol == "claude":
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": name,
                                "input": args,
                                "extra_content": extra,
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "content": result,
                            }
                        ],
                    },
                ]
            )
        else:
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "extra_content": extra,
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": name,
                        "tool_call_id": call_id,
                        "content": result,
                    },
                ]
            )
        gemini_contents.extend(
            [
                {
                    "role": "model",
                    "parts": [
                        {
                            "thoughtSignature": supplied,
                            "functionCall": {"id": call_id, "name": name, "args": args},
                        }
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": call_id,
                                "name": name,
                                "response": {"result": result},
                            }
                        }
                    ],
                },
            ]
        )
        inputs.extend(
            [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(args),
                    "extra_content": extra,
                },
                {"type": "function_call_output", "call_id": call_id, "output": result},
            ]
        )
    model = "gemini-4-hydrogen"
    if protocol == "openai":
        request, model = await openai_to_gemini_request(
            OpenAIChatCompletionRequest.model_validate(
                {"model": model, "messages": messages}
            )
        )
    elif protocol == "claude":
        request, model = await claude_to_gemini_request(
            ClaudeMessagesRequest.model_validate({"model": model, "messages": messages})
        )
    elif protocol == "responses":
        request, model = await responses_to_gemini_request(
            OpenAIResponsesRequest.model_validate({"model": model, "input": inputs})
        )
    else:
        request = GeminiGenerateContentRequest.model_validate(
            {"contents": gemini_contents}
        )
    normalized = normalize_gemini_request(request, model)
    outbound, _, _ = ModelTurnHistory().prepare("fixture", normalized.contents)
    wire = json.dumps([content.to_wire() for content in outbound], ensure_ascii=False)
    for call_id, _name, args, result in HISTORY:
        assert call_id in wire and result in wire
        for value in args.values():
            assert value in wire
    assert normalized.contents[-1].role == "user"
    assert normalized.contents[-1].parts[0].text != " "
    calls = [
        part for content in outbound for part in content.parts if part.function_call
    ]
    assert len(calls) == 3
    assert (
        len(
            [
                part
                for content in outbound
                for part in content.parts
                if part.function_response
            ]
        )
        == 3
    )
    for index, part in enumerate(calls):
        expected = (
            SIGNATURE
            if warm and signature_mode == "missing" and index == 0
            else SENTINEL
        )
        assert part.thought_signature == expected
        assert part.to_wire()[14] == expected


@pytest.mark.parametrize("with_id", [False, True])
def test_gemini_text_fallback_preserves_reordered_results_and_images(with_id):
    call_id = "id-less-probe" if with_id else None
    request = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "read_context",
                                "id": call_id,
                                "response": {"context": "must-keep-context"},
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": "aW1hZ2U=",
                                        }
                                    }
                                ],
                            }
                        }
                    ],
                },
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "read_context",
                                "id": call_id,
                                "args": {"query": "must-keep-query"},
                            }
                        }
                    ],
                },
                {"role": "user", "parts": [{"text": "Continue."}]},
            ]
        }
    )
    normalized = normalize_gemini_request(request, "gemini-4-hydrogen")
    parts = [part for content in normalized.contents for part in content.parts]
    assert any("must-keep-context" in (part.text or "") for part in parts)
    assert any(
        part.function_call and part.function_call[1] == {"query": "must-keep-query"}
        for part in parts
    )
    assert any(part.inline_data == ("image/png", "aW1hZ2U=") for part in parts)
    assert sum(part.function_call is not None for part in parts) == 1
    assert not any(part.function_response for part in parts)


@pytest.mark.parametrize("field", ["thought_signature", "thoughtSignature"])
async def test_openai_accepts_valid_google_signature_aliases(field):
    request, model = await openai_to_gemini_request(
        OpenAIChatCompletionRequest.model_validate(
            {
                "model": "gemini-4-hydrogen",
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "signed-call",
                                "type": "function",
                                "function": {"name": "read_context", "arguments": "{}"},
                                "extra_content": {"google": {field: SIGNATURE}},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "signed-call",
                        "content": "signed-result",
                    },
                ],
            }
        )
    )
    normalized = normalize_gemini_request(request, model)
    assert normalized.contents[0].parts[0].function_call is not None
    assert normalized.contents[0].parts[0].thought_signature == SIGNATURE
    assert normalized.contents[1].parts[0].function_response is not None


def test_signature_cache_requires_matching_id_name_and_arguments(
    isolated_signature_cache,
):
    cache = isolated_signature_cache
    cache.remember(
        "call-a", "context", {"query": "sync", "nested": {"a": 1, "b": 2}}, SIGNATURE
    )
    assert (
        cache.lookup("call-a", "context", {"nested": {"b": 2, "a": 1}, "query": "sync"})
        == SIGNATURE
    )
    assert (
        cache.lookup("call-b", "context", {"query": "sync", "nested": {"a": 1, "b": 2}})
        is None
    )
    assert (
        cache.lookup("call-a", "other", {"query": "sync", "nested": {"a": 1, "b": 2}})
        is None
    )
    assert cache.lookup("call-a", "context", {"query": "init_world"}) is None


def test_signature_cache_persists_exact_matches_without_arguments(tmp_path):
    path = tmp_path / "cache.json"
    cache = tool_signatures.ThoughtSignatureCache(path)
    cache.remember(
        "persisted", "f", {"private-prompt": "do-not-store-arguments"}, SIGNATURE
    )
    reloaded = tool_signatures.ThoughtSignatureCache(path)
    assert (
        reloaded.lookup("persisted", "f", {"private-prompt": "do-not-store-arguments"})
        == SIGNATURE
    )
    assert "do-not-store-arguments" not in path.read_text()


def test_signature_cache_ignores_legacy_name_only_entries(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({"by_call_id": {"c": SIGNATURE}, "by_name": {"context": SIGNATURE}})
    )
    cache = tool_signatures.ThoughtSignatureCache(path)
    assert cache.lookup("c", "context", {}) is None


def test_skip_sentinel_is_forwarded_but_not_cached(isolated_signature_cache):
    assert tool_signatures.valid_thought_signature_or_none(SENTINEL) == SENTINEL
    cache = isolated_signature_cache
    cache.remember("dummy", "f", {}, SENTINEL)
    assert cache.lookup("dummy", "f", {}) is None


async def test_response_signature_cache_records_arguments(isolated_signature_cache):
    from aistudio_api.application.compat_service import _iter_gemini_sse_frames

    async def source():
        yield {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "response-call",
                                    "name": "f",
                                    "args": {"value": 7},
                                },
                                "thoughtSignature": SIGNATURE,
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }

    assert [frame async for frame in _iter_gemini_sse_frames(source())]
    assert (
        isolated_signature_cache.lookup("response-call", "f", {"value": 7}) == SIGNATURE
    )
    assert isolated_signature_cache.lookup("response-call", "f", {"value": 8}) is None


def test_signature_cache_bounds_and_reloads_lru(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_signatures.ThoughtSignatureCache, "_MAX_ENTRIES", 2)
    path = tmp_path / "lru.json"
    cache = tool_signatures.ThoughtSignatureCache(path)
    cache.remember("a", "f", {}, SIGNATURE)
    cache.remember("b", "f", {}, SIGNATURE)
    assert cache.lookup("a", "f", {}) == SIGNATURE
    cache.remember("c", "f", {}, SIGNATURE)
    # 落盘走 2s 去抖（热路径性能：不再每 remember 全量重写），显式
    # flush 后再重载验证 LRU 淘汰边界（本测试关注点）。
    cache.flush()
    reloaded = tool_signatures.ThoughtSignatureCache(path)
    assert reloaded.lookup("a", "f", {}) == SIGNATURE
    assert reloaded.lookup("b", "f", {}) is None
    assert reloaded.lookup("c", "f", {}) == SIGNATURE


@pytest.mark.parametrize(
    "call_id,result_id,result_name",
    [("a", "a", ""), ("a", None, "f"), (None, "a", "f"), (None, None, "")],
)
def test_result_linking_fills_metadata_from_the_pending_call(
    call_id, result_id, result_name
):
    request = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"id": call_id, "name": "f", "args": {}}}
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": result_id,
                                "name": result_name,
                                "response": {"keep": "all data"},
                            }
                        }
                    ],
                },
            ]
        }
    )
    normalized = normalize_gemini_request(request, "gemini-4-hydrogen")
    result = normalized.contents[-1].parts[0].function_response
    assert result is not None
    assert result[:2] == ("f", {"keep": "all data"})
    assert (result[2] if len(result) > 2 else None) == (result_id or call_id)


@pytest.mark.parametrize("new_turn", [False, True])
def test_result_linking_does_not_reuse_conflicting_ids_or_previous_turns(new_turn):
    contents = [
        {
            "role": "model",
            "parts": [{"functionCall": {"id": "a", "name": "f", "args": {}}}],
        }
    ]
    if new_turn:
        contents.append({"role": "user", "parts": [{"text": "a new user turn"}]})
    contents.append(
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "a" if new_turn else "b",
                        "name": "f",
                        "response": {"keep": "all data"},
                    }
                }
            ],
        }
    )
    normalized = normalize_gemini_request(
        GeminiGenerateContentRequest.model_validate({"contents": contents}),
        "gemini-4-hydrogen",
    )
    parts = [part for content in normalized.contents for part in content.parts]
    assert not any(part.function_response for part in parts)
    assert any("all data" in (part.text or "") for part in parts)


def test_parallel_results_are_linked_by_id_even_in_reverse_order():
    request = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"id": call_id, "name": name, "args": {}}}
                        for call_id, name in [("a", "f"), ("b", "g")]
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": call_id,
                                "response": {"result": call_id},
                            }
                        }
                        for call_id in ["b", "a"]
                    ],
                },
            ]
        }
    )
    normalized = normalize_gemini_request(request, "gemini-4-hydrogen")
    results = [
        part.function_response
        for part in normalized.contents[-1].parts
        if part.function_response
    ]
    assert [result[0] for result in results] == ["g", "f"]
    assert [result[-1] for result in results] == ["b", "a"]


def test_valid_thought_signature_accepts_urlsafe_and_unpadded_variants():
    """URL-safe / 缺 padding 的 Base64 签名必须被接受。

    wire 编码侧（_normalize_standard_base64）会把这两种形态归一化后发送，
    校验层若拒绝会出现「能发却被判无效」的自相矛盾：客户端原样回传的
    真实签名被静默丢弃，只能退化为 SKIP 字面量。
    """
    raw = b"signature-fixture-with-enough-length-for-normalization"
    urlsafe_padded = base64.urlsafe_b64encode(raw).decode()
    urlsafe_unpadded = urlsafe_padded.rstrip("=")
    standard_unpadded = base64.b64encode(raw).decode().rstrip("=")

    assert tool_signatures.valid_thought_signature(urlsafe_padded)
    assert tool_signatures.valid_thought_signature(urlsafe_unpadded)
    assert tool_signatures.valid_thought_signature(standard_unpadded)
    # 长度 %4==1 或含非法字符的伪值仍然拒绝
    assert not tool_signatures.valid_thought_signature("A" * 17)
    assert not tool_signatures.valid_thought_signature("not!base64!!value")
    # SKIP 字面量维持特例放行
    assert tool_signatures.valid_thought_signature(SENTINEL)


@pytest.mark.parametrize("warm", [False, True])
def test_split_thinking_replays_exact_parts_only_with_scoped_history(
    warm, tmp_path, monkeypatch
):
    a, b = "First half of reasoning.", "Second half of reasoning."
    fingerprint = hashlib.sha256((a + b).encode()).hexdigest()
    path = tmp_path / "pre-upgrade-cache.json"
    legacy_cache = tool_signatures.ThoughtSignatureCache(path)
    legacy_cache.remember("thinking", fingerprint, None, SIGNATURE)
    legacy_cache.flush()
    monkeypatch.setattr(
        tool_signatures, "_cache_instance", tool_signatures.ThoughtSignatureCache(path)
    )
    req = GeminiGenerateContentRequest(
        contents=[
            GeminiContent(role="user", parts=[GeminiPart(text="What is the answer?")]),
            GeminiContent(role="model", parts=[GeminiPart(text=a, thought=True)]),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(text=b, thought=True),
                    GeminiPart(text="answer"),
                ],
            ),
            GeminiContent(role="user", parts=[GeminiPart(text="Thanks")]),
        ]
    )
    normalized = normalize_gemini_request(req, "gemini-3.8-flash")
    history = ModelTurnHistory()
    if warm:
        _, _, context = history.prepare("fixture", normalized.contents[:1])
        assert context is not None
        capture = context.capture()
        capture.add(
            [
                AistudioPart(text=a + b, thought=True).to_wire(),
                AistudioPart(text="answer", thought_signature=SIGNATURE).to_wire(),
            ]
        )
        capture.finish(1)
    outbound, _, _ = history.prepare("fixture", normalized.contents)
    assert len(outbound) == 3
    assert [(part.text, part.thought_signature) for part in outbound[1].parts] == (
        [(a + b, None), ("answer", SIGNATURE)]
        if warm
        else [(a, None), (b, None), ("answer", None)]
    )
