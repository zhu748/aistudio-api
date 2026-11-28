"""超长函数名截短/恢复与工具参数 schema 降级链单元测试。

对照 AIStudio2API（Go）2026-11 快照的行为移植验证：

- ``wireFunctionName``：超 63 字节截短 + sha256 后缀，历史回传同步截短；
- ``restoreFunctionNames``：响应侧凭请求内声明映射换回原名（client 层）；
- ``encodeFunctionDeclaration`` 降级链：编码失败降级 shape、wire 无法
  表达的约束（additionalProperties / prefixItems / exclusiveMinimum /
  propertyNames / $ref / 空串 enum / strict）以完整 JSON Schema 附加到
  description；
- ``defineRequiredProperties``：required-but-undeclared 属性补全；
- ``encodeResponseSchema``：结构化输出同款兜底。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from aistudio_api.application.anti_truncation import prepare_anti_truncation
from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    encode_function_declaration_to_wire,
    encode_response_schema_to_wire,
)
from aistudio_api.application.schema_wire_codec import _tool_schema_shape
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioPart,
    wire_function_name,
)

LONG_NAME = "do_something_very_specific_" + "x" * 40  # 71 字节，超 63 上限
WIRE_NAME = wire_function_name(LONG_NAME)

_NOTE = "Arguments must follow this JSON Schema: "
_RESPONSE_NOTE = "The response must follow this JSON Schema: "


def _norm_with_tools(declarations: list[dict], **kwargs):
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiGenerateContentRequest,
        GeminiPart,
        GeminiTool,
    )
    from aistudio_api.application.chat_service import normalize_gemini_request

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="Hi.")])],
        tools=[GeminiTool(functionDeclarations=declarations)],
        **kwargs,
    )
    return normalize_gemini_request(req, "gemini-3.8-flash")


def _decl_names(tools: list[list[object]]) -> list[object]:
    """提取全部 wire 声明名（tools 容器类型收窄辅助）。"""
    names: list[object] = []
    for container in tools:
        for decl in cast(list[list[object]], container[1]):
            names.append(decl[0])
    return names


def _encode_decl(declaration: dict) -> list[Any]:
    """encode_function_declaration_to_wire 的类型放宽视图（wire 元素异构）。"""
    return cast(list[Any], encode_function_declaration_to_wire(declaration))


def _encode_resp_schema(schema: dict) -> list[Any]:
    """encode_response_schema_to_wire 的类型放宽视图。"""
    return cast(list[Any], encode_response_schema_to_wire(schema))


def _shape(schema: dict) -> dict[str, Any]:
    """_tool_schema_shape 的类型收紧视图（产物恒为 dict）。"""
    return cast(dict[str, Any], _tool_schema_shape(schema))


# ---------------------------------------------------------------------------
# wire_function_name 边界
# ---------------------------------------------------------------------------


def test_wire_function_name_within_limit_unchanged():
    assert wire_function_name("getWeather") == "getWeather"
    assert wire_function_name("x" * 63) == "x" * 63


def test_wire_function_name_truncates_with_hash_suffix():
    truncated = wire_function_name("y" * 80)
    assert len(truncated.encode("utf-8")) <= 63
    assert truncated.startswith("y" * (63 - 13))
    assert truncated[-13] == "_"
    assert len(truncated[-12:]) == 12  # sha256 前 6 字节的 hex
    int(truncated[-12:], 16)  # 合法 hex


def test_wire_function_name_is_stable_and_idempotent():
    assert wire_function_name(LONG_NAME) == WIRE_NAME
    # 截短产物恰好 63 字节：再次截短应保持不变（历史回传幂等）
    assert wire_function_name(WIRE_NAME) == WIRE_NAME


def test_wire_function_name_counts_bytes_not_chars():
    # 40 个三字节汉字 = 120 字节，必须截短且产物不超字节预算
    truncated = wire_function_name("查" * 40)
    assert len(truncated.encode("utf-8")) <= 63
    assert truncated.endswith("_" + truncated[-12:])


# ---------------------------------------------------------------------------
# 声明编码与 normalize 映射
# ---------------------------------------------------------------------------


def test_declaration_wire_truncates_long_name():
    wire = _encode_decl(
        {"name": LONG_NAME, "parameters": {"type": "object", "properties": {}}}
    )
    assert wire[0] == WIRE_NAME
    assert wire[0] != LONG_NAME


def test_normalize_builds_name_restore_mapping():
    normalized = _norm_with_tools(
        [
            {
                "name": LONG_NAME,
                "parameters": {"type": "object", "properties": {}},
            },
            {"name": "short_tool", "parameters": {"type": "object"}},
        ]
    )
    assert normalized.function_name_restore == {WIRE_NAME: LONG_NAME}
    # wire 侧声明已用截短名
    assert normalized.tools is not None
    names = _decl_names(normalized.tools)
    assert WIRE_NAME in names
    assert "short_tool" in names


def test_normalize_without_long_names_has_no_mapping():
    normalized = _norm_with_tools([{"name": "short_tool"}])
    assert normalized.function_name_restore is None


def test_allowed_function_names_match_truncated_wire_name():
    normalized = _norm_with_tools(
        [{"name": LONG_NAME, "parameters": {"type": "object"}}],
        toolConfig={"functionCallingConfig": {"allowedFunctionNames": [LONG_NAME]}},
    )
    assert normalized.tools, "allowedFunctionNames 按原名过滤应保留截短声明"
    names = _decl_names(normalized.tools)
    assert names == [WIRE_NAME]


def test_history_function_parts_wire_use_truncated_name():
    fc_part = AistudioPart(function_call=(LONG_NAME, {"city": "上海"}, "call_123"))
    wire = fc_part.to_wire()
    fc_wire = cast(list[object], wire[10])
    assert fc_wire[0] == WIRE_NAME
    assert fc_wire[2] == "call_123"

    fr_part = AistudioPart(function_response=(LONG_NAME, {"temp": "18C"}, "call_123"))
    wire = fr_part.to_wire()
    fr_wire = cast(list[object], wire[11])
    assert fr_wire[0] == WIRE_NAME


# ---------------------------------------------------------------------------
# client 层恢复（AIStudio2API restoreFunctionNames 移植）
# ---------------------------------------------------------------------------


def _make_stub_client() -> AIStudioClient:
    stub = cast(Any, AIStudioClient.__new__(AIStudioClient))
    stub._session = SimpleNamespace()
    stub._model_turns = None
    return cast(AIStudioClient, stub)


async def _stub_prepare(**_kwargs):
    return PreparedRequest(url="https://stub/Prompt", headers={}, body="[]")


async def test_client_stream_restores_truncated_tool_call_names():
    client = _make_stub_client()
    events = [
        (
            "tool_calls",
            [
                {"name": WIRE_NAME, "args": {"a": 1}, "call_id": "c1"},
                {"name": "normal_tool", "args": {}, "call_id": "c2"},
            ],
        ),
        ("done", None),
    ]

    class _StubGateway:
        async def stream_chat(self, *, request, model):
            for event in events:
                yield event

    cast(Any, client)._streaming_gateway = _StubGateway()
    client.prepare_request = _stub_prepare  # type: ignore[method-assign]

    collected = [
        event
        async for event in client.stream_generate_content(
            model="m",
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="q")])],
            function_name_restore={WIRE_NAME: LONG_NAME},
        )
    ]

    calls = cast(list[dict], collected[0][1])
    assert calls[0]["name"] == LONG_NAME
    # 未命中映射的普通名不受影响
    assert calls[1]["name"] == "normal_tool"


async def test_client_generate_restores_truncated_tool_call_names():
    client = _make_stub_client()
    fc_part: list[object] = [None] * 11
    fc_part[10] = [WIRE_NAME, [[["city", [None, None, "上海"]]]], "call_9"]
    chunk = [
        [[[[fc_part], "model"]]],
        None,
        [10, 3, 30, None, [[1, 10]], None, None, None, None, 30],
    ]
    body = json.dumps([[chunk]]).encode("utf-8")

    async def _send_request(**_kwargs):
        return 200, body

    client._session.send_request = _send_request
    client.prepare_request = _stub_prepare  # type: ignore[method-assign]

    output = await client.generate_content(
        model="m",
        contents=[AistudioContent(role="user", parts=[AistudioPart(text="q")])],
        function_name_restore={WIRE_NAME: LONG_NAME},
    )

    assert output.function_calls[0]["name"] == LONG_NAME
    assert output.function_calls[0]["call_id"] == "call_9"


def test_apply_function_name_restore_ignores_unmapped():
    from aistudio_api.infrastructure.gateway.client import (
        _apply_function_name_restore,
    )

    calls = [{"name": "other"}, {"name": WIRE_NAME}]
    _apply_function_name_restore(calls, {WIRE_NAME: LONG_NAME})
    assert calls[0]["name"] == "other"
    assert calls[1]["name"] == LONG_NAME
    # 空映射零改动
    _apply_function_name_restore(calls, None)
    assert calls[1]["name"] == LONG_NAME


# ---------------------------------------------------------------------------
# 声明参数 schema 降级链
# ---------------------------------------------------------------------------


def test_declaration_recursive_ref_falls_back_to_shape():
    """递归 $ref 此前直接 ValueError 拒绝整轮请求；现降级为 shape 编码。"""
    parameters = {
        "type": "object",
        "properties": {"node": {"$ref": "#/definitions/node"}},
        "definitions": {
            "node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/definitions/node"}},
            }
        },
    }
    wire = _encode_decl({"name": "t", "parameters": parameters})
    assert isinstance(wire[2], list) and wire[2]  # shape 编码产物
    assert _NOTE in wire[1]
    assert '"$ref"' in wire[1]  # 完整原文随 description 下发


def test_declaration_embeds_schema_for_lossy_keys():
    parameters = {
        "type": "object",
        "properties": {"meta": {"type": "object", "additionalProperties": True}},
    }
    wire = _encode_decl(
        {"name": "t", "description": "原始描述", "parameters": parameters}
    )
    assert wire[1].startswith("原始描述")
    assert _NOTE in wire[1]
    assert "additionalProperties" in wire[1]


def test_declaration_closed_object_does_not_embed_schema():
    """``additionalProperties: false`` 是 Gemini 对象默认闭合语义：声明侧不再
    为它把整份 schema 原文重复附加进 description（Claude Code 15 个工具里
    12 个仅因该键触发，每请求多 ~17KB）。"""
    parameters = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "路径"},
            "meta": {"type": "object", "additionalProperties": False},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    wire = _encode_decl(
        {"name": "Read", "description": "读文件", "parameters": parameters}
    )
    assert wire[1] == "读文件"
    assert _NOTE not in wire[1]
    # 参数编码本身完整保留（属性仍在 wire 槽位 6）
    assert wire[2][6][0][0] == "path"


def test_declaration_truthy_additional_properties_still_embeds():
    """``additionalProperties`` 为 schema/true 时 wire 仍无法表达，继续附加原文。"""
    parameters = {
        "type": "object",
        "properties": {
            "meta": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            }
        },
    }
    wire = _encode_decl({"name": "t", "parameters": parameters})
    assert _NOTE in wire[1]
    assert "additionalProperties" in wire[1]


def test_declaration_embed_strips_schema_dialect_keyword():
    """仍需附加原文时（propertyNames 等真 lossy 键），附加的 JSON 剔除 ``$schema``。"""
    parameters = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "env": {
                "type": "object",
                "propertyNames": {"pattern": "^[A-Z_]+$"},
            }
        },
    }
    wire = _encode_decl({"name": "Bash", "parameters": parameters})
    assert _NOTE in wire[1]
    assert "propertyNames" in wire[1]
    assert "$schema" not in wire[1]


def test_declaration_strict_embeds_full_schema():
    parameters = {"type": "object", "properties": {"q": {"type": "string"}}}
    plain = _encode_decl({"name": "t", "parameters": parameters})
    strict = _encode_decl({"name": "t", "parameters": parameters, "strict": True})
    assert _NOTE not in (plain[1] or "")
    assert _NOTE in strict[1]


def test_declaration_required_but_undeclared_gets_definition():
    parameters = {"type": "object", "required": ["x"]}
    wire = _encode_decl({"name": "t", "parameters": parameters})
    schema_wire = wire[2]
    properties = schema_wire[6]
    assert any(entry[0] == "x" for entry in properties)
    # 无 lossy 键：不附加原文
    assert _NOTE not in (wire[1] or "")


def test_declaration_normal_schema_has_no_embed():
    wire = _encode_decl(
        {
            "name": "getWeather",
            "description": "查询天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    )
    assert wire[0] == "getWeather"
    assert wire[1] == "查询天气"
    assert _NOTE not in wire[1]


def test_enum_empty_string_filtered_and_schema_embedded():
    parameters = {"type": "string", "enum": ["", "a"]}
    wire = _encode_decl({"name": "t", "parameters": parameters})
    schema_wire = wire[2]
    assert schema_wire[4] == ["a"]  # ENUM 槽位剔除空串
    assert _NOTE in wire[1]

    only_empty = _encode_decl(
        {"name": "t", "parameters": {"type": "string", "enum": [""]}}
    )
    schema_wire = only_empty[2]
    # 全空 enum 整体省略（上游拒绝空串值）
    assert len(schema_wire) <= 4 or schema_wire[4] is None
    assert _NOTE in only_empty[1]


# ---------------------------------------------------------------------------
# 结构化输出 responseSchema 兜底
# ---------------------------------------------------------------------------


def test_response_schema_recursive_ref_falls_back():
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/definitions/a"}},
        "definitions": {
            "a": {"type": "object", "properties": {"b": {"$ref": "#/definitions/a"}}}
        },
    }
    wire = _encode_resp_schema(schema)
    assert isinstance(wire, list) and wire
    assert _RESPONSE_NOTE in wire[2]  # DESCRIPTION 槽位
    assert '"$ref"' in wire[2]


def test_response_schema_lossy_detection():
    open_object = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "additionalProperties": True,
    }
    assert _RESPONSE_NOTE in _encode_resp_schema(open_object)[2]

    # additionalProperties: false 是 Gemini 对象默认闭合语义，不触发
    closed = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "additionalProperties": False,
    }
    closed_wire = _encode_resp_schema(closed)
    assert len(closed_wire) <= 2 or closed_wire[2] is None

    prefix_items = {"type": "array", "prefixItems": [{"type": "string"}]}
    assert _RESPONSE_NOTE in _encode_resp_schema(prefix_items)[2]


def test_response_schema_normal_has_no_note():
    wire = _encode_resp_schema(
        {"type": "object", "properties": {"q": {"type": "string"}}}
    )
    assert len(wire) <= 2 or wire[2] is None


# ---------------------------------------------------------------------------
# toolSchemaShape 语义
# ---------------------------------------------------------------------------


def test_tool_schema_shape_const_folding():
    number = _shape({"const": 5})
    assert number["type"] == "integer"
    assert number["minimum"] == 5 and number["maximum"] == 5
    assert "The value must be: 5" in number["description"]

    flag = _shape({"const": True})
    assert flag["type"] == "boolean"

    text = _shape({"const": "yes"})
    assert text["type"] == "string"
    assert text["enum"] == ["yes"]

    null_const = _shape({"const": None})
    assert null_const["type"] == "string"
    assert null_const["nullable"] is True


def test_tool_schema_shape_null_type_and_enum_rules():
    null_type = _shape({"type": "null"})
    assert null_type["type"] == "string"
    assert null_type["nullable"] is True

    mixed = _shape({"enum": ["a", 1]})
    assert "enum" not in mixed  # 非全字符串 enum 丢弃

    strings = _shape({"enum": ["a", "b"]})
    assert strings["enum"] == ["a", "b"]


def test_tool_schema_shape_snake_aliases_and_recursion():
    shape = _shape(
        {
            "type": "object",
            "min_properties": 1,
            "properties": {"child": {"type": "string", "min_length": 2}},
            "items": {"type": "number"},
        }
    )
    assert shape["minProperties"] == 1
    assert shape["properties"]["child"]["minLength"] == 2
    assert shape["items"] == {"type": "number"}


# ---------------------------------------------------------------------------
# 抗截断合成名注册
# ---------------------------------------------------------------------------


def test_anti_truncation_registers_truncated_synthetic_name(monkeypatch):
    long_tool = "router_" + "z" * 60
    from aistudio_api.application import anti_truncation as at_module

    monkeypatch.setattr(
        at_module,
        "get_anti_truncation_settings",
        lambda: {"enabled": True, "tool_name": long_tool},
    )
    normalized = NormalizedGeminiRequest(
        model="models/gemini-2.5-flash",
        contents=[
            AistudioContent(role="user", parts=[AistudioPart(text="写一首长诗")])
        ],
        system_instruction=None,
        tools=None,
        safety_settings=None,
    )
    ctx = prepare_anti_truncation(normalized, override=True)
    assert ctx.active is True
    assert ctx.tool_name == long_tool
    # 截短合成名注册进恢复映射，流式前缀匹配不受截短影响
    assert normalized.function_name_restore == {
        wire_function_name(long_tool): long_tool
    }


# ───────────── 声明编码跨请求记忆化（性能优化：Agent 负载每请求重发相同声明） ─────────────


def test_declaration_encoding_is_memoized_and_mutation_isolated():
    """同一声明跨调用命中记忆化缓存；命中结果为全新对象，突变不污染缓存。"""
    decl = {
        "name": "memo_get_weather",
        "description": "查询城市天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
            "required": ["city"],
        },
    }
    from aistudio_api.application import chat_service as cs

    first = cs.encode_function_declaration_to_wire(decl)
    # 等值但非同一对象（模拟下一请求重新反序列化的声明）
    second = cs.encode_function_declaration_to_wire(json.loads(json.dumps(decl)))
    assert first == second
    assert first is not second
    # 突变命中结果不得影响后续调用
    first.append("poison")
    third = cs.encode_function_declaration_to_wire(json.loads(json.dumps(decl)))
    assert third == second
    assert "poison" not in third


def test_declaration_cache_distinguishes_strict_flag():
    """strict 取自声明本身：同一 schema 的 strict/非 strict 声明各自编码缓存。"""
    base = {
        "name": "memo_strict_check",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }
    from aistudio_api.application import chat_service as cs

    plain = cs.encode_function_declaration_to_wire(dict(base))
    strict_decl = {**base, "strict": True}
    strict = cs.encode_function_declaration_to_wire(json.loads(json.dumps(strict_decl)))
    # strict 附加完整 schema 原文到 description，输出必须不同
    assert plain != strict
    # 各自缓存命中后仍稳定
    assert cs.encode_function_declaration_to_wire(dict(base)) == plain
    assert (
        cs.encode_function_declaration_to_wire(json.loads(json.dumps(strict_decl)))
        == strict
    )
