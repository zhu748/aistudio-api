from typing import Any, cast

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
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        text="Normal answer", thoughtSignature="dmFsaWRfc2lnXzEyMw=="
                    ),
                ],
            )
        ]
    )
    normalized = normalize_gemini_request(req, "gemini-3.7-flash")
    part = normalized.contents[0].parts[0]
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


def test_normalize_drops_hollow_turns_and_merges_same_role():
    """剥离后的全空轮必须彻底消失（WAA 403 / model-turn 400 双修）。

    有毒历史（预设伪造工具调用，缓存未命中被剥离）：
    [user text, model FC(剥离), user FR(丢弃), model FC(剥离),
     user FR(丢弃)+text, model FC(剥离), user FR(丢弃)]
    → 清理后仅剩单个 user 轮（两段 text 合并），以 user 收尾。
    """
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
    # 全部空轮被丢弃，两段 user 文本合并为单轮
    assert len(normalized.contents) == 1
    merged = normalized.contents[0]
    assert merged.role == "user"
    assert [p.text for p in merged.parts] == ["Hello.", "Continue."]
    # wire 形态：无 [[], role] 空轮（WAA 403 触发形态），以 user 收尾
    for content in normalized.contents:
        assert content.parts, "空轮残留"
        assert all(
            p.text or p.function_call or p.function_response or p.thought_signature
            for p in content.parts
        ), "无载荷 part 残留"


def test_normalize_keeps_signature_only_part():
    """纯签名 part（文本轮尾部信封）是有效载荷，不可被清理丢弃。"""
    from aistudio_api.api.schemas import GeminiContent, GeminiPart

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
    # 末尾空文本签名轮保留（有签名载荷，非空轮）；
    # model 结尾追加单空格 user 占位（真机实测 200，见 2026-11-14 判别）
    assert normalized.contents[-2].parts[0].thought_signature == (
        "dmFsaWRfc2lnXzEyMw=="
    )
    assert normalized.contents[-1].role == "user"
    assert normalized.contents[-1].parts[0].text == " "


def test_normalize_tail_model_text_gets_space_user_sentinel():
    """尾部 model 文本轮（prefill/混合轮剥离残留）→ 追加单空格 user 占位。

    真机五形态判别（2026-11-14）：model 结尾 400；空文本 user 占位无效
    （上游不认空文本轮）；单空格 user 占位 200。
    """
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiFunctionCall,
        GeminiPart,
    )

    # 混合轮：文本 + 伪造工具调用（剥离后文本保留，轮次以 model 收尾）
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
    assert [c.role for c in normalized.contents] == ["user", "model", "user"]
    assert normalized.contents[0].parts[0].text == "Hello."
    assert normalized.contents[1].parts[0].text == "Let me reply in style."
    assert all(p.function_call is None for p in normalized.contents[1].parts)
    sentinel = normalized.contents[-1]
    assert sentinel.role == "user"
    assert sentinel.parts[0].text == " "


def test_normalize_all_hollow_falls_back_to_single_user_turn():
    """全部轮被清空时回退单个空文本 user 轮（保持 user 收尾语义）。"""
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiFunctionCall,
        GeminiFunctionResponse,
        GeminiPart,
    )

    normalized = _norm(
        [
            GeminiContent(
                role="model",
                parts=[
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name="context", args={}, id="call_x"
                        )
                    )
                ],
            ),
            GeminiContent(
                role="user",
                parts=[
                    GeminiPart(
                        functionResponse=GeminiFunctionResponse(
                            name="context", response={}, id="call_x"
                        )
                    )
                ],
            ),
        ]
    )
    assert len(normalized.contents) == 1
    assert normalized.contents[0].role == "user"
    assert normalized.contents[0].parts[0].text == ""


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
        GeminiFunctionCall,
        GeminiFunctionResponse,
        GeminiPart,
    )
    from aistudio_api.infrastructure.waa.signer import extract_content_digest

    poisoned = [
        GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
        GeminiContent(
            role="model",
            parts=[
                GeminiPart(
                    functionCall=GeminiFunctionCall(
                        name="context", args={}, id="call_c1"
                    )
                )
            ],
        ),
        GeminiContent(
            role="user",
            parts=[
                GeminiPart(
                    functionResponse=GeminiFunctionResponse(
                        name="context", response={}, id="call_c1"
                    )
                ),
                GeminiPart(text="Continue."),
            ],
        ),
    ]
    normalized = _norm(poisoned)
    # 与「无工具轮的等价历史」摘要一致：剥离 + 清理后内容语义不变
    equivalent = _norm(
        [
            GeminiContent(role="user", parts=[GeminiPart(text="Hello.")]),
            GeminiContent(role="user", parts=[GeminiPart(text="Continue.")]),
        ]
    )
    assert extract_content_digest(normalized.contents) == extract_content_digest(
        equivalent.contents
    )
