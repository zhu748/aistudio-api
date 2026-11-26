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

from aistudio_api.api.response_models import (
    GeminiGenerateContentResponse,
)
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
)
from aistudio_api.api.schemas_compat import OpenAIResponsesRequest
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
    _read_signature,
    _signature_extra,
    _stream_adapter_lifecycle,
    normalize_reasoning_effort_word,
    resolve_compat_model,
)
from aistudio_api.application.compat_shared import (
    _code_text,
    _is_signed_empty_thought,
    _parse_data_url,
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
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.responses")
_STANDARD_BASE64 = re.compile(r"[A-Za-z0-9+/]++={0,2}")
_UNFINISHED_CALL_STATUSES = ("incomplete", "in_progress")

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


async def _parts_from_input_content(content: object) -> list[GeminiPart]:
    """Responses input content（str 或 content part 数组）→ Gemini parts。

    支持 ``input_text`` / ``input_image``（image_url 或 file_id 形态不适用
    ——本项目无文件存储，仅 URL/data URL）/ ``input_file``（data URL）；
    其余类型（audio 等）忽略并打日志。
    """
    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
    if len(content) > 1 and has_remote_media(content):
        return await _convert_items_concurrently(content, _parts_from_input_content)
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
        elif part_type == "input_image":
            # data URL / http(s) URL；remote 回传 file_id 场景无法回源，忽略
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


@media_session()
async def responses_to_gemini_request(
    req: OpenAIResponsesRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """OpenAI Responses 请求 → (Gemini 请求, 解析后的模型名)。"""
    resolved_model = resolve_compat_model(req.model, channel="openai")
    if req.previous_response_id:
        raise ValueError(
            "previous_response_id is not supported; send the complete conversation in input"
        )
    if req.parallel_tool_calls is False:
        # OpenAI 官方语义是提示而非硬约束；接受+忽略+告警（与 chat
        # completions 侧同策略）。
        logger.warning("parallel_tool_calls=false 无法在上游强制串行，已忽略该提示")
    contents: list[GeminiContent] = []
    system_texts: list[str] = [req.instructions] if req.instructions else []
    tool_name_by_call_id: dict[str, str] = {}

    # Index call ownership first because a result may precede its call.
    replayable_call_ids: set[str] = set()
    unfinished_call_ids: set[str] = set()
    for item in req.input if isinstance(req.input, list) else []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = _as_str(item.get("call_id"))
        if not call_id:
            continue
        if item.get("status") in _UNFINISHED_CALL_STATUSES:
            unfinished_call_ids.add(call_id)
            continue
        replayable_call_ids.add(call_id)
        if name := _as_str(item.get("name")):
            tool_name_by_call_id[call_id] = name
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
                parts = await _parts_from_input_content(item.get("content"))
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
                if item.get("status") in _UNFINISHED_CALL_STATUSES:
                    continue
                name = _as_str(item.get("name")) or "unknown"
                call_id = _as_str(item.get("call_id"))
                args = item.get("arguments")
                parsed_args: object = {}
                if isinstance(args, str) and args.strip():
                    try:
                        parsed_args = json.loads(args)
                    except json.JSONDecodeError:
                        parsed_args = {"raw_arguments": args}
                elif isinstance(args, dict):
                    parsed_args = args
                if call_id:
                    tool_name_by_call_id[call_id] = name
                contents.append(
                    GeminiContent(
                        role="model",
                        parts=[
                            GeminiPart(
                                thoughtSignature=_read_signature(item),
                                functionCall=GeminiFunctionCall(
                                    name=name,
                                    args=parsed_args
                                    if isinstance(parsed_args, dict)
                                    else {},
                                    id=call_id or None,
                                ),
                            )
                        ],
                    )
                )
            elif item_type == "function_call_output":
                call_id = _as_str(item.get("call_id"))
                if call_id in unfinished_call_ids:
                    continue
                name = tool_name_by_call_id.get(call_id, "unknown")
                payload, media = _tool_output_payload(item.get("output"))
                contents.append(
                    GeminiContent(
                        role="user",
                        parts=[
                            GeminiPart(
                                functionResponse=GeminiFunctionResponse(
                                    name=name,
                                    response=payload,
                                    id=call_id or None,
                                    parts=media or None,
                                )
                            )
                        ],
                    )
                )
            elif item_type == "reasoning":
                # 思维链回传：思考摘要转 thought part 保留思考链（此前整项
                # 静默丢弃，Codex CLI 等客户端重发的 reasoning 历史全部丢失）。
                # 签名经 extra_content 精确回读——值由网关 reasoning item
                # 原样下发（与 function_call item 同一载体形态），伪造值在
                # 规范化层校验显式载体；缺失时不可从其他摘要推断签名。
                summary_text = _reasoning_summary_text(item)
                signature = _read_signature(item)
                if not summary_text and not (
                    _is_explicit_empty_summary(item)
                    and _is_signed_empty_thought(summary_text, signature)
                ):
                    continue
                contents.append(
                    GeminiContent(
                        role="model",
                        parts=[
                            GeminiPart(
                                text=summary_text,
                                thought=True,
                                thoughtSignature=signature,
                            )
                        ],
                    )
                )
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

    gemini_req = GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=(
            GeminiContent(
                role="user", parts=[GeminiPart(text="\n\n".join(system_texts))]
            )
            if system_texts
            else None
        ),
        tools=_openai_tools_to_gemini(req.tools),
        toolConfig=_openai_tool_choice_to_config(req.tool_choice),
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

    def __init__(self) -> None:
        self.output: list[dict[str, object]] = []
        self._kind = ""
        self._buffer: list[str] = []
        self._signature = ""

    def _flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        if self._kind == "reasoning":
            self.output.append(
                {
                    **_signature_extra(self._signature),
                    "id": _item_id("rs"),
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": text}],
                }
            )
        else:
            self.output.append(
                {
                    "id": _item_id("msg"),
                    "type": "message",
                    "role": "assistant",
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
        if isinstance(text, str):
            if getattr(part, "thought", None) is True:
                signature = part_payload_signature(part, "text")
                if text or _is_signed_empty_thought(text, signature):
                    self._append("reasoning", text, signature)
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

    def build_output(self, status: str) -> list[dict[str, object]]:
        self._flush()
        for item in self.output:
            if item["type"] == "message":
                item["status"] = status
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
        "tool_choice": "auto",
        "tools": [],
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
    response: GeminiGenerateContentResponse, requested_model: str
) -> dict[str, object]:
    """Gemini 完整响应 → Responses ``response`` 对象（非流式）。"""
    finish_reason_raw: str | None = None
    candidates = response.candidates or []
    if candidates:
        finish_reason_raw = candidates[0].finishReason

    builder = _ResponsesOutputBuilder()
    builder_allow_tools = finish_reason_raw == "STOP"
    for part in _iter_response_parts(response):
        builder.add_part(part, allow_tools=builder_allow_tools)

    incomplete_reason = _incomplete_reason(finish_reason_raw)
    status = "incomplete" if incomplete_reason else "completed"
    usage = _usage_payload_from_gemini(response.usageMetadata)
    return _base_response(
        _response_id(),
        requested_model,
        status=status,
        output=builder.build_output(status),
        usage=usage,
        incomplete_reason=incomplete_reason,
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
    ) -> None:
        self.response_id = response_id
        self.requested_model = requested_model
        self.delivery = delivery
        self.created_at = int(time.time())
        self.seq = -1  # 首事件自增后为 0，与 SDK 从 0 递增一致
        self.output_index = -1
        # 各 item 记住自己的 output_index：关闭事件与 delta 事件必须报
        # item 自己的 index，不能引用共享计数器的当前值（交错流中当前值
        # 已指向后打开的 item，SDK 流累积器按 index 对账会错位）
        self.reasoning_index: int | None = None
        self.message_index: int | None = None
        # (index, item) 对，completed 快照按实际打开顺序排序
        self.item_order: list[tuple[int, dict[str, object]]] = []
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
            (
                self.reasoning_index,
                {
                    "id": self.reasoning_item_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                },
            )
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
            (
                self.message_index,
                {
                    "id": self.message_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            )
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
        self.item_order.append((fc_index, dict(item)))
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
        if self.reasoning_item_id is None:
            return
        # Release the fragments but retain the joined value for timeout recovery.
        self._reasoning_accumulated[:] = [accumulated]
        final_item = {
            **_signature_extra(self.thinking_signature),
            "id": self.reasoning_item_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": accumulated}]
            if stage is not PartStage.ABSENT
            else [],
        }
        self._replace_order_item(self.reasoning_item_id, final_item)
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
        if self.message_item_id is None:
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
        self._replace_order_item(self.message_item_id, final_item)
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

    def _replace_order_item(self, item_id: str, final_item: dict[str, object]) -> None:
        """把 item_order 中该 item 的占位快照替换为关闭时的完整内容。"""
        for pos, (index, existing) in enumerate(self.item_order):
            if existing.get("id") == item_id:
                self.item_order[pos] = (index, final_item)
                return

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
                    **self._response_snapshot(
                        status="failed", output=self._ordered_output()
                    ),
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
        emitter = _ResponsesStreamEmitter(self.response_id, self.requested_model)
        emitter.created_at = self.created_at
        emitter.seq = sent.sequence
        chunks = [emitter.created()] if sent.sequence == -1 else []
        # Items are opened serially and in output_index order. Already closed
        # snapshots are immutable; only the final accepted item may be partial.
        index = sent.output_index
        emitter.item_order = self.item_order[: index + 1]
        if index >= 0 and sent.last != b"response.output_item.done":
            _, item = emitter.item_order[-1]
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
                emitter.item_order[-1] = (index, partial)
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

    def _ordered_output(self) -> list[dict[str, object]]:
        """按实际打开顺序（output_index）产出 items 快照。"""
        return [item for _, item in sorted(self.item_order, key=lambda pair: pair[0])]

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
        output = self._ordered_output()
        event_type = (
            "response.incomplete" if incomplete_reason else "response.completed"
        )
        yield self.sse(
            event_type,
            {
                "response": self._response_snapshot(
                    status=final_status,
                    output=output,
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
) -> list[str]:
    """Open and fail a Responses stream whose HTTP headers are already sent."""
    emitter = _ResponsesStreamEmitter(_response_id(), requested_model, delivery)
    register_failure(emitter.timeout_error)
    return [emitter.created(), *emitter.error(message, code)]


async def responses_stream_adapter(
    source: AsyncIterable[object],
    *,
    requested_model: str,
    register_failure: Callable[[Callable[[str], list[str]]], None] | None = None,
    delivery: ResponsesStreamDelivery | None = None,
) -> AsyncGenerator[str, None]:
    """Gemini SSE → OpenAI Responses 语义事件流。"""
    response_id = _response_id()
    emitter = _ResponsesStreamEmitter(response_id, requested_model, delivery)
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
                    function_call = part.get("functionCall")
                    if isinstance(function_call, dict):
                        call_id = _as_str(function_call.get("id")) or _call_id()
                        name = _as_str(function_call.get("name")) or "unknown"
                        args = function_call.get("args")
                        for event in emitter.boundary("function_call"):
                            yield event
                        for event in emitter.function_call(
                            call_id,
                            name,
                            args,
                            part_payload_signature(part, "functionCall"),
                        ):
                            yield event
                        continue
                    inline = _as_dict(part.get("inlineData"))
                    if inline:
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
                                for event in emitter.boundary("reasoning", signature):
                                    yield event
                                for event in emitter.reasoning_delta(text):
                                    yield event
                        elif text:
                            for event in emitter.boundary("message"):
                                yield event
                            for event in emitter.text_delta(text):
                                yield event
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


__all__ = [
    "build_responses_object",
    "build_responses_preparation_failure",
    "responses_stream_adapter",
    "responses_to_gemini_request",
]
