from typing import Any, cast

import pytest

from aistudio_api.infrastructure.gateway.wire_parser import parse_response_chunk


def test_parse_response_chunk_keeps_raw_function_call_and_response():
    chunk = [
        [
            [
                [
                    [
                        [None, None, None, ["getWeather", '{"city":"Shanghai"}']],
                        [
                            None,
                            None,
                            None,
                            None,
                            ["getWeather", {"city": "Shanghai", "temperature": "24C"}],
                        ],
                    ]
                ],
                1,
            ]
        ],
        None,
        [5, 1, 6],
        None,
        None,
        None,
        None,
        "resp_123",
    ]

    candidate = parse_response_chunk(chunk)

    assert candidate.function_calls == [
        {
            "type": "functionCall",
            "raw": ["getWeather", '{"city":"Shanghai"}'],
            "name": "getWeather",
            "args": {"city": "Shanghai"},
        }
    ]
    assert candidate.function_responses == [
        {
            "type": "functionResponse",
            "raw": ["getWeather", {"city": "Shanghai", "temperature": "24C"}],
            "name": "getWeather",
            "args": {"city": "Shanghai", "temperature": "24C"},
        }
    ]


def test_parse_response_chunk_extracts_real_aistudio_function_call_shape():
    chunk = [
        [
            [
                [
                    [
                        [
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            [
                                "getWeather",
                                [[["city", [None, None, "Shanghai"]]]],
                                "e6ni61kr",
                            ],
                            None,
                            None,
                            None,
                            "EiYKJGUyNDgzMGE3LTVjZDYtNDJmZS05OThiLWVlNTM5ZTcyYjljMw==",
                        ]
                    ],
                    "model",
                ]
            ]
        ],
        None,
        [52, 15, 147, None, [[1, 52]], None, None, None, None, 80],
        None,
        None,
        None,
        None,
        "resp_real",
    ]

    candidate = parse_response_chunk(chunk)

    assert candidate.function_calls == [
        {
            "type": "functionCall",
            "raw": ["getWeather", [[["city", [None, None, "Shanghai"]]]], "e6ni61kr"],
            "name": "getWeather",
            "args": {"city": "Shanghai"},
            "call_id": "e6ni61kr",
            "thought_signature": "EiYKJGUyNDgzMGE3LTVjZDYtNDJmZS05OThiLWVlNTM5ZTcyYjljMw==",
        }
    ]


def test_to_gemini_parts_preserves_thought_signature_and_id():
    from aistudio_api.api.responses import to_gemini_parts

    fcs = [
        {
            "name": "getWeather",
            "args": {"city": "Tokyo"},
            "call_id": "call_abc123",
            "thought_signature": "sig_xyz789",
        }
    ]
    frs = [
        {
            "name": "getWeather",
            "args": {"temperature": "20C"},
            "call_id": "call_abc123",
        }
    ]

    parts = to_gemini_parts("", function_calls=fcs, function_responses=frs)
    assert len(parts) == 2

    # FunctionCall part
    fc_part = parts[0]
    assert fc_part.functionCall is not None
    assert fc_part.functionCall.name == "getWeather"
    assert fc_part.functionCall.args == {"city": "Tokyo"}
    assert fc_part.functionCall.id == "call_abc123"
    assert fc_part.thoughtSignature == "sig_xyz789"

    # FunctionResponse part
    fr_part = parts[1]
    assert fr_part.functionResponse is not None
    assert fr_part.functionResponse.name == "getWeather"
    assert fr_part.functionResponse.response == {"temperature": "20C"}
    assert fr_part.functionResponse.id == "call_abc123"


