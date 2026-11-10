import json
from pathlib import Path

from aistudio_api.infrastructure.gateway.wire_codec import (
    AistudioWireCodec,
    build_image_generation_search_tool,
    build_tools_from_names,
    encode_request_body,
)
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioGenerationConfig,
    AistudioImageOutputMode,
    AistudioThinkingConfig,
    MediaResolution,
    ThinkingLevel,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_encode_request_body_updates_generation_config_and_prompt():
    rewritten = encode_request_body(
        model="models/new",
        prompt="new prompt",
        system_instruction="sys",
        max_tokens=256,
        temperature=0.2,
        top_p=0.9,
        top_k=32,
    )
    assert '"models/new"' in rewritten
    assert '"new prompt"' in rewritten
    assert '"sys"' in rewritten
    body = json.loads(rewritten)
    assert body[3][3] == 256
    assert body[3][4] == 0.2
    assert body[3][5] == 0.9
    assert body[3][6] == 32
    assert len(body[3]) == 7


def test_build_image_generation_search_tool_variants():
    assert build_image_generation_search_tool(
        google_search=True, image_search=False
    ) == [None, None, None, [None, [[]]]]
    assert build_image_generation_search_tool(
        google_search=False, image_search=True
    ) == [None, None, None, [None, [None, []]]]
    assert build_image_generation_search_tool(
        google_search=True, image_search=True
    ) == [None, None, None, [None, [[], []]]]
    assert (
        build_image_generation_search_tool(google_search=False, image_search=False)
        is None
    )


def test_build_tools_from_names_supports_image_tool_aliases():
    assert build_tools_from_names(
        ["google_search"],
        model="models/gemini-3.1-flash-image-preview",
        is_image_model=True,
    ) == [[None, None, None, [None, [[]]]]]
    assert build_tools_from_names(
        ["image_search"],
        model="models/gemini-3.1-flash-image-preview",
        is_image_model=True,
    ) == [[None, None, None, [None, [None, []]]]]
    assert build_tools_from_names(
        ["google_search_and_image_search"],
        model="models/gemini-3.1-flash-image-preview",
        is_image_model=True,
    ) == [[None, None, None, [None, [[], []]]]]


def test_build_tools_from_names_merges_image_search_flags_into_single_tool():
    assert build_tools_from_names(
        ["google_search", "image_search"],
        model="models/gemini-3.1-flash-image-preview",
        is_image_model=True,
    ) == [[None, None, None, [None, [[], []]]]]


def test_build_tools_from_names_restricts_gemma_builtin_tools():
    assert build_tools_from_names(
        ["google_search", "code_execution"], model="models/gemma-4-31b-it"
    ) == [
        [None, None, None, [None, [[]]]],
        [[]],
    ]

    try:
        build_tools_from_names(["google_maps"], model="models/gemma-4-31b-it")
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_build_tools_from_names_allows_four_gemini_builtin_tools():
    assert build_tools_from_names(
        ["google_search", "code_execution", "google_maps", "url_context"],
        model="models/gemini-3.5-flash",
    ) == [
        [None, None, None, [None, [[]]]],
        [[]],
        [None, None, None, None, None, None, None, None, None, None, []],
        [None, None, None, None, None, None, None, []],
    ]


def test_build_tools_from_names_restricts_unknown_model_to_safe_subset():
    try:
        build_tools_from_names(
            ["google_maps"], model="models/learnlm-1.5-pro-experimental"
        )
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_wire_codec_decodes_semantic_fields():
    codec = AistudioWireCodec()
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"!snap",[[[null,"sys"]],"user"],[[[]]]]'

    decoded = codec.decode(original)

    assert decoded.model == "models/original"
    assert decoded.snapshot == "!snap"
    assert decoded.contents[0].role == "user"
    assert decoded.contents[0].parts[-1].text == "old"
    assert decoded.system_instruction is not None
    assert decoded.system_instruction.parts[0].text == "sys"
    assert decoded.generation_config.max_tokens == 128


def test_encode_request_body_does_not_inject_default_thinking_when_omitted():
    rewritten = encode_request_body(
        model="models/new-text-model",
        prompt="hello",
    )

    body = json.loads(rewritten)
    assert body[3] == []


def test_encode_request_body_can_override_image_output_resolution():
    rewritten = encode_request_body(
        model="models/gemini-3.1-flash-image-preview",
        prompt="hello",
        generation_config_overrides={"output_resolution": ["16:9", "1K"]},
    )

    body = json.loads(rewritten)
    assert body[3][26] == ["16:9", "1K"]


def test_encode_request_body_applies_model_safety_defaults():
    rewritten = encode_request_body(
        model="models/gemma-4-31b-it",
        prompt="hello",
    )

    body = json.loads(rewritten)
    assert body[2] == [
        [None, None, 7, 5],
        [None, None, 8, 5],
        [None, None, 9, 5],
        [None, None, 10, 5],
    ]


