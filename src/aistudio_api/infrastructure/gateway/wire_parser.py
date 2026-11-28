"""Google Protobuf wire array parser for AI Studio gateway responses.

Reverse-engineered Protobuf-over-JSON array specifications are documented in:
docs/WIRE_SPECIFICATION.md
"""

from __future__ import annotations

import json

from aistudio_api.domain.models import (
    Candidate,
    GeneratedImage,
    ModelOutput,
    ResponsePart,
)
from aistudio_api.infrastructure.gateway.wire_spec import (
    WIRE_FINISH_REASON_CONTINUING,
    WIRE_FINISH_REASON_STOP,
    PartIndex,
)
from aistudio_api.infrastructure.gateway.wire_unicode import validate_json_surrogates
from aistudio_api.infrastructure.utils.common import extract_outer_json

_ROLE_NAMES = frozenset({"model", "user", "system", "assistant"})
_MAX_REPLAY_BYTES = 4 * 1024 * 1024
_MAX_REPLAY_PARTS = 16384

# ==============================================================================
# Google Protobuf-over-JSON Wire Array Specifications (docs/WIRE_SPECIFICATION.md)
# ==============================================================================
# Top-level chunk indexes (WIRE_SPECIFICATION.md §7.1)
WIRE_CHUNK_CANDIDATES_INDEX = 0  # Field 1 candidates container [[candidate, ...]]
WIRE_CHUNK_USAGE_INDEX = 2  # Field 3 usage metadata array
WIRE_RESPONSE_ID_INDEX = 7  # Field 8 response unique ID string

# Candidate Part indexes inside content[0] ([parts, role]) (WIRE_SPECIFICATION.md §3.2)
# 单一真相源：wire_spec.PartIndex（wire_types 编码侧同源）；此处保留既有
# WIRE_PART_*_INDEX 别名以免 wire_codec/解析调用点大改，取值与 PartIndex 严格一致。
WIRE_PART_THOUGHT_LEAD_INDEX = int(
    PartIndex.THOUGHT_LEAD
)  # Field 1 boolean thought flag (fallback)
WIRE_PART_TEXT_INDEX = int(PartIndex.TEXT)  # Field 2 text string
WIRE_PART_INLINE_DATA_INDEX = int(
    PartIndex.INLINE_DATA
)  # Field 3 [mimeType, base64_data]
WIRE_PART_FUNCTION_CALL_INDEX = int(
    PartIndex.FUNCTION_CALL
)  # Field 4 [name, args_struct, call_id]
WIRE_PART_FUNCTION_RESPONSE_INDEX = int(
    PartIndex.FUNCTION_RESPONSE
)  # Field 5 [name, response_struct, call_id]
WIRE_PART_FILE_DATA_INDEX = int(PartIndex.FILE_DATA)  # Field 6 [file_id]
WIRE_PART_EXECUTABLE_CODE_INDEX = int(
    PartIndex.EXECUTABLE_CODE
)  # Field 9 code execution script string
WIRE_PART_CODE_EXECUTION_RESULT_INDEX = int(
    PartIndex.CODE_RESULT
)  # Field 10 code execution output string
WIRE_PART_THOUGHT_FLAG_INDEX = int(
    PartIndex.THOUGHT_FLAG
)  # Field 11 boolean thought flag (primary)
WIRE_PART_FUNCTION_CALL_ALT_INDEX = int(
    PartIndex.FUNCTION_CALL_ALT
)  # Field 11 function call alternative slot
WIRE_PART_FUNCTION_RESPONSE_ALT_INDEX = int(
    PartIndex.FUNCTION_RESPONSE_ALT
)  # Field 12 function response alternative slot
WIRE_PART_THOUGHT_ALT_FLAG_INDEX = int(
    PartIndex.THOUGHT_ALT_FLAG
)  # Field 13 integer 1 thought flag (AI Studio variant)
WIRE_PART_THOUGHT_SIGNATURE_INDEX = int(
    PartIndex.THOUGHT_SIGNATURE
)  # Field 15 thought cryptographic signature token

