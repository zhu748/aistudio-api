"""Application services for chat/image orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aistudio_api.api.schemas import GeminiGenerateContentRequest, GeminiTool

from dataclasses import dataclass

from aistudio_api.domain.tool_signatures import (
    is_server_side_tool_call as _is_server_side_tool_call,
    valid_thought_signature as _valid_thought_signature,
    valid_thought_signature_or_none as _valid_thought_signature_or_none,
)
from aistudio_api.infrastructure.gateway.model_defaults import (
    resolve_model_defaults,
    snap_thinking_level_to_supported as _snap_thinking_level_to_supported,
    snap_thinking_wire_to_model as _snap_thinking_wire_to_model,
)
from aistudio_api.infrastructure.gateway.wire_codec import build_tools_from_names
from aistudio_api.infrastructure.gateway.wire_spec import JspbArray, SchemaIndex
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioImageOutputMode,
    AistudioPart,
    AistudioThinkingConfig,
    ThinkingLevel,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("chat")


@dataclass
class NormalizedGeminiRequest:
    model: str
    contents: list[AistudioContent]
    system_instruction: AistudioContent | None
    tools: list[list[object]] | None
    safety_settings: list[list[object]] | None
    tool_config: list[object] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    generation_config_overrides: dict[str, object] | None = None


SCHEMA_TYPE_CODES = {
    "string": 1,
    "number": 2,
    "float": 2,
    "integer": 3,
    "int": 3,
    "boolean": 4,
    "bool": 4,
    "array": 5,
    "object": 6,
    "null": 7,
}


def _resolve_schema_type_code(schema: dict[str, object]) -> tuple[int, str, bool]:
    raw_type = schema.get("type")
    is_nullable = False
    if isinstance(raw_type, list):
        non_null_types = [t for t in raw_type if str(t).lower() != "null"]
        if any(str(t).lower() == "null" for t in raw_type):
            is_nullable = True
        raw_type = non_null_types[0] if non_null_types else "string"
    schema_type = str(raw_type or "").split(".")[-1].strip().lower()

    if not schema_type or schema_type not in SCHEMA_TYPE_CODES or schema_type == "null":
        if "properties" in schema:
            schema_type = "object"
        elif "items" in schema or "prefixItems" in schema or "prefix_items" in schema:
            schema_type = "array"
        elif "const" in schema:
            cv = schema["const"]
            if isinstance(cv, str):
                schema_type = "string"
            elif isinstance(cv, bool):
                schema_type = "boolean"
            elif isinstance(cv, int):
                schema_type = "integer"
            elif isinstance(cv, float):
                schema_type = "number"
            elif isinstance(cv, dict):
                schema_type = "object"
            elif isinstance(cv, list):
                schema_type = "array"
        elif (
            "enum" in schema
            and isinstance(schema["enum"], (list, tuple))
            and schema["enum"]
        ):
            all_types = {type(x) for x in schema["enum"]}
            if all_types == {str}:
                schema_type = "string"
            elif all_types == {bool}:
                schema_type = "boolean"
            elif all_types == {int}:
                schema_type = "integer"
            elif all_types <= {int, float}:
                schema_type = "number"
        elif any(
            k in schema
            for k in ("anyOf", "any_of", "oneOf", "one_of", "allOf", "all_of")
        ):
            combiner = (
                schema.get("anyOf")
                or schema.get("any_of")
                or schema.get("oneOf")
                or schema.get("one_of")
                or schema.get("allOf")
                or schema.get("all_of")
            )
            if isinstance(combiner, list) and combiner:
                # OpenAI 常用 {type:"null"} 表达可空：剔除 null 分支后
                # 若只剩一种类型，正确语义是「该类型 + nullable」，而非
                # 降级成 object（null-first / object+null 组合此前都错编）
                branch_types = [
                    str(b.get("type") or "").split(".")[-1].strip().lower()
                    for b in combiner
                    if isinstance(b, dict) and b.get("type")
                ]
                has_null = "null" in branch_types
                non_null = [t for t in branch_types if t != "null"]
                if has_null and len(non_null) == 1:
                    schema_type = non_null[0]
                    is_nullable = True
                elif "object" in non_null:
                    schema_type = "object"
                    if has_null:
                        is_nullable = True
                elif non_null:
                    schema_type = non_null[0]
                    if has_null:
                        is_nullable = True
                else:
                    schema_type = "object"
            else:
                schema_type = "object"
        else:
            schema_type = "object"

    type_code = SCHEMA_TYPE_CODES.get(schema_type, 6)
    if type_code not in (1, 2, 3, 4, 5, 6):
        type_code = 6
    return type_code, schema_type, is_nullable


def _encode_schema_combiners(
    schema: dict[str, object], wire: JspbArray, include_required: bool
) -> None:
    one_of = schema.get("oneOf") or schema.get("one_of")
    if isinstance(one_of, list) and one_of:
        wire[SchemaIndex.ONE_OF] = [
            _encode_schema_to_wire(
                sub if isinstance(sub, dict) else {},
                include_required=include_required,
            )
            for sub in one_of
            if isinstance(sub, dict) or sub is True
        ]

    any_of = schema.get("anyOf") or schema.get("any_of")
    if isinstance(any_of, list) and any_of:
        wire[SchemaIndex.ANY_OF] = [
            _encode_schema_to_wire(
                sub if isinstance(sub, dict) else {},
                include_required=include_required,
            )
            for sub in any_of
            if isinstance(sub, dict) or sub is True
        ]

    all_of = schema.get("allOf") or schema.get("all_of")
    if isinstance(all_of, list) and all_of:
        wire[SchemaIndex.ALL_OF] = [
            _encode_schema_to_wire(
                sub if isinstance(sub, dict) else {},
                include_required=include_required,
            )
            for sub in all_of
            if isinstance(sub, dict) or sub is True
        ]

    not_schema = schema.get("not") or schema.get("not_")
    if isinstance(not_schema, dict):
        wire[SchemaIndex.NOT] = _encode_schema_to_wire(
            not_schema, include_required=include_required
        )


def encode_schema_to_wire(
    schema: dict[str, object], *, include_required: bool = True
) -> list[object]:
    """Resolve local JSON Schema references before encoding the wire subset."""
    remaining = 10_000

    def expand(node: object, refs: tuple[str, ...] = (), depth: int = 0) -> object:
        nonlocal remaining
        remaining -= 1
        if depth > 64 or remaining < 0:
            raise ValueError("JSON Schema expansion exceeds complexity limit")
        if isinstance(node, list):
            return [expand(item, refs, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if reference is not None:
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise ValueError("Only local JSON Schema $ref references are supported")
            if reference in refs:
                raise ValueError(
                    f"Recursive JSON Schema reference is not supported: {reference}"
                )
            target: object = schema
            for segment in reference[2:].split("/"):
                key = segment.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or key not in target:
                    raise ValueError(f"Unresolved JSON Schema reference: {reference}")
                target = target[key]
            resolved = expand(target, (*refs, reference), depth + 1)
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            if siblings:
                return {"allOf": [resolved, expand(siblings, refs, depth + 1)]}
            return resolved
        result: dict[str, object] = {}
        for key, value in node.items():
            if key in ("$defs", "definitions"):
                continue
            if key in (
                "properties",
                "patternProperties",
                "dependentSchemas",
            ) and isinstance(value, dict):
                result[key] = {
                    name: expand(child, refs, depth + 1)
                    for name, child in value.items()
                }
            elif key in (
                "items",
                "prefixItems",
                "prefix_items",
                "anyOf",
                "any_of",
                "oneOf",
                "one_of",
                "allOf",
                "all_of",
                "not",
                "not_",
                "additionalProperties",
                "propertyNames",
                "contains",
                "if",
                "then",
                "else",
            ):
                result[key] = expand(value, refs, depth + 1)
            else:
                # Literal data and property names can themselves contain '$ref'.
                result[key] = value
        return result

    resolved = expand(schema)
    if not isinstance(resolved, dict):
        raise ValueError("JSON Schema must resolve to an object")
    return _encode_schema_to_wire(resolved, include_required=include_required)


def _encode_schema_to_wire(
    schema: dict[str, object], *, include_required: bool = True
) -> list[object]:
    type_code, schema_type, is_nullable = _resolve_schema_type_code(schema)
    wire = JspbArray([type_code])

    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt:
        wire[SchemaIndex.FORMAT] = fmt

    desc = schema.get("description")
    if isinstance(desc, str) and desc:
        wire[SchemaIndex.DESCRIPTION] = desc

    nullable = schema.get("nullable")
    if isinstance(nullable, bool):
        wire[SchemaIndex.NULLABLE] = nullable
    elif is_nullable:
        wire[SchemaIndex.NULLABLE] = True

    enum_vals = schema.get("enum")
    if not enum_vals and "const" in schema:
        enum_vals = [schema["const"]]
    if isinstance(enum_vals, (list, tuple)) and enum_vals:
        wire[SchemaIndex.ENUM] = [str(x) for x in enum_vals]

    items = schema.get("items")
    if schema_type == "array" or items is not None:
        if isinstance(items, dict):
            wire[SchemaIndex.ITEMS] = _encode_schema_to_wire(
                items,
                include_required=include_required,
            )
        elif isinstance(items, list) and items and isinstance(items[0], dict):
            wire[SchemaIndex.ITEMS] = _encode_schema_to_wire(
                items[0],
                include_required=include_required,
            )
        elif items is True:
            wire[SchemaIndex.ITEMS] = [6]

    properties = schema.get("properties")
    if isinstance(properties, dict):
        wire[SchemaIndex.PROPERTIES] = [
            [
                name,
                _encode_schema_to_wire(
                    prop if isinstance(prop, dict) else {},
                    include_required=include_required,
                ),
            ]
            for name, prop in properties.items()
            if isinstance(prop, dict) or prop is True
        ]

    required = schema.get("required")
    if include_required and isinstance(required, (list, tuple, set)):
        wire[SchemaIndex.REQUIRED] = [str(x) for x in required]

    min_props = schema.get("minProperties", schema.get("min_properties"))
    if isinstance(min_props, int):
        wire[SchemaIndex.MIN_PROPERTIES] = min_props

    max_props = schema.get("maxProperties", schema.get("max_properties"))
    if isinstance(max_props, int):
        wire[SchemaIndex.MAX_PROPERTIES] = max_props

    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        wire[SchemaIndex.MINIMUM] = minimum

    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        wire[SchemaIndex.MAXIMUM] = maximum

    min_len = schema.get("minLength", schema.get("min_length"))
    if isinstance(min_len, int):
        wire[SchemaIndex.MIN_LENGTH] = min_len

    max_len = schema.get("maxLength", schema.get("max_length"))
    if isinstance(max_len, int):
        wire[SchemaIndex.MAX_LENGTH] = max_len

    pattern = schema.get("pattern")
    if isinstance(pattern, str) and pattern:
        wire[SchemaIndex.PATTERN] = pattern

    _encode_schema_combiners(schema, wire, include_required)

    max_items = schema.get("maxItems", schema.get("max_items"))
    if isinstance(max_items, int):
        wire[SchemaIndex.MAX_ITEMS] = max_items

    min_items = schema.get("minItems", schema.get("min_items"))
    if isinstance(min_items, int):
        wire[SchemaIndex.MIN_ITEMS] = min_items

    example = schema.get("example", schema.get("examples"))
    if isinstance(example, (str, int, float, bool)):
        wire[SchemaIndex.EXAMPLE] = example

    # default (Field 25) 不存在于 Google Schema Proto（wire_spec 逆向
    # 结论：注入下标 23+ 会 400）。把 default 折叠进 description 尾部
    # 而非静默丢弃——模型仍可感知默认值语义
    default_val = schema.get("default")
    if default_val is not None:
        default_str = str(default_val)
        if default_str:
            desc_now = (
                wire[SchemaIndex.DESCRIPTION]
                if len(wire) > SchemaIndex.DESCRIPTION
                else None
            )
            if isinstance(desc_now, str) and desc_now:
                wire[SchemaIndex.DESCRIPTION] = f"{desc_now} (default: {default_str})"
            else:
                wire[SchemaIndex.DESCRIPTION] = f"(default: {default_str})"

    property_ordering = schema.get("propertyOrdering") or schema.get(
        "property_ordering"
    )
    if not isinstance(property_ordering, list):
        # 2026-11 AI Studio 真机抓包（hybrid6 函数调用实验）：前端对
        # object schema 恒在槽位 22 携带 propertyOrdering（= properties
        # 的键序快照），即使客户端未显式声明。缺省时按键序推导对齐
        # 浏览器线缆形态；仅 object 且有属性时推导。
        if schema_type == "object" and isinstance(properties, dict) and properties:
            property_ordering = [
                str(name)
                for name, prop in properties.items()
                if isinstance(prop, dict) or prop is True
            ]
        else:
            property_ordering = None
    if isinstance(property_ordering, list):
        wire[SchemaIndex.PROPERTY_ORDERING] = list(property_ordering)

    return wire.compact(max_index=SchemaIndex.PROPERTY_ORDERING)


def encode_function_declaration_to_wire(declaration: dict[str, object]) -> list[object]:
    if isinstance(declaration.get("function"), dict):
        declaration = declaration["function"]  # type: ignore[assignment]
    if not declaration.get("name"):
        raise ValueError("functionDeclarations[].name is required")
    wire = JspbArray([declaration["name"]])
    if declaration.get("description") is not None:
        wire[1] = declaration["description"]

    parameters = (
        declaration.get("parameters")
        or declaration.get("parametersJsonSchema")
        or declaration.get("parameters_json_schema")
        or declaration.get("input_schema")
        or declaration.get("parameters_schema")
    )
    if isinstance(parameters, dict):
        wire[2] = encode_schema_to_wire(parameters, include_required=True)
    elif isinstance(parameters, list):
        wire[2] = parameters

    return wire.compact()


def _normalize_gemini_modalities(
    value: object, *, drop_unsupported: bool = True
) -> AistudioImageOutputMode | None:
    if value is None:
        return None
    if not isinstance(value, list):
        if drop_unsupported:
            return None
        raise ValueError("generationConfig.responseModalities must be a list")

    modalities = {str(item).strip().upper() for item in value if str(item).strip()}
    if not modalities:
        return None
    unknown = modalities - {"TEXT", "IMAGE"}
    if unknown:
        if not drop_unsupported:
            raise ValueError(
                f"Unsupported response modalities: {', '.join(sorted(unknown))}"
            )
        modalities = modalities & {"TEXT", "IMAGE"}
        if not modalities:
            return None
    if "IMAGE" not in modalities:
        return None
    if "TEXT" in modalities:
        return AistudioImageOutputMode.text_and_image()
    return AistudioImageOutputMode.image_only()


_REASONING_EFFORT_TO_LEVEL: dict[str, ThinkingLevel] = {
    "MINIMAL": ThinkingLevel.MINIMAL,
    "LOW": ThinkingLevel.LOW,
    "MEDIUM": ThinkingLevel.MEDIUM,
    "HIGH": ThinkingLevel.HIGH,
    # 2026-11 目录实测新档（gemini-4-hydrogen thinkingLevels=[1,2,3,5]）；
    # openai/claude 兼容层透传 very_high 文本，此处按 upper 后带下划线命中。
    "VERY_HIGH": ThinkingLevel.VERY_HIGH,
    # xhigh：较新 openai 客户端 effort 词汇（GPT-5.1-codex-max 系选择器），
    # 与 VERY_HIGH(5) 同档；分隔符变体（X-HIGH/X_HIGH/XHIGH）由下方
    # compact 匹配统一命中。
    "XHIGH": ThinkingLevel.VERY_HIGH,
}


def _parse_thinking_level(raw_level: object) -> ThinkingLevel | None:
    if raw_level is None:
        return None
    if isinstance(raw_level, ThinkingLevel):
        return raw_level
    if isinstance(raw_level, int) and not isinstance(raw_level, bool):
        return ThinkingLevel(raw_level)
    label = str(raw_level).strip().upper()
    if not label or label in ("NONE", "OFF", "DISABLE", "DISABLED", "0"):
        return None
    if label in _REASONING_EFFORT_TO_LEVEL:
        return _REASONING_EFFORT_TO_LEVEL[label]
    # 分隔符变体兼容：VERY_HIGH / VERY-HIGH / VERYHIGH 等写法统一命中。
    compact = label.replace("_", "").replace("-", "")
    for key, value in _REASONING_EFFORT_TO_LEVEL.items():
        if key.replace("_", "") == compact:
            return value
    try:
        return ThinkingLevel[label]
    except KeyError:
        # 无效档位是客户端参数错误（400），KeyError 会漏到未分类异常
        # 路径变成 500 且误给服务账号记健康度惩罚
        raise ValueError(
            f"未识别的 thinkingLevel/reasoningEffort: {raw_level}"
        ) from None


# 思考档位吸附与浏览器形态默认值的模型自适应已下沉至基础设施层
# （model_defaults.snap_thinking_level_to_supported / snap_thinking_wire_to_model），
# 使 wire 构造层注入的 profile 默认思考档也走同一套档位兼容链路。
# 旧版 chat_service 本地实现已删除，上方以别名方式导入。


def _user_explicitly_disabled_thinking(value: object) -> bool:
    """判断用户是否显式关闭思考（thinkingLevel=NONE/OFF 或 budget=0）。

    与「空 thinkingConfig」区分：空对象（如 {} / includeThoughts 单独出现）
    表示未指定档位，profile 注入的浏览器默认档应照常生效；仅显式关闭
    才需要以 thinking_config=None 抑制默认档。
    """
    if not isinstance(value, dict):
        return False
    raw_level = value.get(
        "thinkingLevel", value.get("thinking_level", value.get("level"))
    )
    if isinstance(raw_level, str) and raw_level.strip().upper() in (
        "NONE",
        "OFF",
        "DISABLE",
        "DISABLED",
    ):
        return True
    raw_budget = value.get(
        "thinkingBudget", value.get("thinking_budget", value.get("budget"))
    )
    return bool(
        isinstance(raw_budget, int)
        and not isinstance(raw_budget, bool)
        and raw_budget == 0
        and raw_level is None
        and value.get("mode") is None
    )


def _normalize_gemini_thinking_config(
    value: object,
) -> list[object] | None:
    if value is None or isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ValueError(
            "generationConfig.thinkingConfig must be an object or wire array"
        )

    raw_level = value.get(
        "thinkingLevel", value.get("thinking_level", value.get("level"))
    )
    raw_budget = value.get(
        "thinkingBudget", value.get("thinking_budget", value.get("budget"))
    )
    raw_mode = value.get("mode")
    raw_include = value.get("includeThoughts", value.get("include_thoughts"))

    if isinstance(raw_level, str) and raw_level.strip().upper() in (
        "NONE",
        "OFF",
        "DISABLE",
        "DISABLED",
    ):
        return None
    if (
        raw_include is False
        and raw_level is None
        and raw_budget is None
        and raw_mode is None
    ):
        return None

    level = _parse_thinking_level(raw_level)
    budget: int | None = None
    if raw_budget is not None and not isinstance(raw_budget, bool):
        budget = int(str(raw_budget))
        if budget == 0 and level is None and raw_mode is None:
            return None

    if level is None and budget is None and raw_mode is None:
        return None

    mode = (
        int(str(raw_mode))
        if raw_mode is not None and not isinstance(raw_mode, bool)
        else 1
    )
    return AistudioThinkingConfig(level=level, mode=mode, budget=budget).to_wire()


def _normalize_gemini_image_config(
    value: object, *, drop_unsupported: bool = True
) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        if drop_unsupported:
            return {}
        raise ValueError("generationConfig.imageConfig must be an object")

    aspect_ratio = value.get("aspectRatio")
    image_size = value.get("imageSize")
    if isinstance(aspect_ratio, str) and not aspect_ratio.strip():
        aspect_ratio = None
    if isinstance(image_size, str):
        image_size = image_size.strip() or None
    person_generation = value.get("personGeneration")
    if person_generation not in (None, "") and not drop_unsupported:
        raise ValueError(
            "generationConfig.imageConfig.personGeneration is not supported yet"
        )

    normalized: dict[str, object] = {}
    if aspect_ratio is not None or image_size is not None:
        normalized["output_resolution"] = [aspect_ratio, image_size]
    return normalized


def _normalize_gemini_tool_config(value: object) -> list[object] | None:
    """toolConfig → fcc wire（语义解析用，MakerSuite wire 无 tool_config 槽位）。

    2026-11 前端 bundle 逆向结论（boq-makersuite jspb 类 _.Rv 全字段）：
    MakerSuite GenerateContentRequest 顶层字段为 1=model, 2=contents,
    3=safety, 4=generation_config, 5=snapshot, 6=system_instruction,
    7=tools, 12=session_token, 13=conversation_tracking_mode,
    14=request_context, 15=api_key, 16=preamble_config —— **不存在
    tool_config 字段**。上游 "Please enable
    tool_config.include_server_side_tool_invocations" 400 是其后端内部
    校验（内置工具与 Function calling 组合的限制），前端 wire 层无法
    控制该开关；本网关的对策是不主动混发该组合（见 tools 注入逻辑）。

    本函数产出 fcc wire 形态供 build_request 的语义过滤（NONE 置空
    tools / allowedNames 过滤）使用；NONE 与 allowedNames 的处理也同步
    在 normalize_gemini_request 中前置执行。
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    model_dump_fn = getattr(value, "model_dump", None)
    if callable(model_dump_fn):
        value = model_dump_fn(mode="json", exclude_none=True)
    if not isinstance(value, dict):
        return None

    fcc = value.get("functionCallingConfig") or value.get("function_calling_config")
    if not isinstance(fcc, dict):
        return None

    mode_map = {
        "MODE_UNSPECIFIED": 0,
        "AUTO": 1,
        "ANY": 2,
        "NONE": 3,
        "VALIDATED": 4,
    }
    raw_mode = fcc.get("mode", "AUTO")
    if isinstance(raw_mode, int):
        mode_code = raw_mode
    else:
        mode_code = mode_map.get(str(raw_mode).strip().upper(), 1)

    allowed_names = fcc.get("allowedFunctionNames") or fcc.get("allowed_function_names")
    fcc_wire: list[object] = [mode_code]
    if isinstance(allowed_names, list) and allowed_names:
        fcc_wire.append(list(allowed_names))

    # includeServerSideToolInvocations 在 MakerSuite wire 协议中无槽位可
    # 编码（见函数 docstring），仅保留在语义层供观察/后续上游支持。
    include_invocations = value.get("includeServerSideToolInvocations") or value.get(
        "include_server_side_tool_invocations"
    )
    tool_config_wire: list[object] = [None, fcc_wire]
    if include_invocations is not None:
        tool_config_wire.append(bool(include_invocations))
    return tool_config_wire


