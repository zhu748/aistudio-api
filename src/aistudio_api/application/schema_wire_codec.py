"""JSON Schema -> JSPB wire 编码器（自 chat_service 拆出）。

函数声明 / 结构化输出 schema 到 Playground wire 数组的编码与降级链
（AIStudio2API encodeFunctionDeclaration / encodeResponseSchema 移植）。
对外稳定入口经 chat_service 重导出（anti_truncation 与既有测试的 import
路径保持不变）；新代码请直接从本模块导入。
"""

from __future__ import annotations

import copy
import json

from aistudio_api.infrastructure.gateway.wire_spec import JspbArray, SchemaIndex
from aistudio_api.infrastructure.gateway.wire_types import (
    wire_function_name as _wire_function_name,
)

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
        # 上游拒绝空串 enum 值（AIStudio2API geminiSchema 同款过滤）：
        # 剔除空串，全空则整体省略；含空串的 schema 由降级链把完整
        # JSON Schema 附加到 description 兜底
        filtered_enum = [str(x) for x in enum_vals if str(x) != ""]
        if filtered_enum:
            wire[SchemaIndex.ENUM] = filtered_enum

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


# 完整 schema 附加到 description 时的前缀（AIStudio2API 同文）
_TOOL_ARGUMENTS_SCHEMA_NOTE = "Arguments must follow this JSON Schema: "
_RESPONSE_SCHEMA_NOTE = "The response must follow this JSON Schema: "

# 声明侧：wire 无法表达、需附加完整 schema 到 description 的键
# （AIStudio2API encodeFunctionDeclaration 的字符串包含判定，改为键遍历）
_DECLARATION_LOSSY_KEYS = frozenset(
    {
        "additionalProperties",
        "additional_properties",
        "prefixItems",
        "prefix_items",
        "exclusiveMinimum",
        "exclusive_minimum",
        "propertyNames",
        "property_names",
        "$ref",
    }
)

# 结构化输出侧：jsonSchemaBeyondPlayground 的键集（additionalProperties
# 仅真值时触发，单独判断）
_RESPONSE_LOSSY_KEYS = frozenset(
    {
        "prefixItems",
        "prefix_items",
        "propertyNames",
        "property_names",
        "exclusiveMinimum",
        "exclusive_minimum",
        "exclusiveMaximum",
        "exclusive_maximum",
        "uniqueItems",
        "unique_items",
    }
)

# shape 提取时需要兼容的别名：canonical → 可接受的 snake_case 形态
_SHAPE_KEY_ALIASES = {
    "minLength": ("min_length",),
    "maxLength": ("max_length",),
    "minItems": ("min_items",),
    "maxItems": ("max_items",),
    "minProperties": ("min_properties",),
    "maxProperties": ("max_properties",),
    "propertyOrdering": ("property_ordering",),
}