# UsageMetadata indexes inside chunk[2] (WIRE_SPECIFICATION.md §7.3)
WIRE_USAGE_PROMPT_TOKENS = 0  # Field 1 prompt token count
WIRE_USAGE_VISIBLE_COMPLETION = 1  # Field 2 visible completion token count
WIRE_USAGE_TOTAL_TOKENS = 2  # Field 3 total token count
WIRE_USAGE_CACHED_TOKENS = 3  # Field 4 cached prompt token count
WIRE_USAGE_PROMPT_DETAILS = 4  # Field 5 prompt tokens breakdown structure
WIRE_USAGE_REASONING_TOKENS = 9  # Field 10 thinking/reasoning token count

# Candidate indexes inside chunk[0][0] (WIRE_SPECIFICATION.md §7.2)
WIRE_CANDIDATE_CONTENT = 0  # Field 1 [parts, role]
WIRE_CANDIDATE_FINISH_REASON = 1  # Field 2 finish reason enum code
WIRE_CANDIDATE_FINISH_MESSAGE = 3  # Field 4 finish message string
WIRE_CANDIDATE_SAFETY_RATINGS = 4  # Field 5 safety ratings array


def _looks_like_response_chunk(value: object) -> bool:
    return isinstance(value, list) and len(value) > 0 and isinstance(value[0], list)


def _iter_response_chunks(outer: object) -> list[list[object]]:
    if isinstance(outer, list) and outer:
        if len(outer) == 1 and isinstance(outer[0], list):
            inner = outer[0]
            nested_chunks = [
                item
                for item in inner
                if _looks_like_response_chunk(item) and isinstance(item, list)
            ]
            if nested_chunks:
                return nested_chunks
        top_level_chunks = [
            item
            for item in outer
            if _looks_like_response_chunk(item) and isinstance(item, list)
        ]
        if len(top_level_chunks) > 1:
            return top_level_chunks

    if _looks_like_response_chunk(outer) and isinstance(outer, list):
        return [outer]

    return []


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and (
            stripped.isdigit() or (stripped[0] in "+-" and stripped[1:].isdigit())
        ):
            return int(stripped)
    return None


