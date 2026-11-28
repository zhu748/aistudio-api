"""OpenAI Responses API 协议兼容转换层。

在既有 Gemini 管线（复用 ``handle_gemini_generate_content`` 的账号轮换 /
403 阶梯重试 / 抗截断传输）之上，实现 ``POST /v1/responses``：

- 请求侧：Responses 请求（``input`` items + ``instructions`` + ``reasoning``
  / ``text`` 配置 + function tools）→ Gemini 请求模型
- 响应侧：Gemini 响应 / SSE 帧 → Responses ``response`` 对象与
  语义化流式事件（``response.created`` … ``response.completed``）

事件形状与 openai-python SDK（3.50.0 ``types/responses/*``）逐字段对齐：
``sequence_number`` 从 0 起每个事件 +1；流以 ``response.completed`` /
``response.failed`` / ``response.incomplete`` 终结（不发 ``[DONE]``，SDK
以终结事件收束，兼容两者但语义事件为准）。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterable, Callable, Iterator
from dataclasses import dataclass

import httpx

from aistudio_api.api.response_models import (
    GeminiGenerateContentResponse,
)
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.api.schemas_compat import OpenAIResponsesRequest
from aistudio_api.application.chat_service import _normalize_gemini_tool_config
from aistudio_api.application.compat_openai import (
    _history_refusal,
    _openai_history_call,
)
from aistudio_api.application.compat_result_owners import index_call_name, result_part
from aistudio_api.application.compat_service import (
    _as_dict,
    _as_int,
    _as_str,
    _convert_items_concurrently,
    _file_block_to_part,
    _iter_response_parts,
    _media_part_from_url,
    _openai_tool_choice_to_config,
    _openai_tools_to_gemini,
    _signature_extra,
    _stream_adapter_lifecycle,
    normalize_reasoning_effort_word,
    resolve_compat_model,
)
from aistudio_api.application.compat_shared import (
    _DATA_URL_RE,
    _code_text,
    _dropped_part_reason,
    _has_inline_file_source,
    _is_signed_empty_thought,
    _parse_data_url,
    _read_signature_with_source,
    decode_signature_envelope,
    encode_signature_envelope,
)
from aistudio_api.application.media_session import has_remote_media, media_session
from aistudio_api.application.responses_delivery import (
    PartStage,
    ResponsesStreamDelivery,
)
from aistudio_api.application.stream_utils import part_payload_signature
from aistudio_api.domain.tool_signatures import (
    remember_function_signature as _remember_function_signature,
)
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.signature_trace import (
    get_signature_trace,
    set_trace_protocol,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.responses")
_STANDARD_BASE64 = re.compile(r"[A-Za-z0-9+/]++={0,2}")
_UNFINISHED_TOOL_ITEM_STATUSES = ("incomplete", "in_progress")

# Responses reasoning.effort → Gemini reasoningEffort：统一走
# normalize_reasoning_effort_word（与 chat completions 侧同一套词表，
# 额外获得 off/disabled 等别名与分隔符变体容错）。

# Gemini finishReason → Responses status / incomplete_details.reason
_FINISH_TO_INCOMPLETE_REASON: dict[str, str] = {
    "MAX_TOKENS": "max_output_tokens",
    # OTHER = streaming.py 检测到流截断（无上游 finishReason）时下发。
    # 不映射则伪装成 completed，SDK 把残缺回答当完整答案。
    "OTHER": "max_output_tokens",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "IMAGE_OTHER": "content_filter",
    "IMAGE_RECITATION": "content_filter",
    "LANGUAGE": "content_filter",
    "MALFORMED_FUNCTION_CALL": "content_filter",
    "UNEXPECTED_TOOL_CALL": "content_filter",
    "TOO_MANY_TOOL_CALLS": "content_filter",
    "NO_IMAGE": "content_filter",
    "MISSING_THOUGHT_SIGNATURE": "content_filter",
    "FINISH_REASON_UNSPECIFIED": "max_output_tokens",
}


def _response_id() -> str:
    return f"resp_{secrets.token_hex(24)}"


def _item_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _call_id() -> str:
    return f"call_{secrets.token_hex(12)}"


# ---------------------------------------------------------------------------
# 请求侧：Responses → Gemini
# ---------------------------------------------------------------------------


def _reject_state_reference(value: object, field: str) -> None:
    if isinstance(value, str) and value and not value.isspace():
        raise ValueError(
            f"{field} is not supported; send the complete conversation as inline input items"
        )


def _has_image_url_source(value: object) -> bool:
    url = _as_str(value).strip()
    if url[:8].lower().startswith(("http://", "https://")):
        # Use the downloader's URL parser, not a DNS lookup or a media client.
        try:
            return httpx.URL(url).is_absolute_url
        except (httpx.InvalidURL, UnicodeError):
            return False
    return _DATA_URL_RE.match(url) is not None


def _check_user_media(content: object, input_index: int) -> None:
    """Reject unsupported user media before any message can start media I/O."""
    if not isinstance(content, list):
        return
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "input_audio":
            # The SDK can send this block outside its typed message union.
            # Reject supplied data without decoding it or validating a format.
            data = _as_str(_as_dict(part.get("input_audio")).get("data"))
            if data and not data.isspace():
                raise ValueError(
                    f"input[{input_index}].content[{index}].input_audio is not supported; "
                    "use input_file.file_data with an inline base64 data URL"
                )
            continue
        if kind not in ("input_image", "input_file"):
            continue
        field = "file_id"
        if not _as_str(part.get(field)).strip():
            if kind != "input_file" or not _as_str(part.get("file_url")).strip():
                continue
            field = "file_url"

        # Match the ordinary converters' source shapes without charging the same
        # inline bytes twice or attempting to resolve either reference.
        if kind == "input_image":
            if _has_image_url_source(part.get("image_url")):
                continue
            alternative = (
                "input_image.image_url with an HTTP(S) URL or an inline base64 data URL"
            )
        else:
            if _has_inline_file_source(part.get("file_data")):
                continue
            alternative = "input_file.file_data with an inline base64 data URL"
        raise ValueError(
            f"input[{input_index}].content[{index}].{field} is not supported for {kind}; use {alternative}"
        )


async def _parts_from_input_content(
    content: object, *, assistant: bool = False
) -> list[GeminiPart]:
    """Responses input content（str 或 content part 数组）→ Gemini parts。

    支持 ``input_text`` / ``input_image``（image_url: HTTP(S) 或 data URL）
    / ``input_file``（file_data: data URL）；用户消息中无法解析的文件引用及带数据的
    ``input_audio`` 已预检；其余不支持的类型忽略并打日志。
    """
    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
    if len(content) > 1 and has_remote_media(content):
        return await _convert_items_concurrently(
            content, lambda items: _parts_from_input_content(items, assistant=assistant)
        )
    parts: list[GeminiPart] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        part_type = item.get("type")
        if (
            part_type == "input_text"
            or part_type == "text"
            # assistant 历史回传的文本块类型是 output_text（SDK 序列化
            # 历史即此形态），漏掉它会把上一轮回复整段清空
            or part_type == "output_text"
        ):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(GeminiPart(text=text))
        elif assistant and part_type == "refusal":
            if explanation := _history_refusal(item.get("refusal")):
                parts.append(GeminiPart(text=explanation))
        elif part_type == "input_image":
            url = _as_str(item.get("image_url")).strip()
            if url:
                parts.append(await _media_part_from_url(url))
        elif part_type == "input_file":
            file_data = _as_str(item.get("file_data"))
            part = _file_block_to_part(file_data, item.get("filename"))
            if part is not None:
                parts.append(part)
        else:
            logger.debug("忽略不支持的 Responses content 类型: %s", part_type)
    return parts


def _content_text(content: object) -> str:
    """Responses system/developer content（str 或块数组）→ 纯文本。

    块类型兼容 ``input_text`` / ``text`` / ``output_text``（SDK 序列化
    历史时 assistant 块是 output_text，输入侧是 input_text）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            _as_str(part.get("text"))
            for part in content
            if isinstance(part, dict)
            and part.get("type") in ("input_text", "text", "output_text")
        ]
        return "\n\n".join(t for t in texts if t)
    return ""