def _extract_google_search_tool_names(
    tool: GeminiTool, *, is_image_model: bool
) -> list[str]:
    if tool.googleSearchRetrieval is not None:
        return ["google_search"]

    config = tool.googleSearch
    if config is None:
        return []
    if not is_image_model or not isinstance(config, dict):
        return ["google_search"]

    search_types = config.get("searchTypes")
    if not isinstance(search_types, dict):
        return ["google_search"]

    web_enabled = search_types.get("webSearch") is not None
    image_enabled = search_types.get("imageSearch") is not None
    if web_enabled and image_enabled:
        return ["google_search_and_image_search"]
    if image_enabled:
        return ["image_search"]
    return ["google_search"]


def _filter_default_tools_for_model(
    tool_names: tuple[str, ...], *, is_image_model: bool
) -> list[str]:
    names = [str(name).strip() for name in tool_names if str(name).strip()]
    if not is_image_model:
        return names
    allowed = {"google_search", "image_search", "google_search_and_image_search"}
    return [name for name in names if name in allowed]


# 内置搜索工具的覆盖关系：复合工具覆盖其组成部分，去重时据此跳过被覆盖的窄工具。
_BUILTIN_TOOL_COVERS = {
    "google_search_and_image_search": ("google_search", "image_search"),
}