def parse_response_part(raw_part: object) -> ResponsePart:
    # Plain text has only its two canonical slots, including an explicit empty string.
    if (
        type(raw_part) is list
        and len(raw_part) == 2
        and raw_part[0] is None
        and type(raw_part[1]) is str
    ):
        return ResponsePart(text=raw_part[1], text_present=True)
    if not isinstance(raw_part, list):
        return ResponsePart()

    # A content block [parts, role] or nested list is not a part.
    if len(raw_part) > 0 and isinstance(raw_part[0], list):
        return ResponsePart()

    # Guard against role name in 2-element structure where index 1 is role.
    # Only treat as a content block when index 0 is a parts list or role name;
    # `[None, "model"]` is a valid text part whose text happens to be "model".
    if (
        len(raw_part) == 2
        and isinstance(raw_part[1], str)
        and raw_part[1] in _ROLE_NAMES
    ):
        first = raw_part[0]
        if isinstance(first, list) or (isinstance(first, str) and first in _ROLE_NAMES):
            return ResponsePart()

    thought = False
    if len(raw_part) > WIRE_PART_THOUGHT_FLAG_INDEX and isinstance(
        raw_part[WIRE_PART_THOUGHT_FLAG_INDEX], bool
    ):
        thought = raw_part[WIRE_PART_THOUGHT_FLAG_INDEX]
    elif len(raw_part) > WIRE_PART_THOUGHT_LEAD_INDEX and isinstance(
        raw_part[WIRE_PART_THOUGHT_LEAD_INDEX], bool
    ):
        thought = raw_part[WIRE_PART_THOUGHT_LEAD_INDEX]
    elif (
        len(raw_part) > WIRE_PART_THOUGHT_ALT_FLAG_INDEX
        and raw_part[WIRE_PART_THOUGHT_ALT_FLAG_INDEX] == 1
    ):
        thought = True

    inline_data = None
    if (
        len(raw_part) > WIRE_PART_INLINE_DATA_INDEX
        and isinstance(raw_part[WIRE_PART_INLINE_DATA_INDEX], list)
        and len(raw_part[WIRE_PART_INLINE_DATA_INDEX]) >= 2
    ):
        raw_inline = raw_part[WIRE_PART_INLINE_DATA_INDEX]
        inline_data = (str(raw_inline[0]), str(raw_inline[1]))

    function_call = _coerce_structural_payload(
        raw_part[WIRE_PART_FUNCTION_CALL_INDEX]
        if len(raw_part) > WIRE_PART_FUNCTION_CALL_INDEX
        else None,
        "functionCall",
    )
    if function_call is None and len(raw_part) > WIRE_PART_FUNCTION_CALL_ALT_INDEX:
        function_call = _coerce_structural_payload(
            raw_part[WIRE_PART_FUNCTION_CALL_ALT_INDEX], "functionCall"
        )
    thought_signature = (
        raw_part[WIRE_PART_THOUGHT_SIGNATURE_INDEX]
        if len(raw_part) > WIRE_PART_THOUGHT_SIGNATURE_INDEX
        and isinstance(raw_part[WIRE_PART_THOUGHT_SIGNATURE_INDEX], str)
        else ""
    )
    if function_call is not None:
        if thought_signature:
            function_call["thought_signature"] = thought_signature
        raw = function_call.get("raw")
        if isinstance(raw, list) and len(raw) > 2 and isinstance(raw[2], str):
            function_call["call_id"] = raw[2]

    function_response = _coerce_structural_payload(
        raw_part[WIRE_PART_FUNCTION_RESPONSE_INDEX]
        if len(raw_part) > WIRE_PART_FUNCTION_RESPONSE_INDEX
        else None,
        "functionResponse",
    )
    if (
        function_response is None
        and len(raw_part) > WIRE_PART_FUNCTION_RESPONSE_ALT_INDEX
    ):
        function_response = _coerce_structural_payload(
            raw_part[WIRE_PART_FUNCTION_RESPONSE_ALT_INDEX], "functionResponse"
        )

    executable_code = (
        raw_part[WIRE_PART_EXECUTABLE_CODE_INDEX]
        if len(raw_part) > WIRE_PART_EXECUTABLE_CODE_INDEX
        else None
    )
    code_execution_result = (
        raw_part[WIRE_PART_CODE_EXECUTION_RESULT_INDEX]
        if len(raw_part) > WIRE_PART_CODE_EXECUTION_RESULT_INDEX
        else None
    )

    text = (
        raw_part[WIRE_PART_TEXT_INDEX] if len(raw_part) > WIRE_PART_TEXT_INDEX else None
    )
    return ResponsePart(
        text=text if isinstance(text, str) else "",
        text_present=isinstance(text, str),
        inline_data=inline_data,
        thought=thought,
        function_call=function_call,
        function_response=function_response,
        thought_signature=thought_signature,
        executable_code=executable_code,
        code_execution_result=code_execution_result,
    )


class PositionalTextCall(dict[str, object]):
    """Normalized call fields plus the original positional argument string.

    This provenance is not a JSON field: a dictionary-shaped wire payload
    (including one containing its own `raw` list) cannot claim it.
    """

    __slots__ = ("argument_text",)

    def __init__(self, argument_text: str) -> None:
        super().__init__()
        self.argument_text = argument_text


def _coerce_wire_payload(
    raw_value: object, payload_type: str
) -> dict[str, object] | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, dict):
        dict_payload = dict(raw_value)
        dict_payload.setdefault("type", payload_type)
        return dict_payload
    if isinstance(raw_value, list):
        list_payload: dict[str, object] = (
            PositionalTextCall(raw_value[1])
            if payload_type == "functionCall"
            and len(raw_value) > 1
            and isinstance(raw_value[1], str)
            else {}
        )
        list_payload.update(type=payload_type, raw=raw_value)
        if raw_value and isinstance(raw_value[0], str):
            list_payload["name"] = raw_value[0]
        if len(raw_value) > 1:
            second = raw_value[1]
            if isinstance(second, dict):
                list_payload["args"] = second
            elif isinstance(second, list):
                list_payload["args"] = _decode_wire_struct(second)
            elif isinstance(second, str):
                stripped = second.strip()
                if stripped.startswith(("{", "[")):
                    try:
                        list_payload["args"] = json.loads(second)
                    except json.JSONDecodeError:
                        list_payload["arguments"] = second
                else:
                    list_payload["arguments"] = second
            elif second is not None:
                list_payload["args"] = second
        if len(raw_value) > 2 and isinstance(raw_value[2], str):
            list_payload["call_id"] = raw_value[2]
        return list_payload
    return {"type": payload_type, "raw": raw_value}