def test_encode_schema_to_wire_case_insensitive_and_aliases():
    from aistudio_api.application.chat_service import encode_schema_to_wire

    schema = {
        "type": "OBJECT",
        "properties": {
            "location": {"type": "STRING"},
            "count": {"type": "INTEGER"},
            "ratio": {"type": "Type.FLOAT"},
            "active": {"type": "BOOLEAN"},
            "tags": {"type": "ARRAY", "items": {"type": "string"}},
        },
        "required": ["location"],
    }

    wire = encode_schema_to_wire(schema)
    assert wire[0] == 6  # OBJECT
    props = dict(cast(list[tuple[str, Any]], wire[6]))
    assert props["location"] == [1]  # STRING
    assert props["count"] == [3]  # INTEGER
    assert props["ratio"] == [2]  # FLOAT
    assert props["active"] == [4]  # BOOLEAN
    assert props["tags"][0] == 5  # ARRAY
    assert props["tags"][5] == [1]  # ARRAY items STRING
    assert wire[7] == ["location"]  # required


def test_wire_args_nested_struct_encoding():
    from aistudio_api.infrastructure.gateway.wire_types import _encode_wire_args

    nested_data = {
        "name": "get_current_weather",
        "content": {"temperature": "15C", "condition": "Sunny", "metrics": {"uv": 3}},
    }

    encoded = _encode_wire_args(nested_data)
    # Must be [ [ [key, val], ... ] ]
    assert isinstance(encoded, list) and len(encoded) == 1
    fields = dict(cast(list[tuple[str, Any]], encoded[0]))
    assert fields["name"] == [None, None, "get_current_weather"]
    # content should be [None, None, None, None, [ [ [key, val], ... ] ]]
    content_val = fields["content"]
    assert content_val[0] is None
    assert content_val[4] is not None
    content_fields = dict(cast(list[tuple[str, Any]], content_val[4][0]))
    assert content_fields["temperature"] == [None, None, "15C"]
    assert content_fields["condition"] == [None, None, "Sunny"]
    metrics_val = content_fields["metrics"]
    assert metrics_val[0] is None
    assert metrics_val[4] is not None
    metrics_fields = dict(cast(list[tuple[str, Any]], metrics_val[4][0]))
    assert metrics_fields["uv"] == [None, 3]


def test_chat_service_does_not_convert_signed_text_into_thought():
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiGenerateContentRequest,
        GeminiPart,
    )
    from aistudio_api.application.chat_service import normalize_gemini_request

    req = GeminiGenerateContentRequest(
        contents=[
            GeminiContent(role="user", parts=[GeminiPart(text="Hi.")]),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        text="Normal answer", thoughtSignature="dmFsaWRfc2lnXzEyMw=="
                    ),
                ],
            ),
            # 以 user 轮收尾：中间 model 轮不受续写改写触碰
            GeminiContent(role="user", parts=[GeminiPart(text="Next.")]),
        ]
    )
    normalized = normalize_gemini_request(req, "gemini-3.7-flash")
    part = normalized.contents[1].parts[0]
    assert part.text == "Normal answer"
    assert part.thought is False
    assert part.thought_signature == "dmFsaWRfc2lnXzEyMw=="

    # Wire serialization must not have thought flag at index 12
    wire = part.to_wire()
    assert wire[1] == "Normal answer"
    assert len(wire) > 14
    assert wire[14] == "dmFsaWRfc2lnXzEyMw=="
    assert wire[12] is None


# ---------- 剥离残留清理（2026-11-14 双 400/403 修复） ----------


def _norm(contents, model="gemini-3.8-flash", **kwargs):
    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.application.chat_service import normalize_gemini_request

    return normalize_gemini_request(
        GeminiGenerateContentRequest(contents=contents, **kwargs), model
    )