def test_encode_request_body_applies_image_model_defaults():
    rewritten = encode_request_body(
        model="models/gemini-3.1-flash-image-preview",
        prompt="hello",
    )

    body = json.loads(rewritten)
    assert body[2] is None
    assert body[3] == []


def test_encode_request_body_keeps_image_model_tools():
    rewritten = encode_request_body(
        model="models/gemini-3.1-flash-image-preview",
        prompt="hello",
        tools=[[None, None, None, [None, [[], []]]]],
    )

    body = json.loads(rewritten)
    assert body[6] == [[None, None, None, [None, [[], []]]]]


def test_encode_request_body_can_override_safety_settings():
    rewritten = encode_request_body(
        model="models/gemini-3.5-flash",
        prompt="hello",
        safety_settings=[
            [None, None, 7, 1],
            [None, None, 8, 4],
            [None, None, 9, 3],
            [None, None, 10, 2],
        ],
    )

    body = json.loads(rewritten)
    assert body[2] == [
        [None, None, 7, 1],
        [None, None, 8, 4],
        [None, None, 9, 3],
        [None, None, 10, 2],
    ]


def test_encode_request_body_keeps_structured_generation_config_for_gemini_mode():
    rewritten = encode_request_body(
        model="models/gemini-2.5-pro-preview-05-06",
        prompt="hello",
        generation_config_overrides={
            "stop_sequences": ["STOP"],
            "response_mime_type": "application/json",
            "response_schema": [6],
            "presence_penalty": 0.3,
            "frequency_penalty": 0.4,
            "response_logprobs": True,
            "logprobs": 7,
            "image_output_mode": [2, 1],
            "thinking_config": [1, None, None, 3],
            "media_resolution": 2,
        },
    )
    body = json.loads(rewritten)
    assert body[3][1] == ["STOP"]
    assert body[3][7] == "application/json"
    assert body[3][8] == [6]
    assert body[3][9] == 0.3
    assert body[3][10] == 0.4
    assert body[3][11] is True
    assert body[3][12] == 7
    assert body[3][14] == [2, 1]
    assert body[3][16] == [1, None, None, 3]
    assert body[3][17] == 2


def test_decode_image_request_maps_generation_config_fields_from_proto_indexes():
    codec = AistudioWireCodec()
    raw = (FIXTURES / "test_image_input.json").read_text(encoding="utf-8")

    request = codec.decode(raw)

    assert request.model == "models/gemini-3.1-flash-image-preview"
    assert request.generation_config.stop_sequences == ["6"]
    assert request.generation_config.max_tokens == 65536
    assert request.generation_config.temperature == 1
    assert request.generation_config.top_p == 0.95
    assert request.generation_config.top_k == 64
    assert request.generation_config.image_output_mode == [2, 1]
    assert request.generation_config.thinking_config == [1, None, None, 3]
    assert request.request_flag == 1
    assert request.session_token is not None and request.session_token.startswith("v1_")


def test_encode_preserves_newly_mapped_proto_fields():
    codec = AistudioWireCodec()
    raw = (FIXTURES / "test_image_input.json").read_text(encoding="utf-8")

    request = codec.decode(raw)
    encoded = codec.encode(request)
    reparsed = codec.decode(encoded)

    assert reparsed.generation_config.stop_sequences == ["6"]
    assert reparsed.generation_config.image_output_mode == [2, 1]
    assert reparsed.generation_config.thinking_config == [1, None, None, 3]
    assert reparsed.request_flag == 1
    assert reparsed.session_token == request.session_token


def test_thinking_config_encodes_high_level_wire_shape():
    assert AistudioThinkingConfig(ThinkingLevel.HIGH).to_wire() == [1, None, None, 3]


def test_generation_config_stays_empty_by_default_and_trims_none():
    config = AistudioGenerationConfig([])

    assert config.thinking_config is None
    assert config.media_resolution is None
    assert config.values == []

    config.thinking_config = [1, None, None, 3]
    assert config.thinking_config == [1, None, None, 3]
    config.thinking_config = None
    assert config.thinking_config is None
    assert config.values == []


def test_generation_config_accepts_readable_image_output_mode_wrapper():
    config = AistudioGenerationConfig([])

    config.image_output_mode = AistudioImageOutputMode.text_and_image()

    assert config.image_output_mode == [2, 1]


def test_generation_config_accepts_media_resolution_enum():
    config = AistudioGenerationConfig([])

    config.media_resolution = MediaResolution.MEDIUM

    assert config.media_resolution == 2


def test_generation_config_seed_maps_to_index_18():
    config = AistudioGenerationConfig([])
    config.seed = 42
    assert config.seed == 42
    assert config.values[18] == 42


def test_wire_codec_retains_schema_with_tools():
    """Verify response_schema and response_mime_type are retained when tools are present."""
    rewritten = encode_request_body(
        model="models/gemini-3.5-flash",
        prompt="test",
        tools=[[[]]],
        generation_config_overrides={
            "response_mime_type": "application/json",
            "response_schema": [6],
        },
    )
    decoded = AistudioWireCodec().decode(rewritten)
    assert decoded.tools == [[[]]]
    assert decoded.generation_config.response_mime_type == "application/json"
    assert decoded.generation_config.response_schema == [6]