def _coerce_structural_payload(
    raw_value: object, payload_type: str
) -> dict[str, object] | None:
    """Only accept structured (list/dict) payloads for functionCall/functionResponse.

    Index 10 is shared with WIRE_PART_THOUGHT_FLAG_INDEX (boolean True); scalar flags
    at index 10/11 must not be misinterpreted as function calls or responses.
    """
    if not isinstance(raw_value, (list, dict)):
        return None
    return _coerce_wire_payload(raw_value, payload_type)


def _is_wire_value(val: object) -> bool:
    if isinstance(val, list) and len(val) > 0:
        return val[0] is None or (val[0] == 0 and len(val) == 1)
    return False


_MAX_WIRE_DEPTH = 100


def _decode_wire_struct(raw_struct: object, depth: int = 0) -> dict[str, object]:
    if isinstance(raw_struct, dict):
        return raw_struct
    if not isinstance(raw_struct, list) or not raw_struct:
        return {}
    if depth >= _MAX_WIRE_DEPTH:
        return {}

    # Single unboxed key-value pair [key, wire_value]
    if len(raw_struct) >= 2 and isinstance(raw_struct[0], str):
        return {raw_struct[0]: _decode_wire_value(raw_struct[1], depth + 1)}
    entries: list[object] = raw_struct
    while (
        len(entries) == 1
        and isinstance(entries[0], list)
        and not (len(entries[0]) >= 2 and isinstance(entries[0][0], str))
    ):
        entries = entries[0]

    result: dict[str, object] = {}
    for item in entries:
        if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], str):
            result[item[0]] = _decode_wire_value(item[1], depth + 1)
    return result


def _decode_wire_list(raw_list: object, depth: int = 0) -> list[object]:
    if not isinstance(raw_list, list) or not raw_list:
        return []
    if depth >= _MAX_WIRE_DEPTH:
        return []

    if (
        len(raw_list) == 1
        and isinstance(raw_list[0], list)
        and not _is_wire_value(raw_list[0])
    ):
        items = raw_list[0]
    else:
        items = raw_list

    return [_decode_wire_value(item, depth + 1) for item in items]


def _decode_wire_value(value: object, depth: int = 0) -> object:
    if not isinstance(value, list):
        return value
    if len(value) == 0:
        return []
    if depth >= _MAX_WIRE_DEPTH:
        return None
    if len(value) == 1 and value[0] == 0:
        return None
    if (
        len(value) >= 2
        and isinstance(value[1], (int, float))
        and not isinstance(value[1], bool)
    ):
        return value[1]
    if len(value) >= 3 and isinstance(value[2], str):
        return value[2]
    if len(value) >= 4 and value[3] is not None:
        if isinstance(value[3], bool):
            return value[3]
        if value[3] in (0, 1):
            return bool(value[3])
    if len(value) >= 5 and value[4] is not None:
        return _decode_wire_struct(value[4], depth + 1)
    if len(value) >= 6 and value[5] is not None:
        return _decode_wire_list(value[5], depth + 1)
    if len(value) >= 3 and value[2] is not None:
        return value[2]
    return [_decode_wire_value(item, depth + 1) for item in value]


