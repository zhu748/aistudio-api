"""Anthropic(Claude) Messages 协议兼容转换层。

自 compat_service.py 拆出：请求侧（Messages → Gemini 请求模型，含
tool_use / tool_result / thinking / image / document 块转换）、响应侧
（Gemini 响应 / SSE 帧 → Message 对象与 Messages SSE 事件流），以及
thinking.budget_tokens → 思考等级映射。

媒体管线、签名缓存、SSE 帧解析等双侧共用工具见 compat_shared；
错误码表、refusal 终结集与 thinking 占位签名见 compat_errors。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncGenerator, AsyncIterable, Callable

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application.api_service_gemini import _ERROR_TEXT_PREFIX
from aistudio_api.application.compat_errors import (
    _CLAUDE_ERROR_TYPE,
    _CLAUDE_REFUSAL_FINISH,
    _claude_signature,
    _read_claude_signature,
)
from aistudio_api.application.compat_shared import (
    _as_dict,
    _as_int,
    _as_str,
    _assert_media_size,
    _code_text,
    _convert_items_concurrently,
    _frame_candidate,
    _frame_parts,
    _is_signed_empty_thought,
    _iter_gemini_sse_frames,
    _iter_response_parts,
    _media_part_from_url,
    _read_signature,
    _signature_extra,
    _stream_adapter_lifecycle,
    _usage_from_gemini,
    resolve_compat_model,
)
from aistudio_api.application.media_session import has_remote_media, media_session
from aistudio_api.application.stream_utils import (
    StopSequenceBuffer,
    part_payload_signature,
)
from aistudio_api.domain.tool_signatures import (
    remember_function_signature as _remember_function_signature,
)
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.claude")


async def _claude_blocks_to_parts(
    content: object,
    *,
    tool_name_by_id: dict[str, str],
) -> list[GeminiPart]:
    """Claude content（str 或块数组）→ Gemini parts。

    支持 text / thinking / image / document / tool_use / tool_result 块；
    tool_result 中的图片会展开为紧随其后的独立 inlineData parts。
    工具调用无可恢复签名时携带跳过校验的占位值，保留原生结构。
    """
    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
    if len(content) > 1 and has_remote_media(content):
        return await _convert_items_concurrently(
            content,
            lambda blocks: _claude_blocks_to_parts(
                blocks, tool_name_by_id=tool_name_by_id
            ),
        )
    parts: list[GeminiPart] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(GeminiPart(text=_as_str(block.get("text"))))
        elif block_type == "thinking":
            thinking = block.get("thinking")
            signature = _read_claude_signature(block.get("signature"))
            if isinstance(thinking, str) and (
                thinking or _is_signed_empty_thought(thinking, signature)
            ):
                parts.append(
                    GeminiPart(text=thinking, thought=True, thoughtSignature=signature)
                )
        elif block_type == "image":
            source = _as_dict(block.get("source"))
            source_type = source.get("type")
            if source_type == "base64":
                part = _claude_base64_part(source, "image/png", kind="image media")
                if part is not None:
                    parts.append(part)
            elif source_type == "url":
                url = _as_str(source.get("url")).strip()
                if url:
                    parts.append(await _media_part_from_url(url))
        elif block_type == "document":
            source = _as_dict(block.get("source"))
            if source.get("type") == "base64":
                part = _claude_base64_part(
                    source, "application/pdf", kind="document media"
                )
                if part is not None:
                    parts.append(part)
            elif source.get("type") == "url":
                url = _as_str(source.get("url")).strip()
                if url:
                    parts.append(await _media_part_from_url(url))
            elif source.get("type") == "file":
                # Files API 引用（file_id）不支持：静默丢弃会让客户端误以为
                # 文档已附上，至少留一条可排查的日志。
                logger.warning(
                    "Claude document source type=file（Files API file_id）"
                    "不被支持，已丢弃该块：file_id=%s",
                    _as_str(source.get("file_id")) or "(空)",
                )
        elif block_type == "tool_use":
            name = _as_str(block.get("name")) or "unknown"
            tool_input = block.get("input")
            parts.append(
                GeminiPart(
                    thoughtSignature=_read_signature(block),
                    functionCall=GeminiFunctionCall(
                        name=name,
                        args=tool_input if tool_input is not None else {},
                        id=_as_str(block.get("id")) or None,
                    ),
                )
            )
        elif block_type == "tool_result":
            tool_use_id = _as_str(block.get("tool_use_id"))
            name = tool_name_by_id.get(tool_use_id, "unknown")
            payload: dict[str, object] = {}
            extra_parts: list[GeminiPart] = []
            result_content = block.get("content")
            if isinstance(result_content, str):
                payload = {"result": result_content}
            elif isinstance(result_content, list):
                texts: list[str] = []
                for item in result_content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        texts.append(_as_str(item.get("text")))
                    elif item.get("type") == "image":
                        source = _as_dict(item.get("source"))
                        if source.get("type") == "base64":
                            part = _claude_base64_part(
                                source, "image/png", kind="tool_result media"
                            )
                            if part is not None:
                                extra_parts.append(part)
                joined = "\n".join(t for t in texts if t)
                payload = {"result": joined} if joined else {}
            if block.get("is_error") is True:
                payload["is_error"] = True
            parts.append(
                GeminiPart(
                    functionResponse=GeminiFunctionResponse(
                        name=name, response=payload, id=tool_use_id or None
                    )
                )
            )
            parts.extend(extra_parts)
        else:
            logger.debug("忽略不支持的 Claude content 块类型: %s", block_type)
    return parts


def _claude_system_parts(system: object) -> list[GeminiPart]:
    """Claude system（str 或块数组）→ systemInstruction parts。"""
    if isinstance(system, str):
        return [GeminiPart(text=system)] if system else []
    if not isinstance(system, list):
        return []
    parts: list[GeminiPart] = []
    for block in system:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(GeminiPart(text=_as_str(block.get("text"))))
    return parts


def _claude_tools_to_gemini(
    tools: list[dict[str, object]] | None,
) -> list[GeminiTool] | None:
    if tools is None:
        return None
    if not tools:
        return []
    declarations: list[dict[str, object]] = []
    for tool in tools:
        # 只转换客户端自定义工具（无 type 或 type=custom）；
        # web_search / bash 等服务端工具不误声明为 function，否则模型会
        # 尝试调用一个实际不存在的函数
        tool_type = tool.get("type")
        if tool_type is not None and tool_type != "custom":
            logger.debug("跳过非 custom 类型的 Claude 工具: %s", tool_type)
            continue
        name = _as_str(tool.get("name"))
        if not name:
            continue
        decl: dict[str, object] = {"name": name}
        description = tool.get("description")
        if isinstance(description, str) and description:
            decl["description"] = description
        input_schema = tool.get("input_schema")
        if isinstance(input_schema, dict) and input_schema:
            decl["parameters"] = input_schema
        declarations.append(decl)
    if not declarations:
        return []
    return [GeminiTool(functionDeclarations=declarations)]


def _claude_tool_choice_to_config(
    tool_choice: dict[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(tool_choice, dict):
        return None
    # Claude tool_choice 可带 disable_parallel_tool_use（部分 harness 在
    # "必须单工具"流程里置 true）：上游无强制串行开关，忽略并告警（与
    # OpenAI 侧 parallel_tool_calls=false 同策略），静默忽略会让排障时
    # 误以为串行已生效。
    if tool_choice.get("disable_parallel_tool_use") is True:
        logger.warning(
            "tool_choice.disable_parallel_tool_use 无法在上游强制串行，已忽略该提示"
        )
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return {"functionCallingConfig": {"mode": "AUTO"}}
    if choice_type == "any":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if choice_type == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if choice_type == "tool":
        name = _as_str(tool_choice.get("name"))
        if name:
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [name],
                }
            }
    return None


def _claude_base64_part(
    source: dict[str, object], default_mime: str, *, kind: str
) -> GeminiPart | None:
    """Claude base64 source 块 → inlineData part（缺 data 返回 None）。"""
    media_type = _as_str(source.get("media_type")) or default_mime
    data = _as_str(source.get("data"))
    if not data:
        return None
    _assert_media_size(data, kind=kind)
    return GeminiPart(inlineData=GeminiInlineData(mimeType=media_type, data=data))


def _claude_usage_payload(usage: dict[str, int]) -> dict[str, object]:
    """统一 Gemini token 计数 → Claude usage（input 剔除缓存命中部分）。"""
    payload: dict[str, object] = {
        "input_tokens": max(0, usage["prompt"] - usage["cached"]),
        "output_tokens": usage["completion"],
    }
    if usage["cached"]:
        payload["cache_read_input_tokens"] = usage["cached"]
    return payload


def _claude_thinking_to_level(thinking: dict[str, object] | None) -> str | None:
    """Claude thinking.budget_tokens → Gemini 思考等级（档位兼容由管线自动吸附）。

    预算→档位映射：≤2048 LOW / ≤8192 MEDIUM / ≤32768 HIGH / >32768 VERY_HIGH
    （Anthropic budget_tokens 上限 64k，超大预算视为冲顶档诉求；模型目录
    不支持 5 档时由吸附链路自动降档，如 carbon [1,2,3] 降回 HIGH）。
    """
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return None
    budget = _as_int(thinking.get("budget_tokens"))
    if budget is None or budget <= 0:
        return "HIGH"
    if budget <= 2048:
        return "LOW"
    if budget <= 8192:
        return "MEDIUM"
    if budget <= 32768:
        return "HIGH"
    return "VERY_HIGH"


def _claude_effort_word(
    thinking: dict[str, object] | None,
    output_config: dict[str, object] | None,
) -> str | None:
    """归一化 Claude effort 参数（管线档位词；无法识别返回 None）。

    位置优先级（高 → 低）：
    1. ``output_config.effort`` —— Anthropic 官方位置（2026-11 effort 协议），
       Claude Code 的 Low/Medium/High/Max 思考强度选择器即此形态；
    2. ``thinking.effort`` —— 个别网关/中转的变体位置。

    词表与 OpenAI 兼容层共用（normalize_reasoning_effort_word）：
    ``max → xhigh``（顶格思考档）、``very_high → very_high``、
    ``low/medium/high/minimal`` 原样、``none/off → none``（显式关闭）。
    adaptive thinking（``thinking: {"type": "adaptive"}``）本身不携带档位，
    是否顶格由 effort 决定——这正是「开 Max 却没到 xhigh」的修复点。
    """
    from aistudio_api.application.compat_openai import normalize_reasoning_effort_word

    candidates: list[object] = []
    if isinstance(output_config, dict):
        candidates.append(output_config.get("effort"))
    if isinstance(thinking, dict):
        candidates.append(thinking.get("effort"))
    for raw in candidates:
        word = normalize_reasoning_effort_word(raw)
        if word is not None:
            return word
        if isinstance(raw, str) and raw.strip():
            logger.warning("忽略无法识别的 effort 参数: %r", raw)
    return None


@media_session()
async def claude_to_gemini_request(
    req: ClaudeMessagesRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """Claude Messages 请求 → (Gemini 请求, 解析后的模型名)。"""
    resolved_model = resolve_compat_model(req.model, channel="claude")
    contents: list[GeminiContent] = []
    tool_name_by_id: dict[str, str] = {}

    # 第一遍：登记 assistant tool_use 的 id → name 映射（tool_result 反查用）
    for message in req.messages:
        if message.role.strip().lower() != "assistant":
            continue
        for block in message.content if isinstance(message.content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                call_id = _as_str(block.get("id"))
                name = _as_str(block.get("name"))
                if call_id and name:
                    tool_name_by_id[call_id] = name

    for message in req.messages:
        role = "model" if message.role.strip().lower() == "assistant" else "user"
        parts = await _claude_blocks_to_parts(
            message.content,
            tool_name_by_id=tool_name_by_id,
        )
        contents.append(GeminiContent(role=role, parts=parts or [GeminiPart(text="")]))

    generation_config: dict[str, object] = {}
    if req.temperature is not None:
        generation_config["temperature"] = req.temperature
    if req.top_p is not None:
        generation_config["topP"] = req.top_p
    if req.top_k is not None:
        generation_config["topK"] = req.top_k
    if req.max_tokens is not None:
        generation_config["maxOutputTokens"] = req.max_tokens
    if req.stop_sequences:
        # 过滤空串停止词：空 stopSequences 条目会被上游 400 拒绝
        # （与 OpenAI 侧 stop 处理一致）
        stop_list = [s for s in req.stop_sequences if isinstance(s, str) and s]
        if stop_list:
            generation_config["stopSequences"] = stop_list
    # 思考强度归一化：显式 effort（output_config.effort / thinking.effort，
    # Claude Code 的 Low/Medium/High/Max 选择器）优先于 budget_tokens 推断
    # 档位——二者并存时 effort 才是用户的显式选择，budget 只剩兜底语义。
    # effort 走 reasoningEffort 通道，与 OpenAI 兼容层共用同一管线（
    # normalize_reasoning_effort_word：max → xhigh 顶格档、none → 显式关闭，
    # 档位吸附按模型目录自动升降）。仅 budget 时维持既有 thinkingConfig 路径。
    effort_word = _claude_effort_word(req.thinking, req.output_config)
    thinking_level = _claude_thinking_to_level(req.thinking)
    if effort_word is not None:
        generation_config["reasoningEffort"] = effort_word
    elif thinking_level is not None:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    elif isinstance(req.thinking, dict) and req.thinking.get("type") == "disabled":
        generation_config["thinkingConfig"] = {"thinkingLevel": "NONE"}

    system_parts = _claude_system_parts(req.system)
    gemini_req = GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=(
            GeminiContent(role="user", parts=system_parts) if system_parts else None
        ),
        tools=_claude_tools_to_gemini(req.tools),
        toolConfig=_claude_tool_choice_to_config(req.tool_choice),
        generationConfig=(
            GeminiGenerationConfig.model_validate(generation_config)
            if generation_config
            else None
        ),
    )
    return gemini_req, resolved_model


def _coerce_tool_args(args: object) -> dict[str, object]:
    """functionCall args → 合法 dict 载荷（Claude tool_use.input 必须为对象）。

    上游 wire 层会以 JSON 字符串形式分片下发参数（``arguments`` 路径），
    兼容层拿到的可能是字符串片段：完整合法 JSON 则解析；解析失败（分片
    残段）兜底为 ``{"input": <原文>}``，绝不能把裸字符串直接塞进协议
    必填 object 的字段。
    """
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return {"input": args}
    return {}


def _claude_stop_reason(
    finish_reason: str | None,
    *,
    has_tool_use: bool,
) -> tuple[str, str | None]:
    """Gemini finishReason → Claude stop_reason 纯映射；停止序列命中由调用方
    （StopSequenceBuffer 的 stop_hit）先行判定，不再具备重复扫描语义。"""
    raw = finish_reason or "OTHER"
    # 安全/异常拦截优先于 tool_use，避免把被风控终止的响应误报成正常工具调用。
    if raw in _CLAUDE_REFUSAL_FINISH:
        return "refusal", None
    if raw == "MAX_TOKENS":
        return "max_tokens", None
    # OTHER = 上游流被截断（streaming.py 无 finishReason 时下发 OTHER），
    # 伪装成 end_turn 会让客户端把残缺回答当完整答案继续跑。
    if raw != "STOP":
        return "max_tokens", None
    if has_tool_use:
        return "tool_use", None
    return "end_turn", None


def build_claude_message(
    response: GeminiGenerateContentResponse,
    requested_model: str,
    stop_sequences: list[str] | None,
) -> dict[str, object]:
    """Gemini 完整响应 → Claude Message 对象。"""
    finish_reason_raw: str | None = None
    candidates = response.candidates or []
    if candidates:
        finish_reason_raw = candidates[0].finishReason

    blocks: list[dict[str, object]] = []
    has_tool_use = False
    for part in _iter_response_parts(response):
        text = getattr(part, "text", None)
        if isinstance(text, str):
            is_thought = getattr(part, "thought", None) is True
            signature = part_payload_signature(part, "text") if is_thought else ""
            if is_thought and (text or _is_signed_empty_thought(text, signature)):
                # A signed Part must match precisely one output block.
                if (
                    not signature
                    and blocks
                    and blocks[-1].get("type") == "thinking"
                    and not blocks[-1].get("signature")
                ):
                    blocks[-1]["thinking"] = _as_str(blocks[-1].get("thinking")) + text
                else:
                    blocks.append(
                        {
                            "type": "thinking",
                            "thinking": text,
                            "signature": _claude_signature(signature),
                        }
                    )
            elif not is_thought and text:
                blocks.append({"type": "text", "text": text})
        inline = getattr(part, "inlineData", None)
        if inline is not None:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": inline.mimeType,
                        "data": inline.data,
                    },
                }
            )
        code = _code_text(
            getattr(part, "executableCode", None),
            getattr(part, "codeExecutionResult", None),
        )
        if code:
            blocks.append({"type": "text", "text": code})
        function_call = getattr(part, "functionCall", None)
        if function_call is not None and finish_reason_raw == "STOP":
            has_tool_use = True
            signature = part_payload_signature(part, "functionCall")
            call_id = function_call.id or f"toolu_{secrets.token_hex(12)}"
            args = _coerce_tool_args(function_call.args)
            if function_call.id is None:
                # 上游未带 call id：合成 toolu_ id 发生在 _iter_response_parts
                # 的签名记忆化之后，缓存里没有该 id 的条目——客户端丢失
                # extra_content 载体（Claude Code TS 侧可能剥离未知字段）时
                # 按 id 回查必然 miss，只能退化为 SKIP 占位。补记一条，与
                # 流式路径（coalesce 先定 id、recorder 后记忆化）对齐。
                # remember 内部会校验签名合法性，无效签名静默跳过。
                _remember_function_signature(
                    call_id, function_call.name, args, signature
                )
            blocks.append(
                {
                    **_signature_extra(signature),
                    "type": "tool_use",
                    "id": call_id,
                    "name": function_call.name,
                    "input": args,
                }
            )

    stops = StopSequenceBuffer(stop_sequences)
    filtered: list[dict[str, object]] = []

    def flush_pending() -> None:
        tail = stops.feed("", final=True)
        if tail:
            if filtered and filtered[-1].get("type") == "text":
                filtered[-1]["text"] = _as_str(filtered[-1].get("text")) + tail
            else:
                filtered.append({"type": "text", "text": tail})

    for block in blocks:
        if stops.hit is not None:
            break
        if block.get("type") == "text":
            text = stops.feed(_as_str(block.get("text")))
            if text:
                filtered.append({**block, "text": text})
        else:
            flush_pending()
            filtered.append(block)
    flush_pending()
    blocks = filtered
    if stops.hit is not None and finish_reason_raw == "STOP":
        stop_reason, stop_seq = "stop_sequence", stops.hit
    else:
        stop_reason, stop_seq = _claude_stop_reason(
            finish_reason_raw,
            has_tool_use=has_tool_use,
        )
    usage_payload = _claude_usage_payload(_usage_from_gemini(response.usageMetadata))
    return {
        "id": f"msg_{secrets.token_hex(12)}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": stop_seq,
        "usage": usage_payload,
    }


class _ClaudeStreamEmitter:
    """Claude SSE block lifecycle; calls are normalized by coalesce_tool_frames."""

    def __init__(self, message_id: str, model: str) -> None:
        self.message_id = message_id
        self.model = model
        self.index = -1
        self.open_kind: str | None = None
        self.has_tool_use = False
        self.finish_reason_raw: str | None = None
        self.usage_frame: dict[str, object] | None = None
        self.saw_error = False
        # 首帧 usageMetadata（若携带）：message_start 的 usage 据此携带真实
        # input_tokens / cache_read_input_tokens（Anthropic 官方行为——
        # message_start 即带真实输入计数；旧 SDK 只从 message_start 取
        # input，等 message_delta 的客户端会拿到 0）。无首帧计量时退回
        # 0/1 占位，终值仍由 message_delta usage 覆盖。
        self.start_usage: dict[str, object] | None = None
        # stop sequence 命中状态：命中后丢弃后续正文增量（Claude 语义是
        # 在 stop sequence 处截断文本，透传其后内容会让客户端拿到多余文本）
        self.stop_hit: str | None = None
        self.thinking_signature = ""

    def sse(self, event: str, payload: dict[str, object]) -> str:
        # separators：SSE 每帧省去 ", " 与 ": " 的空格（约 2-4% 字节/分配）
        return (
            f"event: {event}\ndata: "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n\n"
        )

    def message_start(self) -> str:
        usage: dict[str, object] = {"input_tokens": 0, "output_tokens": 1}
        if self.start_usage is not None:
            # 首帧计量：真实输入计数（input 剔除缓存命中部分，与 message_delta
            # 同口径）；首帧 output 计数为 0，按 Anthropic 惯例记 1。
            usage = _claude_usage_payload(_usage_from_gemini(self.start_usage))
            if not usage.get("output_tokens"):
                usage["output_tokens"] = 1
        return self.sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": usage,
                },
            },
        )

    def ping(self) -> str:
        return self.sse("ping", {"type": "ping"})

    def _close_events(self) -> list[str]:
        events: list[str] = []
        if self.open_kind == "thinking" and self.thinking_signature:
            events.append(
                self.sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.index,
                        "delta": {
                            "type": "signature_delta",
                            "signature": _claude_signature(self.thinking_signature),
                        },
                    },
                )
            )
        if self.open_kind is not None:
            events.append(
                self.sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self.index},
                )
            )
            self.open_kind = None
        return events

    def _ensure_block(self, kind: str) -> list[str]:
        events: list[str] = []
        if self.open_kind == kind:
            return events
        events += self._close_events()
        self.index += 1
        block: dict[str, object]
        if kind == "thinking":
            self.thinking_signature = ""
            block = {"type": "thinking", "thinking": "", "signature": ""}
        else:
            block = {"type": "text", "text": ""}
        events.append(
            self.sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.index,
                    "content_block": block,
                },
            )
        )
        self.open_kind = kind
        return events

    def thinking_delta(self, text: str, signature: str = "") -> list[str]:
        events: list[str] = []
        if self.open_kind == "thinking" and (signature or self.thinking_signature):
            events += self._close_events()
        events += self._ensure_block("thinking")
        self.thinking_signature = signature
        if text:
            events.append(
                self.sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.index,
                        "delta": {"type": "thinking_delta", "thinking": text},
                    },
                )
            )
        return events

    def text_delta(self, text: str) -> list[str]:
        events = self._ensure_block("text")
        events.append(
            self.sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return events

    def tool_use(
        self, call_id: str, name: str, args: object, signature: str = ""
    ) -> list[str]:
        """Emit a complete call normalized by the shared stream pipeline."""
        events = self._close_events()
        self.index += 1
        self.has_tool_use = True
        events.append(
            self.sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.index,
                    "content_block": {
                        **_signature_extra(signature),
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": {},
                    },
                },
            )
        )
        events.append(
            self.sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            args, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                },
            )
        )
        events.append(
            self.sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.index},
            )
        )
        return events

    def image(self, media_type: str, data: str) -> list[str]:
        events = self._close_events()
        self.index += 1
        events.append(
            self.sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.index,
                    "content_block": {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        },
                    },
                },
            )
        )
        events.append(
            self.sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.index},
            )
        )
        return events

    def error(self, status: int, message: str) -> list[str]:
        self.saw_error = True
        events = self._close_events()
        events.append(
            self.sse(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": _CLAUDE_ERROR_TYPE.get(status, "api_error"),
                        "message": message or "Upstream error",
                    },
                },
            )
        )
        return events

    def finish(self) -> list[str]:
        events = self._close_events()
        if self.saw_error:
            return events
        if self.stop_hit is not None and self.finish_reason_raw == "STOP":
            stop_reason: str = "stop_sequence"
            stop_seq: str | None = self.stop_hit
        else:
            stop_reason, stop_seq = _claude_stop_reason(
                self.finish_reason_raw,
                has_tool_use=self.has_tool_use,
            )
        usage_payload = _claude_usage_payload(_usage_from_gemini(self.usage_frame))
        events.append(
            self.sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": stop_seq},
                    "usage": usage_payload,
                },
            )
        )
        events.append(self.sse("message_stop", {"type": "message_stop"}))
        return events


async def claude_stream_adapter(
    source: AsyncIterable[object],
    *,
    requested_model: str,
    stop_sequences: list[str] | None,
    register_failure: Callable[[Callable[[str], list[str]]], None] | None = None,
) -> AsyncGenerator[str, None]:
    """Gemini SSE → Claude Messages SSE 事件流。"""
    emitter = _ClaudeStreamEmitter(f"msg_{secrets.token_hex(12)}", requested_model)
    stops = StopSequenceBuffer(stop_sequences)
    # 确定性关闭内层 SSE 生成器（3.6）：自身异常或客户端断连时立即释放上游连接
    async with _stream_adapter_lifecycle(
        source,
        lambda message: emitter.error(504, message),
        register_failure,
    ):
        # 先取首帧再发 message_start：Gemini 首帧一般携带 usageMetadata
        # （promptTokenCount），据此让 message_start 的 usage 带真实
        # input_tokens（Anthropic 官方语义；旧版 SDK 不合并 message_delta
        # 的 input 字段，等 message_start 的客户端会拿到 0 导致上下文
        # 用量统计失真）。首帧未带计量或流立即结束时退回占位值，终值仍
        # 由 message_delta usage 覆盖。等待窗口内 SSE 心跳注释保活。
        frames = _iter_gemini_sse_frames(source)
        first_frame: dict[str, object] | None = None
        pending_error: Exception | None = None
        try:
            first_frame = await anext(frames)
        except StopAsyncIteration:
            pass
        except Exception as exc:
            pending_error = exc
        if first_frame is not None:
            usage_meta = first_frame.get("usageMetadata")
            if isinstance(usage_meta, dict):
                emitter.start_usage = usage_meta

        yield emitter.message_start()
        yield emitter.ping()
        if pending_error is not None:
            logger.warning("Claude 流式转换异常: %s", pending_error)
            record_result(
                "error",
                error=(
                    "Stream conversion failed: "
                    f"{type(pending_error).__name__}: {pending_error}"
                ),
            )
            for event in emitter.error(
                500, f"{type(pending_error).__name__}: {pending_error}"
            ):
                yield event
            return

        async def _peeked_frames() -> AsyncGenerator[dict[str, object], None]:
            if first_frame is not None:
                yield first_frame
            async for frame in frames:
                yield frame

        try:
            async for frame in _peeked_frames():
                error_obj = frame.get("error")
                if isinstance(error_obj, dict):
                    code = _as_int(error_obj.get("code")) or 500
                    message = _as_str(error_obj.get("message")) or "Upstream error"
                    for event in emitter.error(code, message):
                        yield event
                    return
                candidate = _frame_candidate(frame)
                finish = _as_str(candidate.get("finishReason"))
                if finish:
                    emitter.finish_reason_raw = finish
                usage_metadata = frame.get("usageMetadata")
                if isinstance(usage_metadata, dict):
                    emitter.usage_frame = usage_metadata
                for part in _frame_parts(candidate):
                    if stops.hit is not None:
                        continue
                    if (
                        part.get("functionCall")
                        or part.get("inlineData")
                        or part.get("thought") is True
                    ):
                        # A retained text prefix belongs before this next block.
                        tail = stops.feed("", final=True)
                        # 边界冲刷同样可能恰好补完停止序列：立即同步命中状态，
                        # 否则 finish() 读不到 stop_hit，会按 finish_reason 推导出
                        # tool_use/end_turn 等与「正文已截断」矛盾的终止元数据。
                        emitter.stop_hit = stops.hit
                        if tail:
                            for event in emitter.text_delta(tail):
                                yield event
                    function_call = _as_dict(part.get("functionCall"))
                    if function_call:
                        call_id = _as_str(function_call.get("id"))
                        if not call_id:
                            call_id = f"toolu_{secrets.token_hex(12)}"
                        name = _as_str(function_call.get("name")) or "unknown"
                        args = function_call.get("args")
                        for event in emitter.tool_use(
                            call_id,
                            name,
                            args,
                            part_payload_signature(part, "functionCall"),
                        ):
                            yield event
                        continue
                    inline = _as_dict(part.get("inlineData"))
                    if inline:
                        mime = _as_str(inline.get("mimeType")) or "image/png"
                        data = _as_str(inline.get("data"))
                        if data:
                            for event in emitter.image(mime, data):
                                yield event
                        continue
                    text = part.get("text")
                    if isinstance(text, str):
                        if part.get("thought") is True:
                            signature = part_payload_signature(part, "text")
                            if text or _is_signed_empty_thought(text, signature):
                                for event in emitter.thinking_delta(text, signature):
                                    yield event
                        elif text:
                            # 上游错误兑底文本（_error_text_payload）不是
                            # 正文：随后的结构化错误事件已携带同等信息，重复
                            # 下发会混进 assistant 正文（与 Responses 适配器
                            # 同款过滤）。
                            if text.startswith(_ERROR_TEXT_PREFIX):
                                continue
                            # stop sequence 命中后丢弃后续正文（Claude 语义是
                            # 文本在 stop sequence 处截断，不透传其后内容）；
                            # 命中点在本帧中间时，命中点之前的文本仍需下发
                            text = stops.feed(text)
                            emitter.stop_hit = stops.hit
                            if not text:
                                continue
                            for event in emitter.text_delta(text):
                                yield event
        except Exception as exc:
            logger.warning("Claude 流式转换异常: %s", exc)
            record_result(
                "error", error=f"Stream conversion failed: {type(exc).__name__}: {exc}"
            )
            for event in emitter.error(500, f"{type(exc).__name__}: {exc}"):
                yield event
            return
        tail = stops.feed("", final=True)
        # 收尾冲刷若恰好补完停止序列，同样要同步命中状态（见上方边界冲刷）。
        emitter.stop_hit = stops.hit
        if tail:
            for event in emitter.text_delta(tail):
                yield event
        for event in emitter.finish():
            yield event
