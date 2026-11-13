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

import contextlib
import json
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Callable

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiPart,
)
from aistudio_api.api.schemas_compat import OpenAIResponsesRequest
from aistudio_api.application.api_service_gemini import _ERROR_TEXT_PREFIX
from aistudio_api.application.compat_service import (
    _as_dict,
    _as_str,
    _media_part_from_url,
    _openai_tool_choice_to_config,
    _openai_tools_to_gemini,
    _parse_data_url,
    resolve_compat_model,
)
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.responses")

# Responses reasoning.effort → Gemini reasoningEffort（xhigh/max 为上游
# VERY_HIGH(5) 的 SDK 别名，与 chat completions 侧的 effort 白名单一致）
_RESPONSES_EFFORT_MAP: dict[str, str] = {
    "none": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "very_high": "very_high",
    "max": "xhigh",
}

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
    from aistudio_api.api.schemas import GeminiInlineData

    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
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
            if file_data.startswith("data:"):
                parsed = _parse_data_url(file_data)
                if parsed is not None:
                    mime, data = parsed
                    parts.append(
                        GeminiPart(
                            inlineData=GeminiInlineData(mimeType=mime, data=data)
                        )
                    )
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


def _tool_output_text(output: object) -> dict[str, object]:
    """``function_call_output.output``（str 或 content 数组）→ functionResponse 载荷。"""
    if isinstance(output, str):
        return {"result": output}
    if isinstance(output, list):
        texts: list[str] = []
        for part in output:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in ("output_text", "input_text", "text"):
                texts.append(_as_str(part.get("text")))
            elif part_type == "input_image":
                # 图片无 functionResponse 通道：占位提示模型（静默丢弃
                # 会让模型以为结果缺失而重复调用）
                texts.append("[image omitted]")
        return {"result": "\n".join(t for t in texts if t) or ""}
    return {"result": ""}