def parse_usage_metadata(
    raw_usage: object, *, previous: dict[str, object] | None = None
) -> dict[str, object]:
    """Read one snapshot using the last parsed meter from this attempt, if any."""
    if not isinstance(raw_usage, list):
        return {}
    prompt_tokens = _coerce_int(
        raw_usage[WIRE_USAGE_PROMPT_TOKENS]
        if len(raw_usage) > WIRE_USAGE_PROMPT_TOKENS
        else None
    )
    visible_completion_tokens = _coerce_int(
        raw_usage[WIRE_USAGE_VISIBLE_COMPLETION]
        if len(raw_usage) > WIRE_USAGE_VISIBLE_COMPLETION
        else None
    )
    total_tokens = _coerce_int(
        raw_usage[WIRE_USAGE_TOTAL_TOKENS]
        if len(raw_usage) > WIRE_USAGE_TOTAL_TOKENS
        else None
    )
    cached_tokens = _coerce_int(
        raw_usage[WIRE_USAGE_CACHED_TOKENS]
        if len(raw_usage) > WIRE_USAGE_CACHED_TOKENS
        else None
    )
    reasoning_tokens = _coerce_int(
        raw_usage[WIRE_USAGE_REASONING_TOKENS]
        if len(raw_usage) > WIRE_USAGE_REASONING_TOKENS
        else None
    )
    completion_tokens = visible_completion_tokens
    if isinstance(visible_completion_tokens, int) and isinstance(reasoning_tokens, int):
        completion_tokens = visible_completion_tokens + reasoning_tokens
    elif completion_tokens is None:
        completion_tokens = reasoning_tokens
    if (
        prompt_tokens is None
        and completion_tokens is None
        and total_tokens is None
        and cached_tokens is None
    ):
        return {}
    # Sparse rows may report only output. Reuse the latest prompt and its
    # own optional cache count as a pair; a cache-only revision cannot be
    # attributed to that prompt. An explicit prompt (even zero) stands alone.
    if prompt_tokens is None and previous:
        prior_prompt = previous.get("prompt_tokens")
        if isinstance(prior_prompt, int) and prior_prompt >= 0:
            prompt_tokens = prior_prompt
            cached_tokens = _coerce_int(previous.get("cached_tokens"))
    if (
        total_tokens is None
        and prompt_tokens is not None
        and completion_tokens is not None
    ):
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "prompt_tokens_details": (
            raw_usage[WIRE_USAGE_PROMPT_DETAILS]
            if len(raw_usage) > WIRE_USAGE_PROMPT_DETAILS
            else None
        ),
        "completion_tokens_details": {
            "reasoning_tokens": reasoning_tokens or 0,
            "visible_tokens": visible_completion_tokens or 0,
        },
    }


def parse_chunk_usage(
    chunk: object, *, previous: dict[str, object] | None = None
) -> dict[str, object]:
    if not isinstance(chunk, list):
        return {}
    return parse_usage_metadata(
        chunk[WIRE_CHUNK_USAGE_INDEX] if len(chunk) > WIRE_CHUNK_USAGE_INDEX else None,
        previous=previous,
    )


def parse_response_chunk(chunk: list[object]) -> Candidate:
    candidate = Candidate(ordered_parts=[])
    if (
        not isinstance(chunk, list)
        or not chunk
        or len(chunk) <= WIRE_CHUNK_CANDIDATES_INDEX
    ):
        return candidate

    candidates_container = chunk[WIRE_CHUNK_CANDIDATES_INDEX]
    if not isinstance(candidates_container, list) or not candidates_container:
        return candidate

    raw_candidate = candidates_container[0]
    if not isinstance(raw_candidate, list):
        return candidate

    raw_content = (
        raw_candidate[WIRE_CANDIDATE_CONTENT]
        if len(raw_candidate) > WIRE_CANDIDATE_CONTENT
        else None
    )
    raw_parts = (
        raw_content[0] if isinstance(raw_content, list) and len(raw_content) > 0 else []
    )

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    images: list[GeneratedImage] = []
    reasoning_images: list[GeneratedImage] = []
    function_calls: list[dict[str, object]] = []
    function_responses: list[dict[str, object]] = []
    thought_signature = ""
    code_outputs: list[str] = []

    ordered_parts: list[ResponsePart] = []
    for raw_part in raw_parts if isinstance(raw_parts, list) else []:
        if isinstance(raw_part, list):
            candidate.raw_parts.append(raw_part)
        part = parse_response_part(raw_part)
        if part.inline_data:
            # 上游原始 base64 直接作为主数据源：解码推迟到 GeneratedImage.data
            # 首次被访问，解析热路径不再为无人消费的 bytes 白付全量解码。
            part.image = GeneratedImage(
                mime=str(part.inline_data[0]),
                raw_b64=str(part.inline_data[1]),
                thought_signature=part.thought_signature or "",
            )
            if part.thought:
                reasoning_images.append(part.image)
            else:
                images.append(part.image)
        for code in (part.executable_code, part.code_execution_result):
            if isinstance(code, list) and len(code) > 1 and isinstance(code[1], str):
                code_outputs.append(code[1])
            elif isinstance(code, str) and code:
                code_outputs.append(code)
        if part.function_call:
            function_calls.append(part.function_call)
        if part.function_response:
            function_responses.append(part.function_response)
        if part.thought_signature:
            thought_signature = part.thought_signature
        if part.text:
            if part.thought:
                thinking_parts.append(part.text)
            else:
                text_parts.append(part.text)
        if (
            part.text_present
            or part.image
            or part.function_call
            or part.function_response
            or part.thought_signature
            or part.executable_code is not None
            or part.code_execution_result is not None
        ):
            ordered_parts.append(part)

    candidate.text = "".join(text_parts)
    candidate.thinking = "".join(thinking_parts)
    candidate.images = images
    candidate.reasoning_images = reasoning_images
    candidate.function_calls = function_calls
    candidate.function_responses = function_responses
    candidate.thought_signature = thought_signature
    candidate.code_output = "\n".join(code_outputs)
    candidate.ordered_parts = ordered_parts
    candidate.finish_reason = (
        raw_candidate[WIRE_CANDIDATE_FINISH_REASON]
        if len(raw_candidate) > WIRE_CANDIDATE_FINISH_REASON
        and isinstance(raw_candidate[WIRE_CANDIDATE_FINISH_REASON], int)
        and not isinstance(raw_candidate[WIRE_CANDIDATE_FINISH_REASON], bool)
        else None
    )
    candidate.finish_message = (
        str(raw_candidate[WIRE_CANDIDATE_FINISH_MESSAGE])
        if len(raw_candidate) > WIRE_CANDIDATE_FINISH_MESSAGE
        and isinstance(raw_candidate[WIRE_CANDIDATE_FINISH_MESSAGE], str)
        else ""
    )
    raw_ratings = (
        raw_candidate[WIRE_CANDIDATE_SAFETY_RATINGS]
        if len(raw_candidate) > WIRE_CANDIDATE_SAFETY_RATINGS
        else None
    )
    candidate.safety_ratings = raw_ratings if isinstance(raw_ratings, list) else []
    return candidate