def _drop_covered_builtin_tools(names: list[str], seen: set[str]) -> list[str]:
    """从 names 去掉已被 ``seen`` 覆盖或被同批 names 覆盖的内置工具。

    例如 seen 含 google_search_and_image_search 时，google_search 被视为冗余跳过；
    同一批 names 里两者并存时也只保留复合工具。
    """
    have: set[str] = set(seen)
    result: list[str] = []
    for name in names:
        if name in have:
            continue
        if any(
            name in subs and sup in have for sup, subs in _BUILTIN_TOOL_COVERS.items()
        ):
            continue
        result.append(name)
        have.add(name)
    return result


_GEMINI_SAFETY_CATEGORY_MAP = {
    "HARM_CATEGORY_HARASSMENT": 7,
    "HARM_CATEGORY_HATE_SPEECH": 8,
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": 9,
    "HARM_CATEGORY_DANGEROUS_CONTENT": 10,
}

_GEMINI_SAFETY_THRESHOLD_MAP = {
    "BLOCK_LOW_AND_ABOVE": 1,
    "BLOCK_MEDIUM_AND_ABOVE": 2,
    "BLOCK_ONLY_HIGH": 3,
    "BLOCK_NONE": 4,
    "OFF": 5,
}


def _normalize_gemini_safety_settings(
    value: object, *, drop_unsupported: bool = True
) -> list[list[object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        if drop_unsupported:
            return []
        raise ValueError("safetySettings must be a list")

    normalized: list[list[object]] = []
    for item in value:
        raw_cat = (
            getattr(item, "category", None)
            if hasattr(item, "category")
            else (item.get("category") if isinstance(item, dict) else None)
        )
        raw_thresh = (
            getattr(item, "threshold", None)
            if hasattr(item, "threshold")
            else (item.get("threshold") if isinstance(item, dict) else None)
        )

        if raw_cat is None or raw_thresh is None:
            if drop_unsupported:
                continue
            raise ValueError(f"Unsupported safety setting entry: {item!r}")

        category = _GEMINI_SAFETY_CATEGORY_MAP.get(str(raw_cat).strip().upper())
        if category is None:
            if drop_unsupported:
                continue
            raise ValueError(f"Unsupported safety category: {raw_cat}")
        threshold = _GEMINI_SAFETY_THRESHOLD_MAP.get(str(raw_thresh).strip().upper())
        if threshold is None:
            if drop_unsupported:
                continue
            raise ValueError(f"Unsupported safety threshold: {raw_thresh}")
        normalized.append([None, None, category, threshold])
    return normalized


def normalize_gemini_request(
    req: GeminiGenerateContentRequest, requested_model: str
) -> NormalizedGeminiRequest:
    if not req.contents:
        raise ValueError("contents is required")

    model = (
        requested_model
        if requested_model.startswith("models/")
        else f"models/{requested_model}"
    )
    contents: list[AistudioContent] = []
    # 被剥离的服务端工具调用 id（无有效签名，见 _is_server_side_tool_call）：
    # 对应的 functionResponse 一并跳过，避免孤立工具结果触发上游报错。
    dropped_server_tool_ids: set[str] = set()

    for content in req.contents:
        role = content.role or "user"
        parts: list[AistudioPart] = []
        for part in content.parts:
            if part.text is not None:
                is_thought = bool(part.thought)
                parts.append(
                    AistudioPart(
                        text=part.text,
                        thought=is_thought,
                        thought_signature=_valid_thought_signature_or_none(
                            part.thoughtSignature
                        ),
                    )
                )
                continue
            if part.inlineData is not None:
                img_data = (part.inlineData.mimeType, part.inlineData.data)
                parts.append(
                    AistudioPart(
                        inline_data=img_data,
                        thought_signature=_valid_thought_signature_or_none(
                            part.thoughtSignature
                        ),
                    )
                )
                continue
            if part.functionCall is not None:
                fc = part.functionCall
                # 服务端工具（default_api:* / 内置工具）无有效签名时剥离：
                # 部分客户端/转译层不保存 thoughtSignature，回传历史缺签名
                # 会触发上游 400（"Function call is missing a thought_signature
                # in functionCall parts"）。
                if _is_server_side_tool_call(fc.name) and not _valid_thought_signature(
                    part.thoughtSignature
                ):
                    if fc.id:
                        dropped_server_tool_ids.add(fc.id)
                    logger.info(
                        "剥离无签名的服务端工具调用: name=%s, call_id=%s",
                        fc.name,
                        fc.id or "(无id)",
                    )
                    continue
                args_payload = fc.args if fc.args is not None else {}
                parts.append(
                    AistudioPart(
                        function_call=(fc.name, args_payload, fc.id)
                        if fc.id
                        else (fc.name, args_payload),
                        thought_signature=_valid_thought_signature_or_none(
                            part.thoughtSignature
                        ),
                    )
                )
                continue
            if part.functionResponse is not None:
                fr = part.functionResponse
                # 跳过被剥离的服务端工具调用的结果
                if fr.id and fr.id in dropped_server_tool_ids:
                    continue
                resp_payload = fr.response if fr.response is not None else {}
                parts.append(
                    AistudioPart(
                        function_response=(fr.name, resp_payload, fr.id)
                        if fr.id
                        else (fr.name, resp_payload),
                    )
                )
                if fr.parts:
                    for subpart in fr.parts:
                        if subpart.inlineData is not None:
                            img_data = (
                                subpart.inlineData.mimeType,
                                subpart.inlineData.data,
                            )
                            parts.append(
                                AistudioPart(
                                    inline_data=img_data,
                                    thought_signature=subpart.thoughtSignature,
                                )
                            )
                        elif subpart.text is not None:
                            parts.append(AistudioPart(text=subpart.text))
                continue
            if part.executableCode is not None:
                ec = part.executableCode
                parts.append(
                    AistudioPart(
                        executable_code=(ec.language, ec.code),
                        thought_signature=part.thoughtSignature,
                    )
                )
                continue
            if part.codeExecutionResult is not None:
                cer = part.codeExecutionResult
                parts.append(
                    AistudioPart(
                        code_execution_result=(cer.outcome, cer.output),
                        thought_signature=part.thoughtSignature,
                    )
                )
                continue
            if part.fileData is not None:
                raise ValueError("fileData is not supported yet")
            if part.thoughtSignature:
                parts.append(AistudioPart(thought_signature=part.thoughtSignature))
        contents.append(AistudioContent(role=role, parts=parts))
    system_instruction = None
    if req.systemInstruction is not None:
        system_instruction = AistudioContent(
            role=req.systemInstruction.role or "user",
            parts=[
                AistudioPart(
                    text=part.text,
                    thought_signature=part.thoughtSignature,
                )
                if part.text is not None
                else AistudioPart(
                    inline_data=(part.inlineData.mimeType, part.inlineData.data)
                    if part.inlineData
                    else ("", ""),
                    thought_signature=part.thoughtSignature,
                )
                for part in req.systemInstruction.parts
                if part.text is not None or part.inlineData is not None
            ],
        )

    model_defaults = resolve_model_defaults(model)
    tools = None
    seen_builtin: set[str] = set()
    if req.tools is not None:
        tools = []
        all_function_declarations: list[list[object]] = []
        for tool in req.tools:
            builtin_tool_names: list[str] = []
            if tool.codeExecution is not None:
                builtin_tool_names.append("code_execution")
            if tool.functionDeclarations:
                for decl in tool.functionDeclarations:
                    if isinstance(decl, dict):
                        all_function_declarations.append(
                            encode_function_declaration_to_wire(decl)
                        )
            builtin_tool_names.extend(
                _extract_google_search_tool_names(
                    tool, is_image_model=model_defaults.is_image_model
                )
            )
            if tool.googleMaps is not None:
                builtin_tool_names.append("google_maps")
            if tool.urlContext is not None:
                builtin_tool_names.append("url_context")
            if builtin_tool_names:
                filtered_builtin = _drop_covered_builtin_tools(
                    builtin_tool_names, seen_builtin
                )
                if filtered_builtin:
                    tools.extend(
                        build_tools_from_names(
                            filtered_builtin,
                            model=model,
                            is_image_model=model_defaults.is_image_model,
                            drop_unsupported=model_defaults.drop_unsupported_params,
                        )
                    )
                    seen_builtin.update(filtered_builtin)
        if all_function_declarations:
            tools.append([None, all_function_declarations])

    # 仅当客户端未传 tools 时注入 config.yaml 的 default_tools。
    # 若客户端显式声明了 tools（如自定义函数声明），绝不自动混入 Google Search 等内置工具，
    # 避免触发 Google MakerSuite "Please enable tool_config.include_server_side_tool_invocations" 400 报错。
    if req.tools is None and model_defaults.default_tools:
        default_tool_names = _filter_default_tools_for_model(
            model_defaults.default_tools,
            is_image_model=model_defaults.is_image_model,
        )
        injected = (
            build_tools_from_names(
                default_tool_names,
                model=model,
                is_image_model=model_defaults.is_image_model,
            )
            if default_tool_names
            else []
        )
        tools = injected if injected else None

    raw_tool_config = getattr(req, "toolConfig", None)
    tool_config = _normalize_gemini_tool_config(raw_tool_config)

    # 检查工具调用模式。若为 NONE，显式置空 tools 与 tool_config，确保模型仅输出纯文本且不调用任何工具。
    # allowedFunctionNames（tool_choice 指定具体函数）在网关层过滤候选工具列表。
    is_mode_none = False
    allowed_names: list[str] | None = None
    if isinstance(raw_tool_config, dict):
        fcc = raw_tool_config.get("functionCallingConfig") or raw_tool_config.get(
            "function_calling_config"
        )
        if isinstance(fcc, dict):
            m_val = fcc.get("mode")
            if m_val in (3, "NONE", "none"):
                is_mode_none = True
            raw_allowed = fcc.get("allowedFunctionNames") or fcc.get(
                "allowed_function_names"
            )
            if isinstance(raw_allowed, list) and raw_allowed:
                allowed_names = [str(x) for x in raw_allowed]

    if is_mode_none:
        tools = None
        tool_config = None
    elif allowed_names is not None and tools:
        # 与旧 wire 层过滤逻辑对齐：仅保留声明名在 allowed 集合内的
        # functionDeclarations 工具容器；内置工具容器不受影响。
        allowed_set = set(allowed_names)
        filtered_tools: list[list[object]] = []
        for t in tools:
            if isinstance(t, list) and len(t) > 1 and isinstance(t[1], list):
                filtered_decls = [
                    d for d in t[1] if isinstance(d, list) and d and d[0] in allowed_set
                ]
                if filtered_decls:
                    filtered_tools.append([t[0], filtered_decls])
            else:
                filtered_tools.append(t)
        tools = filtered_tools or None
    generation_config = req.generationConfig
    # 只透传用户显式指定的生成参数：profile 的浏览器形态默认值
    # （max_tokens/temperature/top_p/top_k/thinking/…）由 wire_codec.build_request
    # 统一注入并做模型自适应（元数据默认档位/flag13/输出上限钳制/清除语义），
    # 这里不再预先合并 —— 否则 build_request 将无法区分「用户指定」与
    # 「profile 默认」，钳制与档位自适应会因此失效。
    generation_config_overrides: dict[str, object] | None = None
    if generation_config is not None:
        if generation_config_overrides is None:
            generation_config_overrides = {}
        if generation_config.stopSequences is not None:
            generation_config_overrides["stop_sequences"] = (
                generation_config.stopSequences
            )
        if generation_config.maxOutputTokens is not None:
            generation_config_overrides["max_tokens"] = (
                generation_config.maxOutputTokens
            )
        if generation_config.temperature is not None:
            generation_config_overrides["temperature"] = generation_config.temperature
        if generation_config.topP is not None:
            generation_config_overrides["top_p"] = generation_config.topP
        if generation_config.topK is not None:
            generation_config_overrides["top_k"] = generation_config.topK
        if generation_config.responseMimeType is not None:
            generation_config_overrides["response_mime_type"] = (
                generation_config.responseMimeType
            )
        if generation_config.responseSchema is not None:
            generation_config_overrides["response_schema"] = (
                encode_schema_to_wire(generation_config.responseSchema)
                if isinstance(generation_config.responseSchema, dict)
                else generation_config.responseSchema
            )
        if generation_config.presencePenalty is not None:
            generation_config_overrides["presence_penalty"] = (
                generation_config.presencePenalty
            )
        if generation_config.frequencyPenalty is not None:
            generation_config_overrides["frequency_penalty"] = (
                generation_config.frequencyPenalty
            )
        if generation_config.responseLogprobs is not None:
            generation_config_overrides["response_logprobs"] = (
                generation_config.responseLogprobs
            )
        if generation_config.logprobs is not None:
            generation_config_overrides["logprobs"] = generation_config.logprobs
        if generation_config.seed is not None:
            generation_config_overrides["seed"] = generation_config.seed
        if generation_config.mediaResolution is not None:
            # 官方 REST 是字符串枚举，wire 槽位 17 只收 int（1/2/3）；
            # 字符串原样入 wire 会被上游拒绝或静默忽略
            media_res = generation_config.mediaResolution
            if isinstance(media_res, str):
                media_res = media_res.strip().upper().removeprefix("MEDIA_RESOLUTION_")
                media_res = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(media_res)
            if isinstance(media_res, (int, float)) and not isinstance(media_res, bool):
                generation_config_overrides["media_resolution"] = int(media_res)
            # 无法识别的形态按 drop 策略忽略（不透传非法值）
        if generation_config.thinkingConfig is not None:
            normalized_thinking = _normalize_gemini_thinking_config(
                generation_config.thinkingConfig
            )
            if normalized_thinking is not None:
                # 模型档位兼容：等级不支持时自动升/降档，无思考能力则丢弃
                normalized_thinking = _snap_thinking_wire_to_model(
                    model, normalized_thinking
                )
            if normalized_thinking is not None:
                if generation_config_overrides is None:
                    generation_config_overrides = {}
                generation_config_overrides["thinking_config"] = normalized_thinking
            elif _user_explicitly_disabled_thinking(generation_config.thinkingConfig):
                # 用户显式关闭思考 → 显式置 None，抑制 profile 注入的浏览器默认档
                if generation_config_overrides is None:
                    generation_config_overrides = {}
                generation_config_overrides["thinking_config"] = None
            # 其余（空 thinkingConfig 等）不注入 → profile 浏览器默认档生效
        if generation_config.responseModalities is not None:
            image_output_mode = _normalize_gemini_modalities(
                generation_config.responseModalities,
                drop_unsupported=model_defaults.drop_unsupported_params,
            )
            if image_output_mode is not None:
                generation_config_overrides["image_output_mode"] = image_output_mode
        if generation_config.imageConfig is not None:
            generation_config_overrides.update(
                _normalize_gemini_image_config(
                    generation_config.imageConfig,
                    drop_unsupported=model_defaults.drop_unsupported_params,
                )
            )

    effective_effort = (
        generation_config.reasoningEffort if generation_config else None
    ) or req.reasoningEffort
    if effective_effort is not None and (
        generation_config is None or generation_config.thinkingConfig is None
    ):
        level = _parse_thinking_level(effective_effort)
        if level is not None:
            # 模型档位兼容：等级不支持时自动升/降档，无思考能力则不注入
            snapped = _snap_thinking_level_to_supported(model, level)
            if snapped is not None:
                if generation_config_overrides is None:
                    generation_config_overrides = {}
                generation_config_overrides["thinking_config"] = AistudioThinkingConfig(
                    level=snapped, mode=1
                ).to_wire()
        else:
            # reasoningEffort 显式为 none/off → 抑制 profile 默认档
            if generation_config_overrides is None:
                generation_config_overrides = {}
            generation_config_overrides["thinking_config"] = None

    return NormalizedGeminiRequest(
        model=model,
        contents=contents,
        system_instruction=system_instruction,
        tools=tools if tools is not None else None,
        tool_config=tool_config,
        safety_settings=_normalize_gemini_safety_settings(
            req.safetySettings,
            drop_unsupported=model_defaults.drop_unsupported_params,
        )
        if req.safetySettings is not None
        else None,
        temperature=generation_config.temperature if generation_config else None,
        top_p=generation_config.topP if generation_config else None,
        top_k=generation_config.topK if generation_config else None,
        max_tokens=generation_config.maxOutputTokens if generation_config else None,
        generation_config_overrides=generation_config_overrides or None,
    )