def _tool_output_payload(
    output: object,
) -> tuple[dict[str, object], list[GeminiPart]]:
    """Keep text and bounded local media attached to their own function result."""
    if isinstance(output, str):
        return {"result": output}, []
    texts: list[str] = []
    media: list[GeminiPart] = []
    if not isinstance(output, list):
        return {"result": ""}, media
    for index, part in enumerate(output):
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("output_text", "input_text", "text"):
            texts.append(_as_str(part.get("text")))
        elif part_type in ("input_image", "input_file"):
            field = "image_url" if part_type == "input_image" else "file_data"
            path = f"function_call_output.output[{index}].{field}"
            # The ordinary image helper also fetches HTTP URLs. Tool output
            # accepts only self-contained attachments, with no extra I/O.
            reference = _as_str(part.get(field)).strip()
            if reference[:5].lower() != "data:":
                texts.append(
                    "[image omitted]"
                    if part_type == "input_image"
                    else "[file omitted]"
                )
                continue
            parsed = _parse_data_url(reference)
            if parsed is None:
                raise ValueError(f"{path} must be an inline base64 data URL")
            mime, data = parsed
            # Bound and account first; check alphabet/padding without decoding
            # another copy of the full attachment.
            if len(data) % 4 or _STANDARD_BASE64.fullmatch(data) is None:
                raise ValueError(f"{path} contains invalid base64 data")
            if not mime.isascii() or (
                part_type == "input_image" and not mime.lower().startswith("image/")
            ):
                raise ValueError(f"{path} has an invalid media type")
            filename = (
                _as_str(part.get("filename")).strip()
                if part_type == "input_file"
                else ""
            )
            media.append(
                GeminiPart(
                    inlineData=GeminiInlineData(
                        mimeType=mime, data=data, filename=filename or None
                    )
                )
            )
    return {"result": "\n".join(t for t in texts if t)}, media


def _reasoning_summary_text(item: dict[str, object]) -> str:
    """Responses reasoning item 的 summary 文本拼接（summary_text 条目）。"""
    summary = item.get("summary")
    if not isinstance(summary, list):
        return ""
    texts: list[str] = []
    for entry in summary:
        if isinstance(entry, dict):
            text = _as_str(entry.get("text"))
            if text:
                texts.append(text)
    return "".join(texts)


def _is_explicit_empty_summary(item: dict[str, object]) -> bool:
    summary = item.get("summary")
    if not isinstance(summary, list) or len(summary) != 1:
        return False
    entry = _as_dict(summary[0])
    return entry.get("type") == "summary_text" and entry.get("text") == ""


def _responses_gemini_tools(
    tools: list[dict[str, object]] | None, choice: str | dict[str, object] | None
) -> tuple[list[GeminiTool] | None, dict[str, object] | None]:
    return _openai_tools_to_gemini(tools), _openai_tool_choice_to_config(choice)


@dataclass(frozen=True, slots=True)
class ResponsesToolMetadata:
    """Public function catalog and effective choice, not a wire-name or execution policy."""

    tools: list[dict[str, object]]
    choice: str | dict[str, object]

    @classmethod
    def from_gemini(
        cls, tools: list[GeminiTool] | None, config: dict[str, object] | None
    ) -> ResponsesToolMetadata:
        # Start from the declarations accepted by the request converter, before
        # wire aliases, schema fallbacks, or model-specific builtins are added.
        public_tools: list[dict[str, object]] = [
            {
                "type": "function",
                "name": declaration["name"],
                "description": declaration.get("description"),
                "parameters": declaration.get("parameters"),
                # Strict schemas can guide the model but are not enforced.
                "strict": False if "strict" in declaration else None,
            }
            for tool in tools or []
            for declaration in tool.functionDeclarations or []
        ]
        _, disabled, allowed_names = _normalize_gemini_tool_config(config)
        choice: str | dict[str, object] = "auto"
        if disabled:
            choice = "none"
        elif allowed_names:
            declared = {tool["name"] for tool in public_tools}
            selected = [
                {"type": "function", "name": name}
                for name in dict.fromkeys(allowed_names)
                if name in declared
            ]
            if selected:
                # A named/required choice narrows declarations too, but cannot
                # require even one call on this transport.
                choice = {"type": "allowed_tools", "mode": "auto", "tools": selected}
            elif tools is not None:
                # An explicit catalog had no matches. "auto" would instead
                # advertise every declared function; an empty allowlist is not
                # a usable choice. With tools omitted, provider defaults may apply.
                choice = "none"
        return cls(public_tools, choice)

    @classmethod
    def before_conversion(
        cls,
        tools: list[dict[str, object]] | None,
        choice: str | dict[str, object] | None,
    ) -> ResponsesToolMetadata:
        """Preparation failed before the normal converter could return any fields."""
        try:
            return cls.from_gemini(*_responses_gemini_tools(tools, choice))
        except (TypeError, ValueError):
            # For example, a rejected choice must not replace the original
            # preparation error with a second exception after headers are sent.
            return cls([], "auto")