async def responses_to_gemini_request(
    req: OpenAIResponsesRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """OpenAI Responses 请求 → (Gemini 请求, 解析后的模型名)。"""
    resolved_model = resolve_compat_model(req.model, channel="openai")
    contents: list[GeminiContent] = []
    tool_name_by_call_id: dict[str, str] = {}

    # 第一遍：扫 function_call items 建 call_id → 函数名映射（容忍乱序历史）
    for item in req.input if isinstance(req.input, list) else []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = _as_str(item.get("call_id"))
        name = _as_str(item.get("name"))
        if call_id and name:
            tool_name_by_call_id[call_id] = name

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
                    # system/developer 消息插入为 [SYSTEM] 前缀 user turn
                    # （保序，语义分层：instructions 走 systemInstruction）。
                    # content 可为字符串或块数组（input_text 块）
                    text = _content_text(item.get("content"))
                    if text:
                        contents.append(
                            GeminiContent(
                                role="user", parts=[GeminiPart(text=f"[SYSTEM] {text}")]
                            )
                        )
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
                                functionCall=GeminiFunctionCall(
                                    name=name,
                                    args=parsed_args
                                    if isinstance(parsed_args, dict)
                                    else {},
                                    id=call_id or None,
                                )
                            )
                        ],
                    )
                )
            elif item_type == "function_call_output":
                call_id = _as_str(item.get("call_id"))
                name = tool_name_by_call_id.get(call_id, "unknown")
                payload = _tool_output_text(item.get("output"))
                contents.append(
                    GeminiContent(
                        role="user",
                        parts=[
                            GeminiPart(
                                functionResponse=GeminiFunctionResponse(
                                    name=name,
                                    response=payload,
                                    id=call_id or None,
                                )
                            )
                        ],
                    )
                )
            elif item_type == "reasoning":
                # 思维链回传：上游不消费（伪造 thoughtSignature 会触发风控），
                # 静默丢弃即可——客户端会随完整历史重发
                continue
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
        effort = _as_str(req.reasoning.get("effort")).strip().lower()
        mapped = _RESPONSES_EFFORT_MAP.get(effort)
        if mapped:
            generation_config["reasoningEffort"] = mapped
        elif effort:
            logger.debug("忽略无法识别的 reasoning.effort: %s", effort)
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
            GeminiContent(role="user", parts=[GeminiPart(text=req.instructions)])
            if req.instructions
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
    """把 Gemini 输出 parts 聚合为 Responses output items。"""

    def __init__(self) -> None:
        self.reasoning_texts: list[str] = []
        self.text_parts: list[str] = []
        self.function_calls: list[dict[str, object]] = []

    def add_part(self, part: object) -> None:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            if getattr(part, "thought", None) is True:
                self.reasoning_texts.append(text)
            else:
                self.text_parts.append(text)
            return
        inline = getattr(part, "inlineData", None)
        if inline is not None:
            # Responses 协议无原生图片 part：降级为 markdown 占位（与
            # Chat 侧 data-URL markdown 行为对齐），静默丢弃是最差选择
            self.text_parts.append(
                f"\n\n![image](data:{inline.mimeType};base64,{inline.data})"
            )
            return
        function_call = getattr(part, "functionCall", None)
        if function_call is not None:
            self.function_calls.append(
                {
                    "id": _item_id("fc"),
                    "call_id": function_call.id or _call_id(),
                    "name": function_call.name,
                    "arguments": json.dumps(
                        function_call.args if function_call.args is not None else {},
                        ensure_ascii=False,
                    ),
                }
            )

    def build_output(self) -> list[dict[str, object]]:
        """产出 Responses output items（reasoning → message → function_call 顺序）。"""
        output: list[dict[str, object]] = []
        if self.reasoning_texts:
            output.append(
                {
                    "id": _item_id("rs"),
                    "type": "reasoning",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": "".join(self.reasoning_texts),
                        }
                    ],
                }
            )
        if self.text_parts:
            output.append(
                {
                    "id": _item_id("msg"),
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "".join(self.text_parts),
                            "annotations": [],
                        }
                    ],
                }
            )
        for call in self.function_calls:
            output.append(
                {
                    "id": call["id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call["call_id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                }
            )
        return output


def _base_response(
    response_id: str,
    requested_model: str,
    *,
    status: str,
    output: list[dict[str, object]],
    usage: dict[str, int] | None,
    incomplete_reason: str | None = None,
) -> dict[str, object]:
    # tools / tool_choice / parallel_tool_calls 为 openai-python 类型模型
    # 必填字段：流式宽松构造不报错，但 model_validate 严格校验的客户端
    # （pydantic / 第三方 SDK）会直接 ValidationError。
    payload: dict[str, object] = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": requested_model,
        "output": output,
        "parallel_tool_calls": False,
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


def build_responses_object(
    response: GeminiGenerateContentResponse, requested_model: str
) -> dict[str, object]:
    """Gemini 完整响应 → Responses ``response`` 对象（非流式）。"""
    finish_reason_raw: str | None = None
    candidates = response.candidates or []
    if candidates:
        finish_reason_raw = candidates[0].finishReason

    builder = _ResponsesOutputBuilder()
    for part in _iter_response_parts(response):
        builder.add_part(part)

    incomplete_reason = _FINISH_TO_INCOMPLETE_REASON.get(finish_reason_raw or "")
    status = "incomplete" if incomplete_reason else "completed"
    usage = _usage_payload_from_gemini(response.usageMetadata)
    return _base_response(
        _response_id(),
        requested_model,
        status=status,
        output=builder.build_output(),
        usage=usage,
        incomplete_reason=incomplete_reason,
    )


def _iter_response_parts(
    response: GeminiGenerateContentResponse,
) -> list[object]:
    candidates = response.candidates or []
    if not candidates:
        return []
    content = candidates[0].content
    return list(content.parts) if content else []


# ---------------------------------------------------------------------------
# 流式适配：Gemini SSE → Responses 语义事件流
# ---------------------------------------------------------------------------


class _ResponsesStreamEmitter:
    """Responses SSE 事件发射器（事件形状对齐 openai-python 3.50.0）。"""

    def __init__(self, response_id: str, requested_model: str) -> None:
        self.response_id = response_id
        self.requested_model = requested_model
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
        self.function_items: dict[str, dict[str, object]] = {}
        self.finish_reason_raw: str | None = None
        self.usage: dict[str, int] | None = None
        self.saw_error = False
        # 累积文本（output_text.done / completed 快照需要完整文本）
        self._text_accumulated: str = ""
        self._reasoning_accumulated: str = ""

    def sse(self, event_type: str, payload: dict[str, object]) -> str:
        self.seq += 1
        frame = {"type": event_type, "sequence_number": self.seq, **payload}
        return (
            f"event: {event_type}\ndata: "
            + json.dumps(frame, ensure_ascii=False)
            + "\n\n"
        )

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
        return [
            *self._open_reasoning(),
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

    def function_call(self, call_id: str, name: str, args: object) -> list[str]:
        self.output_index += 1
        fc_index = self.output_index
        item_id = _item_id("fc")
        arguments = json.dumps(
            args if isinstance(args, dict) else {}, ensure_ascii=False
        )
        item: dict[str, object] = {
            "id": item_id,
            "type": "function_call",
            "status": "completed",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        }
        self.function_items[item_id] = item
        self.item_order.append((fc_index, dict(item)))
        return [
            self.sse(
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
            ),
            self.sse(
                "response.function_call_arguments.delta",
                {
                    "item_id": item_id,
                    "output_index": fc_index,
                    "delta": arguments,
                },
            ),
            self.sse(
                "response.function_call_arguments.done",
                {
                    "item_id": item_id,
                    "output_index": fc_index,
                    "arguments": arguments,
                },
            ),
            self.sse(
                "response.output_item.done",
                {
                    "output_index": fc_index,
                    "item": dict(item),
                },
            ),
        ]

    def _close_reasoning(self, accumulated: str) -> list[str]:
        if self.reasoning_item_id is None:
            return []
        final_item = {
            "id": self.reasoning_item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": accumulated}],
        }
        self._replace_order_item(self.reasoning_item_id, final_item)
        return [
            self.sse(
                "response.reasoning_summary_text.done",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "text": accumulated,
                },
            ),
            self.sse(
                "response.reasoning_summary_part.done",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": accumulated},
                },
            ),
            self.sse(
                "response.output_item.done",
                {"output_index": self.reasoning_index, "item": dict(final_item)},
            ),
        ]

    def _close_message(self, accumulated: str) -> list[str]:
        if self.message_item_id is None:
            return []
        final_item = {
            "id": self.message_item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": accumulated, "annotations": []}
            ],
        }
        self._replace_order_item(self.message_item_id, final_item)
        return [
            self.sse(
                "response.output_text.done",
                {
                    "item_id": self.message_item_id,
                    "output_index": self.message_index,
                    "content_index": 0,
                    "text": accumulated,
                },
            ),
            self.sse(
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
            ),
            self.sse(
                "response.output_item.done",
                {"output_index": self.message_index, "item": dict(final_item)},
            ),
        ]

    def _replace_order_item(self, item_id: str, final_item: dict[str, object]) -> None:
        """把 item_order 中该 item 的占位快照替换为关闭时的完整内容。"""
        for pos, (index, existing) in enumerate(self.item_order):
            if existing.get("id") == item_id:
                self.item_order[pos] = (index, final_item)
                return

    def error(self, message: str) -> list[str]:
        self.saw_error = True
        # 先收束已打开的 item（以已累积内容 done）：OpenAI 真实行为是
        # 关闭 items 后再发 response.failed，停在 in_progress 的 item
        # 会让 SDK 累积器对账失败
        events = self._close_reasoning(self._reasoning_accumulated)
        events += self._close_message(self._text_accumulated)
        events.append(
            self.sse(
                "response.failed",
                {
                    "response": {
                        **self._response_snapshot(
                            status="failed", output=self._ordered_output()
                        ),
                        "error": {
                            "code": "server_error",
                            "message": message,
                        },
                    }
                },
            )
        )
        return events

    def _ordered_output(self) -> list[dict[str, object]]:
        """按实际打开顺序（output_index）产出 items 快照。"""
        return [item for _, item in sorted(self.item_order, key=lambda pair: pair[0])]

    def finish(self) -> list[str]:
        """收束全部打开的 item 并下发终结事件。"""
        if self.saw_error:
            return []
        events = self._close_reasoning(self._reasoning_accumulated)
        events += self._close_message(self._text_accumulated)
        incomplete_reason = _FINISH_TO_INCOMPLETE_REASON.get(
            self.finish_reason_raw or ""
        )
        final_status = "incomplete" if incomplete_reason else "completed"
        # 快照 item 的 status 与终结状态对齐
        for _, item in self.item_order:
            if item["type"] in ("reasoning", "message", "function_call"):
                item["status"] = final_status
        output = self._ordered_output()
        event_type = (
            "response.incomplete" if incomplete_reason else "response.completed"
        )
        events.append(
            self.sse(
                event_type,
                {
                    "response": self._response_snapshot(
                        status=final_status,
                        output=output,
                        incomplete_reason=incomplete_reason,
                    )
                },
            )
        )
        return events