def wire_size_estimate(value: object) -> int:
    """JSON 序列化长度的廉价近似（per-part 热路径调用，避免整树 dumps）。

    回放/轮次预算都是数量级正确的软上限：字符串按字符数、容器按少量
    结构开销累计即可；ModelTurnHistory.store 另有整树 _json 的精确复核。
    """
    if isinstance(value, str):
        return len(value) + 2
    if isinstance(value, dict):
        return sum(len(k) + 4 + wire_size_estimate(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 2 + sum(wire_size_estimate(item) + 1 for item in value)
    if value is None or isinstance(value, bool):
        return 4
    if isinstance(value, int):
        return 8
    if isinstance(value, float):
        return 16
    return 32


def parse_text_output(raw: str) -> ModelOutput:
    # Also cover a definitely unpaired escape in an otherwise truncated
    # document, before the permissive multiline JSON fallback can discard it.
    validate_json_surrogates(raw, allow_eof_pair=True)
    output = ModelOutput(raw_response=raw)

    parts = extract_outer_json(raw)
    if not parts:
        return output

    # 多文档容错：extract_outer_json 在单体 JSON 解析失败时退化为逐行
    # 解析；若上游/中间层按换行分隔下发多个 JSON 文档，逐个提取 chunk
    # 合并，不再只取首个文档而静默丢弃其余内容。单文档路径行为不变。
    chunks: list[list[object]] = []
    for outer in parts:
        chunks.extend(_iter_response_chunks(outer))
    if not chunks:
        return output

    ordered_parts: list[ResponsePart] = []
    merged = Candidate(ordered_parts=ordered_parts)
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    code_output_parts: list[str] = []
    retain_parts = True
    replay_bytes = 0
    for chunk_index, chunk in enumerate(chunks):
        usage = parse_chunk_usage(chunk, previous=output.usage)
        if usage:
            output.usage = usage
        if len(chunk) > WIRE_RESPONSE_ID_INDEX:
            raw_rid = chunk[WIRE_RESPONSE_ID_INDEX]
            if isinstance(raw_rid, str) and raw_rid:
                output.response_id = raw_rid
        parsed = parse_response_chunk(chunk)
        if parsed.ordered_parts:
            for part in parsed.ordered_parts:
                part.wire_chunk = chunk_index
                ordered_parts.append(part)
        if parsed.images or parsed.reasoning_images or parsed.function_responses:
            retain_parts = False
            merged.raw_parts.clear()
        if retain_parts:
            for wire in parsed.raw_parts:
                # 回放预算只需数量级正确的界（4MB 软上限）：廉价估计
                # 替代每 part 整树 dumps，大响应省数倍 CPU。
                replay_bytes += wire_size_estimate(wire)
                if (
                    replay_bytes > _MAX_REPLAY_BYTES
                    or len(merged.raw_parts) >= _MAX_REPLAY_PARTS
                ):
                    merged.raw_parts.clear()
                    retain_parts = False
                    break
                merged.raw_parts.append(wire)
        if parsed.has_content or parsed.thinking or parsed.reasoning_images:
            # A previous STOP cannot certify a later unfinished continuation.
            merged.finish_reason = None
        if parsed.text:
            text_parts.append(parsed.text)
        if parsed.thinking:
            thinking_parts.append(parsed.thinking)
        if parsed.images:
            merged.images.extend(parsed.images)
        if parsed.reasoning_images:
            merged.reasoning_images.extend(parsed.reasoning_images)
        if parsed.function_calls:
            merged.function_calls.extend(parsed.function_calls)
        if parsed.function_responses:
            merged.function_responses.extend(parsed.function_responses)
        if parsed.thought_signature:
            merged.thought_signature = parsed.thought_signature
        if parsed.code_output:
            code_output_parts.append(parsed.code_output)
        if parsed.finish_reason is not None and parsed.finish_reason not in (
            0,
            WIRE_FINISH_REASON_CONTINUING,
        ):
            # 与流式路径（streaming.emit_parsed_chunk）对称：0/22 是占位/
            # 续写中间态编码，不是终止结论；聚合并把它们当终点会把
            # 续写中间态伪装成 FINISH_REASON_UNSPECIFIED 下发给客户端。
            merged.finish_reason = parsed.finish_reason
            merged.tool_interrupted |= parsed.finish_reason != WIRE_FINISH_REASON_STOP
        if parsed.finish_message:
            merged.finish_message = parsed.finish_message
        if parsed.safety_ratings:
            merged.safety_ratings = parsed.safety_ratings

    merged.text = "".join(text_parts)
    merged.thinking = "".join(thinking_parts)
    merged.code_output = "\n".join(code_output_parts)
    output.candidates = [merged]
    return output


__all__ = [
    "WIRE_CANDIDATE_CONTENT",
    "WIRE_CANDIDATE_FINISH_MESSAGE",
    "WIRE_CANDIDATE_FINISH_REASON",
    "WIRE_CANDIDATE_SAFETY_RATINGS",
    "WIRE_CHUNK_CANDIDATES_INDEX",
    "WIRE_CHUNK_USAGE_INDEX",
    "WIRE_PART_CODE_EXECUTION_RESULT_INDEX",
    "WIRE_PART_EXECUTABLE_CODE_INDEX",
    "WIRE_PART_FILE_DATA_INDEX",
    "WIRE_PART_FUNCTION_CALL_ALT_INDEX",
    "WIRE_PART_FUNCTION_CALL_INDEX",
    "WIRE_PART_FUNCTION_RESPONSE_ALT_INDEX",
    "WIRE_PART_FUNCTION_RESPONSE_INDEX",
    "WIRE_PART_INLINE_DATA_INDEX",
    "WIRE_PART_TEXT_INDEX",
    "WIRE_PART_THOUGHT_ALT_FLAG_INDEX",
    "WIRE_PART_THOUGHT_FLAG_INDEX",
    "WIRE_PART_THOUGHT_LEAD_INDEX",
    "WIRE_PART_THOUGHT_SIGNATURE_INDEX",
    "WIRE_RESPONSE_ID_INDEX",
    "WIRE_USAGE_CACHED_TOKENS",
    "WIRE_USAGE_PROMPT_DETAILS",
    "WIRE_USAGE_PROMPT_TOKENS",
    "WIRE_USAGE_REASONING_TOKENS",
    "WIRE_USAGE_TOTAL_TOKENS",
    "WIRE_USAGE_VISIBLE_COMPLETION",
    "PositionalTextCall",
    "ResponsePart",
    "parse_chunk_usage",
    "parse_response_chunk",
    "parse_response_part",
    "parse_text_output",
    "parse_usage_metadata",
    "wire_size_estimate",
]