def test_normalize_keeps_unsigned_tool_history_instead_of_creating_hollow_turns():
    """无签名工具消息含上下文，不能当成全空轮清理。"""
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiFunctionCall,
        GeminiFunctionResponse,
        GeminiPart,
    )

    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name="context", args={}, id="call_init_context_00"
                        )
                    )
                ],
            ),
            GeminiContent(
                role="user",
                parts=[
                    GeminiPart(
                        functionResponse=GeminiFunctionResponse(
                            name="context", response={}, id="call_init_context_00"
                        )
                    )
                ],
            ),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name="soliumbra_reply",
                            args={},
                            id="call_hist_01",
                        )
                    )
                ],
            ),
            GeminiContent(
                role="user",
                parts=[
                    GeminiPart(
                        functionResponse=GeminiFunctionResponse(
                            name="soliumbra_reply", response={}, id="call_hist_01"
                        )
                    ),
                    GeminiPart(text="Continue."),
                ],
            ),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name="context", args={}, id="call_sync_final"
                        )
                    )
                ],
            ),
            GeminiContent(
                role="user",
                parts=[
                    GeminiPart(
                        functionResponse=GeminiFunctionResponse(
                            name="context", response={}, id="call_sync_final"
                        )
                    )
                ],
            ),
        ]
    )
    assert len(normalized.contents) == 7
    assert normalized.contents[0].parts[0].text == "Hello."
    assert normalized.contents[4].parts[-1].text == "Continue."
    assert normalized.contents[-1].role == "user"
    assert normalized.contents[-1].parts[0].function_response is not None
    # wire 形态：无 [[], role] 空轮（WAA 403 触发形态），以 user 收尾
    for content in normalized.contents:
        assert content.parts, "空轮残留"
        assert all(
            p.text or p.function_call or p.function_response or p.thought_signature
            for p in content.parts
        ), "无载荷 part 残留"


def test_normalize_keeps_signature_only_part():
    """纯签名 part（文本轮尾部信封）是有效载荷，clean 不可丢弃。

    尾部 model 轮（含纯签名轮）由续写改写剥离：无正文前缀时直接删除，
    对话以 user 轮收尾；中间位置的纯签名轮不受改写触碰，原样保留。
    """
    from aistudio_api.api.schemas import GeminiContent, GeminiPart

    # 尾部纯签名轮：续写改写剥离（prefix 全空白 → 直接删，无占位轮）
    normalized = _norm(
        [
            GeminiContent(role="model", parts=[GeminiPart(text="Answer.")]),
            GeminiContent(role="user", parts=[GeminiPart(text="Next.")]),
            GeminiContent(
                role="model",
                parts=[GeminiPart(text="", thoughtSignature="dmFsaWRfc2lnXzEyMw==")],
            ),
        ]
    )
    assert [c.role for c in normalized.contents] == ["model", "user"]
    assert normalized.contents[-1].parts[0].text == "Next."

    # 中间纯签名轮（非尾部）：clean 保留，续写改写不触碰
    normalized_mid = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="Hi.")]),
            GeminiContent(
                role="model",
                parts=[GeminiPart(text="", thoughtSignature="dmFsaWRfc2lnXzEyMw==")],
            ),
            GeminiContent(role="user", parts=[GeminiPart(text="Next.")]),
        ]
    )
    assert [c.role for c in normalized_mid.contents] == ["user", "model", "user"]
    assert normalized_mid.contents[1].parts[0].thought_signature == (
        "dmFsaWRfc2lnXzEyMw=="
    )


def test_normalize_tail_model_text_rewritten_to_continuation():
    """尾部 model 轮改写为系统指令续写（AIStudio2API normalizeTurns 移植）。

    非白名单模型：尾部 model 轮剥离，正文前缀以续写提示词并入系统
    指令（新建 text part，不碰可能带签名的 part），对话以 user 轮
    收尾；函数调用不拼入前缀。单空格 user 占位轮已废弃。
    """
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiFunctionCall,
        GeminiPart,
    )

    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(text="Let me reply in style."),
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name="context", args={}, id="call_tail"
                        )
                    ),
                ],
            ),
        ]
    )
    # model 轮被剥离，无占位轮，对话只剩 user 轮
    assert [c.role for c in normalized.contents] == ["user"]
    assert normalized.contents[0].parts[0].text == "Hello."
    # 系统指令追加续写提示词 + 前缀（无原有 system 时不加前导换行）
    system = normalized.system_instruction
    assert system is not None
    assert system.parts[-1].text == (
        "Continue the assistant response after the following prefix. "
        "Return only the continuation, without repeating the prefix."
        "\nLet me reply in style."
    )