def test_encode_request_body_handles_tool_config_semantically_and_preserves_evergreen_uri():
    rewritten = encode_request_body(
        model="models/gemini-3.7-flash",
        prompt="hi",
        tools=[[None, [["get_weather", "desc", None], ["other_fn", "desc", None]]]],
        tool_config=[None, [2, ["get_weather"]]],
    )
    body = json.loads(rewritten)
    # Index 7 is evergreen_model_uri (null or string), never corrupted with a list!
    assert body[7] is None
    # Tools are filtered to allowed_function_names
    assert body[6] == [[None, [["get_weather", "desc", None]]]]

    # Mode NONE suppresses tools completely
    rewritten_none = encode_request_body(
        model="models/gemini-3.7-flash",
        prompt="hi",
        tools=[[None, [["get_weather", "desc", None]]]],
        tool_config=[None, [3]],
    )
    body_none = json.loads(rewritten_none)
    assert len(body_none) <= 6 or body_none[6] is None
    assert len(body_none) <= 7 or body_none[7] is None


def test_executable_code_and_result_strings_round_trip():
    """上游把代码/执行结果作为裸字符串下发时不得被丢弃（下标 8/9）。

    回归点：旧实现用 elif 串联两个分支，同一条 part 同时带代码与执行结果时
    只会保留其中一个，decode→encode 后另一半静默消失。
    """
    codec = AistudioWireCodec()
    part = [None, None, None, None, None, None, None, None, "print(1)", "out1"]
    raw = json.dumps(["models/x", [[[part], "model"]]])

    decoded = codec.decode(raw)
    decoded_part = decoded.contents[0].parts[0]
    assert decoded_part.executable_code == ("PYTHON", "print(1)")
    assert decoded_part.code_execution_result == ("OUTCOME_OK", "out1")
    assert decoded_part.text is None

    rewritten = json.loads(codec.encode(decoded))
    encoded_part = rewritten[1][0][0][0]
    assert encoded_part[8] == [1, "print(1)"]
    assert encoded_part[9] == [1, "out1"]

    # 再解一次确认往返稳定
    again = codec.decode(json.dumps(rewritten)).contents[0].parts[0]
    assert again.executable_code == ("PYTHON", "print(1)")
    assert again.code_execution_result == ("OUTCOME_OK", "out1")


def test_build_tools_from_names_returns_isolated_copies():
    """工具模板必须深拷贝：共享全局 list 会让改动跨请求污染。"""
    first = build_tools_from_names(["google_search"])
    second = build_tools_from_names(["google_search"])

    assert first[0] is not second[0]
    assert first[0][3] is not second[0][3]

    # 污染第一个副本后，新调用必须仍然干净
    first[0][3][0] = "POLLUTED"
    third = build_tools_from_names(["google_search"])
    assert third[0][3][0] is None


def test_build_image_generation_search_tool_returns_isolated_copy():
    first = build_image_generation_search_tool(google_search=True)
    assert first is not None
    second = build_image_generation_search_tool(google_search=True)
    assert second is not None
    assert first is not second
    assert first[3] is not second[3]


def test_part_to_wire_tolerates_short_tuples():
    """单元素/空元组不得触发 IndexError/ValueError（防御上游畸形数据）。

    上游数据不可信，这里刻意用 cast 构造与类型标注不符的元组，
    验证 to_wire 的边界保护真的存在。
    """
    from typing import Any, cast

    from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

    # 单元素 function_call：缺 args 时补空 dict，不抛异常
    call_part = AistudioPart(function_call=cast("Any", ("only_name",)))
    call_wire = cast("list[Any]", call_part.to_wire())
    assert call_wire[10][0] == "only_name"
    assert call_wire[10][1] == [[]]  # 空 dict 编码成单层键值对列表

    # 单元素 function_response
    resp_part = AistudioPart(function_response=cast("Any", ("only_name",)))
    resp_wire = cast("list[Any]", resp_part.to_wire())
    assert resp_wire[11][0] == "only_name"

    # 单元素 executable_code / code_execution_result
    code_part = AistudioPart(executable_code=cast("Any", ("PYTHON",)))
    assert cast("list[Any]", code_part.to_wire())[8] == [1, ""]
    result_part = AistudioPart(code_execution_result=cast("Any", ("OUTCOME_OK",)))
    assert cast("list[Any]", result_part.to_wire())[9] == [1, ""]


def test_part_to_wire_emits_both_code_and_result():
    """同时带代码与执行结果时必须两个下标都写。"""
    from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

    part = AistudioPart(
        executable_code=("PYTHON", "print(1)"),
        code_execution_result=("OUTCOME_OK", "1"),
    )
    encoded = part.to_wire()
    assert encoded[8] == [1, "print(1)"]
    assert encoded[9] == [1, "1"]