@media_session()
async def responses_to_gemini_request(
    req: OpenAIResponsesRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """OpenAI Responses 请求 → (Gemini 请求, 解析后的模型名)。"""
    trace = set_trace_protocol("responses")
    resolved_model = resolve_compat_model(req.model, channel="openai")
    if req.previous_response_id:
        raise ValueError(
            "previous_response_id is not supported; send the complete conversation in input"
        )
    if isinstance(req.conversation, dict):
        _reject_state_reference(req.conversation.get("id"), "conversation.id")
    else:
        _reject_state_reference(req.conversation, "conversation")
    if req.parallel_tool_calls is False:
        # 该传输不支持强制串行；兼容层接受并告警（与 Chat Completions 一致）。
        logger.warning("parallel_tool_calls=false 无法在上游强制串行，已忽略该提示")
    contents: list[GeminiContent] = []
    system_texts: list[str] = [req.instructions] if req.instructions else []
    tool_name_by_call_id: dict[str, str | None] = {}

    # Check unsupported history before converting any earlier media.
    # Index call ownership in the same pass because a result may precede its call.
    replayable_call_ids: set[str] = set()
    unfinished_call_ids: set[str] = set()
    for index, item in enumerate(req.input if isinstance(req.input, list) else []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        # The SDK's ItemReference discriminator is optional and nullable.
        # With no discriminator, match only its exact shape, not IDs on inline items.
        if item_type == "item_reference" or (
            item_type is None and "id" in item and item.keys() <= {"id", "type"}
        ):
            _reject_state_reference(
                item.get("id"), f"input[{index}].id (item_reference)"
            )
        if (
            item_type in (None, "message")
            and _as_str(item.get("role")).strip().lower() == "user"
        ):
            _check_user_media(item.get("content"), index)
        if item_type in ("custom_tool_call", "custom_tool_call_output"):
            raise ValueError(
                f"input[{index}].type ({item_type}) is not supported; "
                "use function_call and function_call_output items or send the call and result as plain text"
            )
        if item_type != "function_call":
            continue
        call_id = _as_str(item.get("call_id"))
        if not call_id:
            continue
        if item.get("status") in _UNFINISHED_TOOL_ITEM_STATUSES:
            unfinished_call_ids.add(call_id)
            continue
        replayable_call_ids.add(call_id)
        index_call_name(tool_name_by_call_id, call_id, _as_str(item.get("name")))
    unfinished_call_ids.difference_update(replayable_call_ids)

    if req.input is None and not req.instructions:
        raise ValueError("input is required when instructions is empty")

    if isinstance(req.input, str):
        if req.input:
            contents.append(
                GeminiContent(role="user", parts=[GeminiPart(text=req.input)])
            )
    else:
        for item in req.input or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message" or item_type is None:
                # EasyInputMessage（type 省略时按 message 处理）
                role = _as_str(item.get("role")).strip().lower()
                if role in ("system", "developer"):
                    # Preserve instruction priority instead of lowering it to a user turn.
                    text = _content_text(item.get("content"))
                    if text:
                        system_texts.append(text)
                    continue
                parts = await _parts_from_input_content(
                    item.get("content"), assistant=role == "assistant"
                )
                if role == "assistant":
                    contents.append(
                        GeminiContent(
                            role="model", parts=parts or [GeminiPart(text="")]
                        )
                    )
                else:
                    contents.append(
                        GeminiContent(role="user", parts=parts or [GeminiPart(text="")])
                    )
            elif item_type == "function_call":
                if item.get("status") in _UNFINISHED_TOOL_ITEM_STATUSES:
                    continue
                call_id = _as_str(item.get("call_id"))
                sig, sig_source = _read_signature_with_source(item)
                fc_part = _openai_history_call(
                    item.get("name"),
                    item.get("arguments"),
                    call_id,
                    sig,
                    sig_source,
                    trace,
                    responses=True,
                )
                contents.append(GeminiContent(role="model", parts=[fc_part]))
            elif item_type == "function_call_output":
                call_id = _as_str(item.get("call_id"))
                # Even a completed call may have a draft before its final output.
                if (
                    call_id in unfinished_call_ids
                    or item.get("status") in _UNFINISHED_TOOL_ITEM_STATUSES
                ):
                    continue
                payload, media = _tool_output_payload(item.get("output"))
                contents.append(
                    GeminiContent(
                        role="user",
                        parts=[
                            result_part(
                                tool_name_by_call_id,
                                GeminiFunctionResponse(
                                    name="unknown",
                                    response=payload,
                                    id=call_id or None,
                                    parts=media or None,
                                ),
                            )
                        ],
                    )
                )
            elif item_type == "reasoning":
                # Preserve reasoning summary history and read echoed signatures from
                # extra_content or our self-identifying encrypted_content envelope.
                summary_text = _reasoning_summary_text(item)
                signature, sig_source = _read_signature_with_source(item)
                if not signature:
                    raw_encrypted = item.get("encrypted_content")
                    if isinstance(raw_encrypted, str) and raw_encrypted:
                        decoded = decode_signature_envelope(raw_encrypted)
                        if decoded:
                            signature = decoded
                            sig_source = "client_encrypted_content"
                        elif trace is not None:
                            trace.record_client_drop(
                                carrier="thought",
                                raw_signature=raw_encrypted,
                                source="client_encrypted_content",
                                note="foreign_encrypted_content_ignored",
                            )
                if not summary_text and not (
                    _is_explicit_empty_summary(item)
                    and _is_signed_empty_thought(summary_text, signature)
                ):
                    continue
                reasoning_part = GeminiPart(
                    text=summary_text,
                    thought=True,
                    thoughtSignature=signature,
                )
                if trace is not None and sig_source:
                    trace.tag_inbound_source(reasoning_part, sig_source)
                contents.append(GeminiContent(role="model", parts=[reasoning_part]))
            else:
                logger.debug("忽略不支持的 Responses input item 类型: %s", item_type)

    if not contents:
        raise ValueError("input must contain at least one message")
    generation_config: dict[str, object] = {}
    if req.temperature is not None:
        generation_config["temperature"] = req.temperature
    if req.top_p is not None:
        generation_config["topP"] = req.top_p
    if req.max_output_tokens is not None:
        generation_config["maxOutputTokens"] = req.max_output_tokens
    if req.reasoning is not None:
        raw_effort = _as_str(req.reasoning.get("effort"))
        effort_word = normalize_reasoning_effort_word(raw_effort)
        if effort_word is not None:
            generation_config["reasoningEffort"] = effort_word
        elif raw_effort.strip():
            logger.warning("忽略无法识别的 reasoning.effort: %r", raw_effort)
        elif req.reasoning.get("exclude") is True:
            # OpenRouter 扩展：reasoning.exclude=true 表示不回传思维链。
            # 上游无法关闭思考输出透传，忽略之（记录便于排查）。
            logger.debug("忽略 reasoning.exclude（上游不支持关闭思考回传）")
    if req.text is not None:
        fmt = _as_dict(req.text.get("format"))
        fmt_type = fmt.get("type")
        if fmt_type == "json_object":
            generation_config["responseMimeType"] = "application/json"
        elif fmt_type == "json_schema":
            generation_config["responseMimeType"] = "application/json"
            schema = _as_dict(fmt.get("schema"))
            if schema:
                generation_config["responseSchema"] = schema

    tools, tool_config = _responses_gemini_tools(req.tools, req.tool_choice)
    gemini_req = GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=(
            GeminiContent(
                role="user", parts=[GeminiPart(text="\n\n".join(system_texts))]
            )
            if system_texts
            else None
        ),
        tools=tools,
        toolConfig=tool_config,
        generationConfig=(
            GeminiGenerationConfig.model_validate(generation_config)
            if generation_config
            else None
        ),
    )
    return gemini_req, resolved_model


# ---------------------------------------------------------------------------
# 响应侧：Gemini → Responses 对象
# ---------------------------------------------------------------------------


class _ResponsesOutputBuilder:
    """Build separate output items in the order of consecutive Part runs."""

    def __init__(self, *, include_encrypted_reasoning: bool = False) -> None:
        self.output: list[dict[str, object]] = []
        self._include_encrypted_reasoning = include_encrypted_reasoning
        self._kind = ""
        self._buffer: list[str] = []
        self._signature = ""

    def _flush(self, status: str = "completed") -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        if self._kind == "reasoning":
            item: dict[str, object] = {
                **_signature_extra(self._signature),
                "id": _item_id("rs"),
                "type": "reasoning",
                "status": status,
                "summary": [{"type": "summary_text", "text": text}],
            }
            if self._include_encrypted_reasoning and self._signature:
                item["encrypted_content"] = encode_signature_envelope(self._signature)
            self.output.append(item)
            if self._signature and (trace := get_signature_trace()) is not None:
                trace.record_client_out_sig(
                    carrier="thought",
                    wire_field=(
                        "extra_content+encrypted_content"
                        if self._include_encrypted_reasoning
                        else "extra_content"
                    ),
                    signature=self._signature,
                )
        else:
            self.output.append(
                {
                    "id": _item_id("msg"),
                    "type": "message",
                    "role": "assistant",
                    "status": status,
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
            )
        self._buffer.clear()

    def _append(self, kind: str, text: str, signature: str = "") -> None:
        if self._kind != kind or self._signature or signature:
            self._flush()
        self._kind = kind
        self._signature = signature
        self._buffer.append(text)

    def add_part(self, part: object, *, allow_tools: bool) -> None:
        text = getattr(part, "text", None)
        function_call = getattr(part, "functionCall", None)
        raw_sig = _as_str(getattr(part, "thoughtSignature", None))
        emitted_sig = False
        if isinstance(text, str):
            if getattr(part, "thought", None) is True:
                signature = part_payload_signature(part, "text")
                if text or _is_signed_empty_thought(text, signature):
                    self._append("reasoning", text, signature)
                    emitted_sig = bool(signature)
            elif text:
                self._append("message", text)
        inline = getattr(part, "inlineData", None)
        if inline is not None:
            self._append(
                "message", f"\n\n![image](data:{inline.mimeType};base64,{inline.data})"
            )
        if code := _code_text(
            getattr(part, "executableCode", None),
            getattr(part, "codeExecutionResult", None),
        ):
            self._append("message", code)
        if function_call is not None and allow_tools:
            self._flush()
            signature = part_payload_signature(part, "functionCall")
            call_id = function_call.id or _call_id()
            args = function_call.args if function_call.args is not None else {}
            if function_call.id is None:
                _remember_function_signature(
                    call_id, function_call.name, args, signature
                )
            self.output.append(
                {
                    **_signature_extra(signature),
                    "id": _item_id("fc"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": function_call.name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                }
            )
            if signature:
                emitted_sig = True
                if (trace := get_signature_trace()) is not None:
                    trace.record_client_out_sig(
                        carrier="function_call",
                        wire_field="extra_content",
                        signature=signature,
                        call_id=call_id,
                        call_name=function_call.name,
                    )
        if raw_sig and not emitted_sig and (trace := get_signature_trace()) is not None:
            carrier, reason = _dropped_part_reason(part)
            trace.record_client_out_drop(
                carrier=carrier,
                signature=raw_sig,
                reason=reason,
                call_id=_as_str(getattr(function_call, "id", None))
                if function_call is not None
                else "",
                call_name=_as_str(getattr(function_call, "name", None))
                if function_call is not None
                else "",
            )

    def build_output(self, status: str) -> list[dict[str, object]]:
        # Like SSE, a run closed by an item boundary is already completed.
        # The terminal status applies only to the run still open at the end.
        self._flush(status)
        return self.output


def _base_response(
    response_id: str,
    requested_model: str,
    *,
    status: str,
    output: list[dict[str, object]],
    usage: dict[str, int] | None,
    incomplete_reason: str | None = None,
    created_at: int | None = None,
    tool_metadata: ResponsesToolMetadata | None = None,
) -> dict[str, object]:
    # tools / tool_choice / parallel_tool_calls 为 openai-python 类型模型
    # 必填字段：流式宽松构造不报错，但 model_validate 严格校验的客户端
    # （pydantic / 第三方 SDK）会直接 ValidationError。
    payload: dict[str, object] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at if created_at is not None else int(time.time()),
        "status": status,
        "model": requested_model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": tool_metadata.choice if tool_metadata is not None else "auto",
        "tools": tool_metadata.tools if tool_metadata is not None else [],
    }
    if incomplete_reason is not None:
        payload["incomplete_details"] = {"reason": incomplete_reason}
    if usage is not None:
        payload["usage"] = {
            "input_tokens": usage.get("prompt", 0),
            # cache_write_tokens 为 InputTokensDetails 必填（cached_tokens
            # 命名与 SDK 一致）
            "input_tokens_details": {
                "cached_tokens": usage.get("cached", 0),
                "cache_write_tokens": 0,
            },
            "output_tokens": usage.get("completion", 0),
            "output_tokens_details": {"reasoning_tokens": usage.get("reasoning", 0)},
            "total_tokens": usage.get("total", 0),
        }
    return payload


def _usage_payload_from_gemini(usage_metadata: object) -> dict[str, int]:
    from aistudio_api.application.compat_service import _usage_from_gemini

    return _usage_from_gemini(usage_metadata)


def _incomplete_reason(finish_reason: str | None) -> str | None:
    if finish_reason == "STOP":
        return None
    return _FINISH_TO_INCOMPLETE_REASON.get(
        finish_reason or "OTHER", "max_output_tokens"
    )


def build_responses_object(
    response: GeminiGenerateContentResponse,
    requested_model: str,
    *,
    include_encrypted_reasoning: bool = False,
    tool_metadata: ResponsesToolMetadata | None = None,
) -> dict[str, object]:
    """Gemini 完整响应 → Responses ``response`` 对象（非流式）。"""
    finish_reason_raw: str | None = None
    candidates = response.candidates or []
    if candidates:
        finish_reason_raw = candidates[0].finishReason

    builder = _ResponsesOutputBuilder(
        include_encrypted_reasoning=include_encrypted_reasoning
    )
    builder_allow_tools = finish_reason_raw == "STOP"
    for part in _iter_response_parts(response):
        builder.add_part(part, allow_tools=builder_allow_tools)

    incomplete_reason = _incomplete_reason(finish_reason_raw)
    status = "incomplete" if incomplete_reason else "completed"
    output = builder.build_output(status)
    if (trace := get_signature_trace()) is not None:
        trace.emit_inbound_log()
    usage = _usage_payload_from_gemini(response.usageMetadata)
    return _base_response(
        _response_id(),
        requested_model,
        status=status,
        output=output,
        usage=usage,
        incomplete_reason=incomplete_reason,
        tool_metadata=tool_metadata,
    )


# ---------------------------------------------------------------------------
# 流式适配：Gemini SSE → Responses 语义事件流
# ---------------------------------------------------------------------------


class _ResponsesStreamEmitter:
    """Responses SSE 事件发射器（事件形状对齐 openai-python 3.50.0）。"""

    def __init__(
        self,
        response_id: str,
        requested_model: str,
        delivery: ResponsesStreamDelivery | None = None,
        *,
        include_encrypted_reasoning: bool = False,
        tool_metadata: ResponsesToolMetadata | None = None,
    ) -> None:
        self.response_id = response_id
        self.requested_model = requested_model
        self.delivery = delivery
        self.tool_metadata = tool_metadata
        self.include_encrypted_reasoning = include_encrypted_reasoning
        self.created_at = int(time.time())
        self.seq = -1  # 首事件自增后为 0，与 SDK 从 0 递增一致
        self.output_index = -1
        # 各 item 记住自己的 output_index：关闭事件与 delta 事件必须报
        # item 自己的 index，不能引用共享计数器的当前值（交错流中当前值
        # 已指向后打开的 item，SDK 流累积器按 index 对账会错位）
        self.reasoning_index: int | None = None
        self.message_index: int | None = None
        # Items open serially: output_index is the stable position in this list.
        self.item_order: list[dict[str, object]] = []
        self.reasoning_item_id: str | None = None
        self.message_item_id: str | None = None
        self.finish_reason_raw: str | None = None
        self.usage: dict[str, int] | None = None
        self.saw_error = False
        # Only a signature attached to this reasoning Part can be emitted here.
        self.thinking_signature: str | None = None
        # 累积文本（output_text.done / completed 快照需要完整文本）
        self._text_accumulated: list[str] = []
        self._reasoning_accumulated: list[str] = []

    def sse(self, event_type: str, payload: dict[str, object]) -> str:
        self.seq += 1
        frame = {"type": event_type, "sequence_number": self.seq, **payload}
        # separators：SSE 每帧省去 ", " 与 ": " 的空格（约 2-4% 字节/分配）
        encoded = (
            f"event: {event_type}\ndata: "
            + json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
            + "\n\n"
        )
        if self.delivery is not None and isinstance(delta := payload.get("delta"), str):
            self.delivery.encoded_delta(delta)
        return encoded

    def _response_snapshot(
        self,
        *,
        status: str,
        output: list[dict[str, object]] | None = None,
        incomplete_reason: str | None = None,
    ) -> dict[str, object]:
        return _base_response(
            self.response_id,
            self.requested_model,
            status=status,
            output=output or [],
            usage=self.usage,
            incomplete_reason=incomplete_reason,
            created_at=self.created_at,
            tool_metadata=self.tool_metadata,
        )

    def created(self) -> str:
        return self.sse(
            "response.created",
            {"response": self._response_snapshot(status="in_progress")},
        )

    def in_progress(self) -> str:
        return self.sse(
            "response.in_progress",
            {"response": self._response_snapshot(status="in_progress")},
        )

    def _open_reasoning(self) -> list[str]:
        """打开 reasoning item（幂等）：output_item.added + summary part。"""
        if self.reasoning_item_id is not None:
            return []
        self.output_index += 1
        self.reasoning_index = self.output_index
        self.reasoning_item_id = _item_id("rs")
        self.item_order.append(
            {
                "id": self.reasoning_item_id,
                "type": "reasoning",
                "status": "in_progress",
                "summary": [],
            }
        )
        return [
            self.sse(
                "response.output_item.added",
                {
                    "output_index": self.reasoning_index,
                    "item": {
                        "id": self.reasoning_item_id,
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                    },
                },
            ),
            self.sse(
                "response.reasoning_summary_part.added",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
            ),
        ]

    def reasoning_delta(self, text: str) -> list[str]:
        events = self._open_reasoning()
        if not text:
            return events
        self._reasoning_accumulated.append(text)
        return [
            *events,
            self.sse(
                "response.reasoning_summary_text.delta",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "delta": text,
                },
            ),
        ]

    def _open_message(self) -> list[str]:
        """打开 assistant message item（幂等）：output_item.added + content part。"""
        if self.message_item_id is not None:
            return []
        self.output_index += 1
        self.message_index = self.output_index
        self.message_item_id = _item_id("msg")
        self.item_order.append(
            {
                "id": self.message_item_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
        )
        return [
            self.sse(
                "response.output_item.added",
                {
                    "output_index": self.message_index,
                    "item": {
                        "id": self.message_item_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            self.sse(
                "response.content_part.added",
                {
                    "item_id": self.message_item_id,
                    "output_index": self.message_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def text_delta(self, text: str) -> list[str]:
        events = self._open_message()
        self._text_accumulated.append(text)
        events.append(
            self.sse(
                "response.output_text.delta",
                {
                    "item_id": self.message_item_id,
                    "output_index": self.message_index,
                    "content_index": 0,
                    "delta": text,
                    "logprobs": [],
                },
            )
        )
        return events

    def boundary(self, kind: str, signature: str = "") -> Iterator[str]:
        # Never place text/reasoning on both sides of a function call inside
        # the same output item. A signed reasoning Part also owns its own item.
        if self.reasoning_item_id and (
            kind != "reasoning" or signature or self.thinking_signature
        ):
            yield from self._close_reasoning("".join(self._reasoning_accumulated))
            self.reasoning_item_id = None
            self.reasoning_index = None
            self._reasoning_accumulated.clear()
            self.thinking_signature = None
        if self.message_item_id and kind != "message":
            yield from self._close_message("".join(self._text_accumulated))
            self.message_item_id = None
            self.message_index = None
            self._text_accumulated.clear()
        if kind == "reasoning":
            self.thinking_signature = signature

    def function_call(
        self, call_id: str, name: str, args: object, signature: str = ""
    ) -> Iterator[str]:
        self.output_index += 1
        fc_index = self.output_index
        item_id = _item_id("fc")
        arguments = json.dumps(
            args if isinstance(args, dict) else {}, ensure_ascii=False
        )
        item: dict[str, object] = {
            **_signature_extra(signature),
            "id": item_id,
            "type": "function_call",
            "status": "completed",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        }
        if signature and (trace := get_signature_trace()) is not None:
            trace.record_client_out_sig(
                carrier="function_call",
                wire_field="extra_content",
                signature=signature,
                call_id=call_id,
                call_name=name,
            )
        self.item_order.append(item)
        # The closing frames repeat the entire argument string. Keep the
        # snapshot above, but encode only the frame the HTTP consumer requests.
        # Only the completed argument carrier can expose its signature.
        yield self.sse(
            "response.output_item.added",
            {
                "output_index": fc_index,
                "item": {
                    "id": item_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": name,
                    "arguments": "",
                },
            },
        )
        yield self.sse(
            "response.function_call_arguments.delta",
            {
                "item_id": item_id,
                "output_index": fc_index,
                "delta": arguments,
            },
        )
        yield self.sse(
            "response.function_call_arguments.done",
            {
                "item_id": item_id,
                "output_index": fc_index,
                "arguments": arguments,
            },
        )
        yield self.sse(
            "response.output_item.done",
            {
                "output_index": fc_index,
                "item": dict(item),
            },
        )

    def _close_reasoning(
        self,
        accumulated: str,
        status: str = "completed",
        *,
        stage: PartStage = PartStage.OPEN,
    ) -> Iterator[str]:
        if self.reasoning_item_id is None or self.reasoning_index is None:
            return
        # Release the fragments but retain the joined value for timeout recovery.
        self._reasoning_accumulated[:] = [accumulated]
        final_item: dict[str, object] = {
            **_signature_extra(self.thinking_signature),
            "id": self.reasoning_item_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": accumulated}]
            if stage is not PartStage.ABSENT
            else [],
        }
        if self.include_encrypted_reasoning and self.thinking_signature:
            final_item["encrypted_content"] = encode_signature_envelope(
                self.thinking_signature
            )
        if self.thinking_signature and (trace := get_signature_trace()) is not None:
            trace.record_client_out_sig(
                carrier="thought",
                wire_field=(
                    "extra_content+encrypted_content"
                    if self.include_encrypted_reasoning
                    else "extra_content"
                ),
                signature=self.thinking_signature,
            )
        self.item_order[self.reasoning_index] = final_item
        if stage is PartStage.OPEN:
            yield self.sse(
                "response.reasoning_summary_text.done",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "text": accumulated,
                },
            )
        if stage in (PartStage.OPEN, PartStage.TEXT_DONE):
            yield self.sse(
                "response.reasoning_summary_part.done",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": accumulated},
                },
            )
        yield self.sse(
            "response.output_item.done",
            {"output_index": self.reasoning_index, "item": dict(final_item)},
        )

    def _close_message(
        self,
        accumulated: str,
        status: str = "completed",
        *,
        stage: PartStage = PartStage.OPEN,
    ) -> Iterator[str]:
        if self.message_item_id is None or self.message_index is None:
            return
        self._text_accumulated[:] = [accumulated]
        final_item = {
            "id": self.message_item_id,
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": [{"type": "output_text", "text": accumulated, "annotations": []}]
            if stage is not PartStage.ABSENT
            else [],
        }
        self.item_order[self.message_index] = final_item
        if stage is PartStage.OPEN:
            yield self.sse(
                "response.output_text.done",
                {
                    "item_id": self.message_item_id,
                    "output_index": self.message_index,
                    "content_index": 0,
                    "text": accumulated,
                    "logprobs": [],
                },
            )
        if stage in (PartStage.OPEN, PartStage.TEXT_DONE):
            yield self.sse(
                "response.content_part.done",
                {
                    "item_id": self.message_item_id,
                    "output_index": self.message_index,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "text": accumulated,
                        "annotations": [],
                    },
                },
            )
        yield self.sse(
            "response.output_item.done",
            {"output_index": self.message_index, "item": dict(final_item)},
        )

    def error(self, message: str, code: int = 500) -> Iterator[str]:
        if self.saw_error:
            return
        self.saw_error = True
        # 先收束已打开的 item（以已累积内容 done）：OpenAI 真实行为是
        # 关闭 items 后再发 response.failed，停在 in_progress 的 item
        # 会让 SDK 累积器对账失败
        yield from self._close_reasoning(
            "".join(self._reasoning_accumulated), "incomplete"
        )
        yield from self._close_message("".join(self._text_accumulated), "incomplete")
        yield self._failed(message, code)

    def _failed(self, message: str, code: int) -> str:
        # The typed Responses error.code enum is not Chat's error.type.
        response_code = {400: "invalid_prompt", 429: "rate_limit_exceeded"}.get(
            code, "server_error"
        )
        return self.sse(
            "response.failed",
            {
                "response": {
                    **self._response_snapshot(status="failed", output=self.item_order),
                    "error": {
                        "code": response_code,
                        "message": message,
                    },
                }
            },
        )

    def _delivered_text(self, item: dict[str, object], length: int) -> str:
        if not length:
            return ""
        reasoning = item["type"] == "reasoning"
        parts = item["summary" if reasoning else "content"]
        if isinstance(parts, list) and parts and isinstance(parts[0], dict):
            # The producer may already have closed this item and advanced.
            return _as_str(parts[0].get("text"))[:length]
        fragments = self._reasoning_accumulated if reasoning else self._text_accumulated
        prefix = []
        for fragment in fragments:
            prefix.append(fragment[:length])
            length -= len(prefix[-1])
            if not length:
                break
        return "".join(prefix)

    def timeout_error(self, message: str) -> list[str]:
        sent = self.delivery
        if sent is None:
            return list(self.error(message, code=504))
        if not sent.begin_failure():
            return []
        emitter = _ResponsesStreamEmitter(
            self.response_id, self.requested_model, tool_metadata=self.tool_metadata
        )
        emitter.created_at = self.created_at
        emitter.seq = sent.sequence
        chunks = [emitter.created()] if sent.sequence == -1 else []
        # Items are opened serially and in output_index order. Already closed
        # snapshots are immutable; only the final accepted item may be partial.
        index = sent.output_index
        emitter.item_order = self.item_order[: index + 1]
        if index >= 0 and sent.last != b"response.output_item.done":
            item = emitter.item_order[-1]
            item_id = _as_str(item["id"])
            if item["type"] == "function_call":
                # No synthetic arguments.done: an unfinished call is not an
                # instruction to execute, even if the producer knew more.
                partial = {
                    **item,
                    "status": "incomplete",
                    "arguments": _as_str(item["arguments"])[: sent.characters],
                }
                partial.pop("extra_content", None)
                emitter.item_order[-1] = partial
                chunks.append(
                    emitter.sse(
                        "response.output_item.done",
                        {"output_index": index, "item": partial},
                    )
                )
            else:
                text = self._delivered_text(item, sent.characters)
                stage = sent.part_stage()
                if item["type"] == "reasoning":
                    emitter.reasoning_item_id, emitter.reasoning_index = item_id, index
                    chunks.extend(
                        emitter._close_reasoning(text, "incomplete", stage=stage)
                    )
                else:
                    emitter.message_item_id, emitter.message_index = item_id, index
                    chunks.extend(
                        emitter._close_message(text, "incomplete", stage=stage)
                    )
        chunks.append(emitter._failed(message, 504))
        return chunks

    def finish(self) -> Iterator[str]:
        """收束全部打开的 item 并下发终结事件。"""
        if self.saw_error:
            return
        incomplete_reason = _incomplete_reason(self.finish_reason_raw)
        final_status = "incomplete" if incomplete_reason else "completed"
        # Serialize one event per iteration, so protocol-required copies of the
        # same text do not pile up while the preceding ASGI send is pending.
        yield from self._close_reasoning(
            "".join(self._reasoning_accumulated), final_status
        )
        yield from self._close_message("".join(self._text_accumulated), final_status)
        event_type = (
            "response.incomplete" if incomplete_reason else "response.completed"
        )
        yield self.sse(
            event_type,
            {
                "response": self._response_snapshot(
                    status=final_status,
                    output=self.item_order,
                    incomplete_reason=incomplete_reason,
                )
            },
        )


def build_responses_preparation_failure(
    requested_model: str,
    code: int,
    message: str,
    *,
    register_failure: Callable[[Callable[[str], list[str]]], None],
    delivery: ResponsesStreamDelivery | None,
    tool_metadata: ResponsesToolMetadata | None = None,
) -> list[str]:
    """Open and fail a Responses stream whose HTTP headers are already sent."""
    emitter = _ResponsesStreamEmitter(
        _response_id(), requested_model, delivery, tool_metadata=tool_metadata
    )
    register_failure(emitter.timeout_error)
    return [emitter.created(), *emitter.error(message, code)]


async def responses_stream_adapter(
    source: AsyncIterable[object],
    *,
    requested_model: str,
    register_failure: Callable[[Callable[[str], list[str]]], None] | None = None,
    delivery: ResponsesStreamDelivery | None = None,
    include_encrypted_reasoning: bool = False,
    tool_metadata: ResponsesToolMetadata | None = None,
) -> AsyncGenerator[str, None]:
    """Gemini SSE → OpenAI Responses 语义事件流。"""
    response_id = _response_id()
    emitter = _ResponsesStreamEmitter(
        response_id,
        requested_model,
        delivery,
        include_encrypted_reasoning=include_encrypted_reasoning,
        tool_metadata=tool_metadata,
    )
    async with _stream_adapter_lifecycle(
        source, emitter.timeout_error, register_failure
    ):
        yield emitter.created()
        yield emitter.in_progress()
        from aistudio_api.application.compat_service import (
            _frame_candidate,
            _frame_parts,
            _iter_gemini_sse_frames,
        )

        try:
            async for frame in _iter_gemini_sse_frames(source):
                error_obj = frame.get("error")
                if isinstance(error_obj, dict):
                    code = _as_int(error_obj.get("code")) or 500
                    message = _as_str(error_obj.get("message")) or "Upstream error"
                    for event in emitter.error(message, code):
                        yield event
                    return
                candidate = _frame_candidate(frame)
                finish = _as_str(candidate.get("finishReason"))
                if finish:
                    emitter.finish_reason_raw = finish
                usage_metadata = frame.get("usageMetadata")
                if isinstance(usage_metadata, dict):
                    emitter.usage = _usage_payload_from_gemini(usage_metadata)
                for part in _frame_parts(candidate):
                    raw_sig = _as_str(part.get("thoughtSignature"))
                    function_call = part.get("functionCall")
                    if isinstance(function_call, dict):
                        call_id = _as_str(function_call.get("id")) or _call_id()
                        name = _as_str(function_call.get("name")) or "unknown"
                        args = function_call.get("args")
                        sig = part_payload_signature(part, "functionCall")
                        if (
                            raw_sig
                            and not sig
                            and (trace := get_signature_trace()) is not None
                        ):
                            trace.record_client_out_drop(
                                carrier="function_call",
                                signature=raw_sig,
                                reason="compound_part_unrepresentable",
                                call_id=call_id,
                                call_name=name,
                            )
                        for event in emitter.boundary("function_call"):
                            yield event
                        for event in emitter.function_call(
                            call_id,
                            name,
                            args,
                            sig,
                        ):
                            yield event
                        continue
                    inline = _as_dict(part.get("inlineData"))
                    if inline:
                        if raw_sig and (trace := get_signature_trace()) is not None:
                            trace.record_client_out_drop(
                                carrier="inline_data",
                                signature=raw_sig,
                                reason="inline_data_unrepresentable",
                            )
                        # Responses 无原生图片 part：与 Chat 侧一致降级为
                        # markdown data-URL 占位，避免静默空回（单次构造，
                        # 累积由 text_delta 内部完成）
                        mime = _as_str(inline.get("mimeType")) or "image/png"
                        data = _as_str(inline.get("data"))
                        if data:
                            for event in emitter.boundary("message"):
                                yield event
                            for event in emitter.text_delta(
                                f"\n\n![image](data:{mime};base64,{data})"
                            ):
                                yield event
                        continue
                    text = part.get("text")
                    if isinstance(text, str):
                        if part.get("thought") is True:
                            signature = part_payload_signature(part, "text")
                            if text or _is_signed_empty_thought(text, signature):
                                if (
                                    raw_sig
                                    and not signature
                                    and (trace := get_signature_trace()) is not None
                                ):
                                    trace.record_client_out_drop(
                                        carrier="thought",
                                        signature=raw_sig,
                                        reason="compound_part_unrepresentable",
                                    )
                                for event in emitter.boundary("reasoning", signature):
                                    yield event
                                for event in emitter.reasoning_delta(text):
                                    yield event
                            elif (
                                raw_sig and (trace := get_signature_trace()) is not None
                            ):
                                trace.record_client_out_drop(
                                    carrier="thought",
                                    signature=raw_sig,
                                    reason="compound_part_unrepresentable",
                                )
                        elif text:
                            if raw_sig and (trace := get_signature_trace()) is not None:
                                trace.record_client_out_drop(
                                    carrier="text",
                                    signature=raw_sig,
                                    reason="text_part_unrepresentable",
                                )
                            for event in emitter.boundary("message"):
                                yield event
                            for event in emitter.text_delta(text):
                                yield event
                        elif raw_sig and (trace := get_signature_trace()) is not None:
                            trace.record_client_out_drop(
                                carrier="tail_empty",
                                signature=raw_sig,
                                reason="tail_empty_unrepresentable",
                            )
                    elif raw_sig and (trace := get_signature_trace()) is not None:
                        carrier, reason = _dropped_part_reason(part)
                        trace.record_client_out_drop(
                            carrier=carrier,
                            signature=raw_sig,
                            reason=reason,
                        )
        except Exception as exc:
            logger.warning("Responses 流式转换异常: %s", exc)
            record_result(
                "error", error=f"Stream conversion failed: {type(exc).__name__}: {exc}"
            )
            for event in emitter.error(f"{type(exc).__name__}: {exc}"):
                yield event
            return
        for event in emitter.finish():
            yield event
        if (trace := get_signature_trace()) is not None:
            trace.emit_inbound_log()


__all__ = [
    "ResponsesToolMetadata",
    "build_responses_object",
    "build_responses_preparation_failure",
    "responses_stream_adapter",
    "responses_to_gemini_request",
]