async def responses_stream_adapter(
    source: AsyncIterable[object],
    *,
    requested_model: str,
) -> AsyncGenerator[str, None]:
    """Gemini SSE → OpenAI Responses 语义事件流。"""
    response_id = _response_id()
    emitter = _ResponsesStreamEmitter(response_id, requested_model)
    yield emitter.created()
    yield emitter.in_progress()

    source_aclose: Callable[[], Awaitable[None]] | None = getattr(
        source, "aclose", None
    )
    try:
        from aistudio_api.application.compat_service import (
            _frame_candidate,
            _frame_parts,
            _iter_gemini_sse_frames,
        )

        async for frame in _iter_gemini_sse_frames(source):
            error_obj = frame.get("error")
            if isinstance(error_obj, dict):
                message = _as_str(error_obj.get("message")) or "Upstream error"
                for event in emitter.error(message):
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
                    for event in emitter.function_call(call_id, name, args):
                        yield event
                    continue
                inline = _as_dict(part.get("inlineData"))
                if inline:
                    # Responses 无原生图片 part：与 Chat 侧一致降级为
                    # markdown data-URL 占位，避免静默空回
                    mime = _as_str(inline.get("mimeType")) or "image/png"
                    data = _as_str(inline.get("data"))
                    if data:
                        emitter._text_accumulated += (
                            f"\n\n![image](data:{mime};base64,{data})"
                        )
                        for event in emitter.text_delta(
                            f"\n\n![image](data:{mime};base64,{data})"
                        ):
                            yield event
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    # 上游错误兜底文本（format_sse_error_text）不是正文：
                    # 已输出错误事件的情况下不重复下发为 output_text
                    if text.startswith(_ERROR_TEXT_PREFIX):
                        continue
                    if part.get("thought") is True:
                        emitter._reasoning_accumulated += text
                        for event in emitter.reasoning_delta(text):
                            yield event
                    else:
                        emitter._text_accumulated += text
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
    else:
        for event in emitter.finish():
            yield event
    finally:
        if callable(source_aclose):
            with contextlib.suppress(Exception):
                await source_aclose()


__all__ = [
    "build_responses_object",
    "responses_stream_adapter",
    "responses_to_gemini_request",
]