def test_continuation_rewrite_appends_to_existing_system():
    """已有系统指令时，续写指令前置 ``"\n"`` 分隔后新建 part 追加。"""
    from aistudio_api.api.schemas import GeminiContent, GeminiPart

    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
            GeminiContent(role="model", parts=[GeminiPart(text="I will continue thi")]),
        ],
        systemInstruction=GeminiContent(
            role="user", parts=[GeminiPart(text="You are a poet.")]
        ),
    )
    assert [c.role for c in normalized.contents] == ["user"]
    system = normalized.system_instruction
    assert system is not None
    assert [p.text for p in system.parts] == [
        "You are a poet.",
        (
            "\nContinue the assistant response after the following prefix. "
            "Return only the continuation, without repeating the prefix."
            "\nI will continue thi"
        ),
    ]


def test_native_prefill_model_keeps_model_ending():
    """原生 prefill 模型（白名单 + gemma-）model 结尾直接透传，不注入占位轮。

    对照 AIStudio2API turns.go nativePrefillModels：gemini-3.1/3.0 系
    与 gemma- 模型上游原生接受 model 轮结尾，追加占位轮反而破坏客户端
    assistant prefill 的逐字续写语义。
    """
    from aistudio_api.api.schemas import GeminiContent, GeminiPart

    contents = [
        GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
        GeminiContent(role="model", parts=[GeminiPart(text="I will continue thi")]),
    ]
    # 白名单模型：model 结尾原样透传
    normalized = _norm(contents, model="gemini-3.1-pro-preview")
    assert [c.role for c in normalized.contents] == ["user", "model"]
    assert normalized.contents[-1].parts[0].text == "I will continue thi"

    # 别名同样命中白名单（nano-banana → gemini-3.1-flash-lite-image）
    normalized_alias = _norm(contents, model="nano-banana")
    assert [c.role for c in normalized_alias.contents] == ["user", "model"]

    # gemma- 前缀模型：同规则透传
    normalized_gemma = _norm(contents, model="gemma-4-31b-it")
    assert [c.role for c in normalized_gemma.contents] == ["user", "model"]

    # models/ 前缀 + 大小写不敏感
    normalized_prefixed = _norm(contents, model="models/Gemini-3.1-Pro-Preview")
    assert [c.role for c in normalized_prefixed.contents] == ["user", "model"]


def test_newest_models_get_continuation_rewrite():
    """最新模型（gemini-4 系 / 3.5+）尾部 model 轮走续写改写（默认拒绝）。

    本项目 2026-11-14 真机五形态判别（gemini-3.5-flash-lite）：model 结尾
    400 "Requests ending with a model turn are not supported"。单空格
    占位轮已废弃，改为 AIStudio2API 同构的续写改写：剥离尾部 model 轮、
    正文前缀并入系统指令。白名单刻意不含这些模型。
    """
    from aistudio_api.api.schemas import GeminiContent, GeminiPart

    contents = [
        GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
        GeminiContent(role="model", parts=[GeminiPart(text="I will continue thi")]),
    ]
    for model in ("gemini-4-hydrogen", "gemini-3.8-flash", "future-model-x"):
        normalized = _norm(contents, model=model)
        # model 轮被剥离，无占位轮；前缀进系统指令
        assert [c.role for c in normalized.contents] == ["user"], model
        system = normalized.system_instruction
        assert system is not None, model
        assert (system.parts[-1].text or "").endswith("I will continue thi"), model


def test_rewrite_with_empty_prompt_puts_prefix_in_user_turn():
    """prompt 为空时前缀作为最后一轮 user 正文（AIStudio2API 可配置分支）。"""
    from aistudio_api.application.chat_service import (
        _rewrite_model_tail_for_continuation,
    )
    from aistudio_api.infrastructure.gateway.wire_types import (
        AistudioContent,
        AistudioPart,
    )

    contents, system = _rewrite_model_tail_for_continuation(
        [
            AistudioContent(role="user", parts=[AistudioPart(text="Hi.")]),
            AistudioContent(role="model", parts=[AistudioPart(text="I will conti")]),
        ],
        None,
        rewrite=True,
        prompt="",
    )
    assert [c.role for c in contents] == ["user", "user"]
    assert contents[-1].parts[0].text == "I will conti"
    assert system is None