def _schema_has_lossy_keys(node: object, *, response: bool = False) -> bool:
    """递归查找 wire 无法表达的 schema 约束键。

    声明侧（response=False）对照 AIStudio2API encodeFunctionDeclaration 的
    触发集：additionalProperties（任意值）、prefixItems、exclusiveMinimum、
    propertyNames、$ref。结构化输出侧（response=True）对照
    jsonSchemaBeyondPlayground：另含 exclusiveMaximum / uniqueItems，且
    additionalProperties 仅真值触发（false 是 Gemini 对象的默认闭合语义）。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if response:
                if key in _RESPONSE_LOSSY_KEYS:
                    return True
                if (
                    key in ("additionalProperties", "additional_properties")
                    and value is not False
                ):
                    return True
            elif key in _DECLARATION_LOSSY_KEYS:
                return True
            if _schema_has_lossy_keys(value, response=response):
                return True
        return False
    if isinstance(node, list):
        return any(_schema_has_lossy_keys(item, response=response) for item in node)
    return False


def _schema_has_empty_enum(node: object) -> bool:
    """任一层 enum 含空串（上游拒绝的值，AIStudio2API schemaHasEmptyEnum）。"""
    if isinstance(node, dict):
        values = node.get("enum")
        if isinstance(values, (list, tuple)) and any(v == "" for v in values):
            return True
        return any(_schema_has_empty_enum(value) for value in node.values())
    if isinstance(node, list):
        return any(_schema_has_empty_enum(item) for item in node)
    return False


def _define_required_properties(schema: dict[str, object]) -> None:
    """为 required 列出而 properties 未定义的属性补充不限类型的定义。

    AIStudio2API defineRequiredProperties 移植（原地修改，调用方传深拷贝）。
    """
    required = schema.get("required")
    if isinstance(required, list):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = None
        for item in required:
            if not isinstance(item, str):
                continue
            if properties is not None and item in properties:
                continue
            if properties is None:
                properties = {}
                schema["properties"] = properties
            properties[item] = {}
    for key, child in schema.items():
        if key in ("properties", "$defs", "definitions"):
            if isinstance(child, dict):
                for nested in child.values():
                    if isinstance(nested, dict):
                        _define_required_properties(nested)
        elif key in (
            "items",
            "additionalProperties",
            "additional_properties",
            "not",
            "not_",
        ):
            if isinstance(child, dict):
                _define_required_properties(child)
        elif key in (
            "anyOf",
            "any_of",
            "oneOf",
            "one_of",
            "allOf",
            "all_of",
            "prefixItems",
            "prefix_items",
        ) and isinstance(child, list):
            for nested in child:
                if isinstance(nested, dict):
                    _define_required_properties(nested)


def _tool_schema_shape(value: object) -> dict[str, object]:
    """提取工具参数的层级与类型供 Playground 编码。

    AIStudio2API toolSchemaShape 移植：只保留 wire 可表达的键；const 折叠
    为类型 + 最小/最大值（或 string enum）；仅全字符串 enum 保留；type 为
    null 联合时转 string + nullable + 说明。降级链的兜底编码输入。
    """
    result: dict[str, object] = {}
    if not isinstance(value, dict):
        return result
    for key in (
        "type",
        "format",
        "nullable",
        "description",
        "required",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "propertyOrdering",
    ):
        if key in value:
            result[key] = value[key]
            continue
        for alias in _SHAPE_KEY_ALIASES.get(key, ()):
            if alias in value:
                result[key] = value[alias]
                break
    constant = value.get("const")
    if constant is not None:
        result["description"] = (
            str(result.get("description") or "")
            + "\nThe value must be: "
            + json.dumps(constant, ensure_ascii=False, separators=(",", ":"))
        )
        if isinstance(constant, bool):
            result["type"] = "boolean"
        elif isinstance(constant, (int, float)):
            result["type"] = "integer" if float(constant).is_integer() else "number"
            result["minimum"] = constant
            result["maximum"] = constant
        elif isinstance(constant, str):
            result["type"] = "string"
            result["enum"] = [constant]
        elif isinstance(constant, list):
            result["type"] = "array"
        elif isinstance(constant, dict):
            result["type"] = "object"
    enum_values = value.get("enum")
    if (
        isinstance(enum_values, list)
        and enum_values
        and all(isinstance(item, str) for item in enum_values)
    ):
        result["enum"] = enum_values
    type_field = result.get("type")
    if (isinstance(type_field, str) and type_field.lower() == "null") or (
        isinstance(type_field, list)
        and type_field
        and all(str(item).lower() == "null" for item in type_field)
    ):
        result["type"] = "string"
        result["nullable"] = True
        result["description"] = "The value must be JSON null"
    if constant is None and "const" in value:
        result["type"] = "string"
        result["nullable"] = True
        result["description"] = "The value must be JSON null"
    properties = value.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            str(name): _tool_schema_shape(child) for name, child in properties.items()
        }
    if "items" in value:
        result["items"] = _tool_schema_shape(value["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        branches = value.get(key)
        if isinstance(branches, list):
            result[key] = [_tool_schema_shape(branch) for branch in branches]
    return result


def _compact_schema_json(schema: object) -> str:
    """附加到 description 用的 schema 原文（紧凑 JSON，保序）。"""
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def _encode_tool_parameters(
    schema: dict[str, object], *, strict: bool
) -> tuple[list[object], str | None]:
    """工具参数 schema 编码与降级链（AIStudio2API encodeFunctionDeclaration 移植）。

    返回 ``(wire, 需附加到 description 的 schema 原文 JSON 或 None)``：

    - 先补全 required-but-undeclared 属性再完整编码；
    - 编码失败（递归/非本地/未解析 $ref、复杂度超限）时降级为仅保留
      层级与类型的 shape 编码，不再让整轮请求 400；
    - 编码失败 / strict / wire 无法表达的约束（additionalProperties、
      prefixItems、exclusiveMinimum、propertyNames、$ref、空串 enum）
      时返回原文 JSON，由调用方附加到 description 供模型按 JSON Schema
      理解参数。
    """
    normalized = copy.deepcopy(schema)
    _define_required_properties(normalized)
    encoding_error: ValueError | None = None
    try:
        parameters = encode_schema_to_wire(normalized, include_required=True)
    except ValueError as exc:
        encoding_error = exc
        parameters = encode_schema_to_wire(
            _tool_schema_shape(normalized), include_required=True
        )
    if (
        encoding_error is not None
        or strict
        or _schema_has_lossy_keys(normalized)
        or _schema_has_empty_enum(normalized)
    ):
        return parameters, _compact_schema_json(normalized)
    return parameters, None


def encode_response_schema_to_wire(schema: dict[str, object]) -> list[object]:
    """结构化输出 responseSchema 编码与降级链。

    AIStudio2API encodeResponseSchema 移植：完整编码失败时降级为 shape
    编码；失败或含 wire 无法表达的约束 / 空串 enum 时，把完整 schema
    原文附加到根 description（已附加时幂等）。
    """
    encoding_error: ValueError | None = None
    try:
        wire = encode_schema_to_wire(schema)
    except ValueError as exc:
        encoding_error = exc
        wire = encode_schema_to_wire(_tool_schema_shape(schema))
    if (
        encoding_error is not None
        or _schema_has_lossy_keys(schema, response=True)
        or _schema_has_empty_enum(schema)
    ):
        existing = (
            wire[SchemaIndex.DESCRIPTION]
            if len(wire) > SchemaIndex.DESCRIPTION
            else None
        )
        description = existing if isinstance(existing, str) else ""
        if _RESPONSE_SCHEMA_NOTE not in description:
            wire = JspbArray(wire)
            wire[SchemaIndex.DESCRIPTION] = (
                f"{description}\n{_RESPONSE_SCHEMA_NOTE}{_compact_schema_json(schema)}".strip()
            )
            wire = wire.compact(max_index=SchemaIndex.PROPERTY_ORDERING)
    return wire


def _function_declaration_name(declaration: dict[str, object]) -> str | None:
    """提取声明名（兼容 OpenAI 嵌套 function 形态）。"""
    if isinstance(declaration.get("function"), dict):
        declaration = declaration["function"]  # type: ignore[assignment]
    name = declaration.get("name")
    return str(name) if name else None


# 函数声明 → wire 编码的跨请求记忆化：Agent 客户端每请求原样重发完全相同
# 的工具声明集合（数十个声明 × 每个 KB 级 schema），此前每声明每次都要
# deepcopy + required 补全 + 递归 wire 编码，毫秒级 CPU × 声明数同步跑在
# 事件循环上。以声明紧凑 JSON 为键缓存结果紧凑 JSON，命中经 json.loads
# 返回全新对象（调用方任意突变不污染缓存）；键超限直接重算，防单个巨型
# 声明撑爆缓存内存。编码链是纯函数（sha256 截短名确定性），结果可安全复用。
# 上限 512：MCP 重度 Agent 单客户端即可携带 100+ 工具，多客户端共享网关时
# 128 会触发「逐出一半 → 全量重编码 → 再逐出」抖动；512 条 × 键值均 256KB
# 硬顶的组合上界可控（值与键同量级，实际 KB 级/条）。
_DECLARATION_CACHE_MAX = 512
_DECLARATION_CACHE_MAX_KEY_BYTES = 256 * 1024
_declaration_cache: dict[str, str] = {}


def encode_function_declaration_to_wire(declaration: dict[str, object]) -> list[object]:
    """函数声明 → Playground wire 数组（AIStudio2API encodeFunctionDeclaration 移植）。

    - 函数名超 63 字节截短 + sha256 后缀（Playground 长度限制）；响应侧
      由 ``NormalizedGeminiRequest.function_name_restore`` 换回原名；
    - parameters 走 ``_encode_tool_parameters`` 降级链：编码失败降级为
      shape 而非整体 400，wire 无法表达的约束以完整 JSON Schema 附加到
      description；
    - ``strict``（OpenAI strict mode）透传时同样附加完整 schema；
    - 同一声明（含 strict 标志，它取自声明本身）跨请求结果恒等，走
      ``_declaration_cache`` 记忆化。
    """
    if isinstance(declaration.get("function"), dict):
        declaration = declaration["function"]  # type: ignore[assignment]
    cache_key = ""
    try:
        key = json.dumps(declaration, ensure_ascii=False, separators=(",", ":"))
        if len(key) <= _DECLARATION_CACHE_MAX_KEY_BYTES:
            cache_key = key
    except (TypeError, ValueError):
        # 声明含非 JSON 类型（编程构造的极端入参）：跳过记忆化直接编码
        cache_key = ""
    if cache_key:
        cached = _declaration_cache.get(cache_key)
        if cached is not None:
            # 命中即移到末尾：dict 插入序近似 LRU，与 _TIER_QUOTA_CACHE 同策略
            _declaration_cache[cache_key] = _declaration_cache.pop(cache_key)
            return json.loads(cached)
    name = _function_declaration_name(declaration)
    if not name:
        raise ValueError("functionDeclarations[].name is required")
    description = declaration.get("description")
    if not isinstance(description, str):
        description = ""
    parameters = (
        declaration.get("parameters")
        or declaration.get("parametersJsonSchema")
        or declaration.get("parameters_json_schema")
        or declaration.get("input_schema")
        or declaration.get("parameters_schema")
    )
    wire = JspbArray([_wire_function_name(name)])
    if isinstance(parameters, dict) and parameters:
        parameters_wire, embed_json = _encode_tool_parameters(
            parameters, strict=declaration.get("strict") is True
        )
        if embed_json is not None:
            description = f"{description}\n{_TOOL_ARGUMENTS_SCHEMA_NOTE}{embed_json}"
        wire[2] = parameters_wire
    elif isinstance(parameters, list):
        wire[2] = parameters
    if description:
        wire[1] = description
    result = wire.compact()
    if cache_key:
        if len(_declaration_cache) >= _DECLARATION_CACHE_MAX:
            for _ in range(_DECLARATION_CACHE_MAX // 2):
                _declaration_cache.pop(next(iter(_declaration_cache)), None)
        _declaration_cache[cache_key] = json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        )
    return result


__all__ = [
    "SCHEMA_TYPE_CODES",
    "encode_function_declaration_to_wire",
    "encode_response_schema_to_wire",
    "encode_schema_to_wire",
]