def test_assistant_prefix_code_blocks_and_thought_skip():
    """续写前缀拼接：正文直拼，代码/结果转代码块，思考 part 跳过。"""
    from aistudio_api.application.chat_service import _assistant_prefix
    from aistudio_api.infrastructure.gateway.wire_types import (
        AistudioContent,
        AistudioPart,
    )

    prefix = _assistant_prefix(
        [
            AistudioContent(
                role="model",
                parts=[
                    AistudioPart(text="Running code."),
                    AistudioPart(text="hidden reasoning", thought=True),
                    AistudioPart(executable_code=("Python", "print(1)\n")),
                ],
            ),
            AistudioContent(
                role="model",
                parts=[AistudioPart(code_execution_result=("OK", "1\n"))],
            ),
        ]
    )
    assert prefix == "Running code.\n```python\nprint(1)\n```\n```text\n1\n```\n"


def test_all_hollow_without_system_raises():
    """全部轮被清空且无系统指令 → 报错（单空格占位轮已废弃，
    AIStudio2API 同场景直接拒绝空请求）。"""
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiPart,
    )

    with pytest.raises(ValueError, match="请求没有系统提示或对话内容"):
        _norm(
            [
                GeminiContent(role="model", parts=[GeminiPart(text="")]),
                GeminiContent(role="user", parts=[]),
            ],
            model="gemini-3.1-pro-preview",
        )


def test_normalize_all_hollow_with_system_becomes_user_turn():
    """全部轮被清空但系统指令有载荷 → 系统指令整体转为 user 轮
    （AIStudio2API 同构：SystemText(system) 作为 user 正文）。"""
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiPart,
    )

    normalized = _norm(
        [
            GeminiContent(
                role="model",
                parts=[GeminiPart(text="")],
            ),
            GeminiContent(
                role="user",
                parts=[],
            ),
        ],
        systemInstruction=GeminiContent(
            role="user", parts=[GeminiPart(text="You are helpful.")]
        ),
    )
    # 系统指令不再单独发送，转为唯一 user 轮
    assert normalized.system_instruction is None
    assert len(normalized.contents) == 1
    assert normalized.contents[0].role == "user"
    assert normalized.contents[0].parts[0].text == "You are helpful."


def test_normalize_merges_consecutive_same_role_turns():
    """相邻同角色轮合并（前端同构形态），part 顺序保持。"""
    from aistudio_api.api.schemas import GeminiContent, GeminiPart

    normalized = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="A.")]),
            GeminiContent(role="user", parts=[GeminiPart(text="B.")]),
            GeminiContent(role="model", parts=[GeminiPart(text="C.")]),
            GeminiContent(role="user", parts=[GeminiPart(text="D.")]),
        ]
    )
    assert [(c.role, [p.text for p in c.parts]) for c in normalized.contents] == [
        ("user", ["A.", "B."]),
        ("model", ["C."]),
        ("user", ["D."]),
    ]


def test_normalize_digest_unaffected_by_cleanup():
    """清理不改变 proof 摘要输入（空轮无贡献，合并保持平铺顺序）。"""
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiPart,
    )
    from aistudio_api.infrastructure.waa.signer import extract_content_digest

    with_empty_turns = [
        GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
        GeminiContent(
            role="model",
            parts=[GeminiPart(text="")],
        ),
        GeminiContent(
            role="user",
            parts=[
                GeminiPart(text="Continue."),
            ],
        ),
    ]
    normalized = _norm(with_empty_turns)
    # 仅清理确实无载荷的空轮，不删除工具上下文。
    equivalent = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
            GeminiContent(role="user", parts=[GeminiPart(text="Continue.")]),
        ]
    )
    assert extract_content_digest(normalized.contents) == extract_content_digest(
        equivalent.contents
    )
