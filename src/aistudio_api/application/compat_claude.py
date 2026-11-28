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
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application.compat_errors import (
    _CLAUDE_REFUSAL_FINISH,
    _claude_signature,
    _read_claude_signature,
    claude_error_payload,
)
from aistudio_api.application.compat_result_owners import index_call_name, result_part
from aistudio_api.application.compat_shared import (
    _as_dict,
    _as_int,
    _as_str,
    _assert_media_size,
    _code_text,
    _convert_items_concurrently,
    _dropped_part_reason,
    _frame_candidate,
    _frame_parts,
    _history_call_part,
    _is_signed_empty_thought,
    _iter_gemini_sse_frames,
    _iter_response_parts,
    _media_part_from_url,
    _read_signature,
    _read_signature_with_source,
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
from aistudio_api.infrastructure.signature_trace import (
    get_signature_trace,
    set_trace_protocol,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.claude")


def _claude_document_text(source: dict[str, object]) -> str | None:
    """Read literal document text without converting block arrays or fetching URLs."""
    if source.get("type") == "content":
        content = source.get("content")
        return content if isinstance(content, str) else None
    data = source.get("data")
    if (
        source.get("type") == "text"
        and source.get("media_type") == "text/plain"
        and isinstance(data, str)
    ):
        return data
    return None


def _reject_unsupported_user_block(
    block: object, message_index: int, block_index: int, item_index: int | None = None
) -> None:
    if not isinstance(block, dict):
        return
    kind = block.get("type")
    if kind in ("document", "image"):
        source = _as_dict(block.get("source"))
        if source.get("type") != "file":
            return
        file_id = _as_str(source.get("file_id"))
        if not file_id or file_id.isspace():
            return
    elif kind != "search_result":
        return
    path = f"messages[{message_index}].content[{block_index}]"
    if item_index is not None:
        path += f".content[{item_index}]"
    if kind == "search_result":
        raise ValueError(
            f"{path}: search_result is not supported; use plain text only if "
            "source and citation metadata are not needed"
        )
    alternative = (
        "an inline image source with type='base64', data, and the image's "
        "media_type (for example 'image/png')"
        if kind == "image"
        else "an inline PDF (type='base64', media_type='application/pdf', data) "
        "or a plain-text document (type='text', media_type='text/plain', data)"
    )
    raise ValueError(
        f"{path}.source.file_id is not supported: this gateway cannot resolve "
        f"Anthropic Files API references; use {alternative}"
    )


def _check_user_blocks(content: list[dict[str, object]], message_index: int) -> None:
    """Check unsupported inputs in block order without walking opaque data."""
    for block_index, block in enumerate(content):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            items = block.get("content")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    _reject_unsupported_user_block(
                        item, message_index, block_index, item_index
                    )
        else:
            _reject_unsupported_user_block(block, message_index, block_index)


def _opaque_assistant_history_field(block: dict[str, object]) -> str | None:
    """Identify imported thought blocks that would otherwise disappear on replay."""
    kind = block.get("type")
    if kind == "redacted_thinking":
        data = _as_str(block.get("data"))
        if data and not data.isspace():
            return "redacted_thinking.data"
    elif kind == "thinking" and block.get("thinking") == "":
        # Even whitespace is visible content. Only a truly empty block needs
        # the gateway's own envelope to replay without losing its history.
        signature = _as_str(block.get("signature"))
        if (
            signature
            and not signature.isspace()
            and _read_claude_signature(signature) is None
        ):
            return "thinking.signature"
    return None


async def _claude_blocks_to_parts(
    content: object,
    *,
    tool_name_by_id: dict[str, str | None],
    is_user_content: bool = False,
) -> list[GeminiPart]:
    """Claude content（str 或块数组）→ Gemini parts。

    支持 text / thinking / image / document / tool_use / tool_result 块；
    tool_result 中的内联图片及用户返回的 PDF 展开为紧随其后的独立 inlineData parts。
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
                blocks,
                tool_name_by_id=tool_name_by_id,
                is_user_content=is_user_content,
            ),
        )
    trace = get_signature_trace()
    parts: list[GeminiPart] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(GeminiPart(text=_as_str(block.get("text"))))
        elif block_type == "thinking":
            thinking = block.get("thinking")
            raw_sig = block.get("signature")
            signature = _read_claude_signature(raw_sig)
            if (
                isinstance(raw_sig, str)
                and raw_sig
                and signature is None
                and trace is not None
            ):
                trace.record_client_drop(
                    carrier="thought",
                    raw_signature=raw_sig,
                    source="client_claude_envelope",
                    note="foreign_claude_signature_ignored",
                )
            if isinstance(thinking, str) and (
                thinking or _is_signed_empty_thought(thinking, signature)
            ):
                thought_part = GeminiPart(
                    text=thinking, thought=True, thoughtSignature=signature
                )
                if trace is not None and signature:
                    trace.tag_inbound_source(thought_part, "client_claude_envelope")
                parts.append(thought_part)
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
            if is_user_content and (text := _claude_document_text(source)) is not None:
                parts.append(GeminiPart(text=text))
            elif source.get("type") == "base64":
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
                # Valid user references were rejected in preflight. Never log IDs.
                logger.warning("Claude document source type=file 不被支持，已丢弃该块")
        elif block_type == "tool_use":
            call_id = _as_str(block.get("id"))
            tool_input = block.get("input")
            sig, sig_source = _read_signature_with_source(block)
            parts.append(
                _history_call_part(
                    block.get("name"),
                    tool_input if tool_input is not None else {},
                    call_id,
                    sig,
                    sig_source,
                    trace,
                    args_rewritten=tool_input is None,
                )
            )
        elif block_type == "tool_result":
            tool_use_id = _as_str(block.get("tool_use_id"))
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
                    elif item.get("type") == "document" and is_user_content:
                        source = _as_dict(item.get("source"))
                        text = _claude_document_text(source)
                        if text is not None:
                            texts.append(text)
                        elif (
                            source.get("type") == "base64"
                            and source.get("media_type") == "application/pdf"
                        ):
                            part = _claude_base64_part(
                                source, "application/pdf", kind="tool_result media"
                            )
                            if part is not None:
                                extra_parts.append(part)
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
                result_part(
                    tool_name_by_id,
                    GeminiFunctionResponse(
                        name="unknown", response=payload, id=tool_use_id or None
                    ),
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
    set_trace_protocol("claude")
    resolved_model = resolve_compat_model(req.model, channel="claude")
    contents: list[GeminiContent] = []
    tool_name_by_id: dict[str, str | None] = {}

    # Validate unsupported user blocks and assistant history before *any* media
    # fetch or charge. Also index tool_use owners for later tool_result blocks.
    for message_index, message in enumerate(req.messages):
        if not isinstance(message.content, list):
            continue
        message_role = message.role.strip().lower()
        if message_role == "user":
            _check_user_blocks(message.content, message_index)
            continue
        if message_role != "assistant":
            continue
        for block_index, block in enumerate(message.content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                call_id = _as_str(block.get("id"))
                name = _as_str(block.get("name"))
                index_call_name(tool_name_by_id, call_id, name)
            elif field := _opaque_assistant_history_field(block):
                raise ValueError(
                    f"messages[{message_index}].content[{block_index}].{field} "
                    "contains opaque assistant history that cannot be replayed here; "
                    "continue with the original provider, or supply only the visible "
                    "transcript if you intend to omit that history"
                )

    for message in req.messages:
        message_role = message.role.strip().lower()
        role = "model" if message_role == "assistant" else "user"
        parts = await _claude_blocks_to_parts(
            message.content,
            tool_name_by_id=tool_name_by_id,
            is_user_content=message_role == "user",
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


def _inline_output_notice(media_type: str) -> str:
    kind = "Image" if media_type.lower().startswith("image/") else "Inline media"
    return (
        f"[{kind} output is unavailable in the Claude Messages format. "
        "Use the native Gemini endpoint for binary output.]"
    )


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

    # Strings are gateway notices. Lists collect consecutive unsigned thought
    # fragments; join each run once, without copying its prefix on every Part.
    blocks: list[dict[str, object] | str | list[str]] = []
    has_tool_use = False
    trace = get_signature_trace()
    for part in _iter_response_parts(response):
        raw_sig = _as_str(getattr(part, "thoughtSignature", None))
        emitted_sig = False
        text = getattr(part, "text", None)
        if isinstance(text, str):
            is_thought = getattr(part, "thought", None) is True
            signature = part_payload_signature(part, "text") if is_thought else ""
            if is_thought and (text or _is_signed_empty_thought(text, signature)):
                # A signed Part must match precisely one output block.
                previous = blocks[-1] if blocks else None
                if not signature:
                    if isinstance(previous, list):
                        previous.append(text)
                    else:
                        blocks.append([text])
                else:
                    blocks.append(
                        {
                            "type": "thinking",
                            "thinking": text,
                            "signature": _claude_signature(signature),
                        }
                    )
                    emitted_sig = True
            elif not is_thought and text:
                blocks.append({"type": "text", "text": text})
        inline = getattr(part, "inlineData", None)
        if inline is not None and inline.data:
            blocks.append(_inline_output_notice(inline.mimeType))
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
                # Record synthesized call_id metadata for legacy lookup compatibility.
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
            emitted_sig = bool(signature)
        if raw_sig and not emitted_sig and trace is not None:
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

    stops = StopSequenceBuffer(stop_sequences)
    filtered: list[dict[str, object]] = []
    text_block_open = False

    def flush_pending() -> None:
        tail = stops.feed("", final=True)
        if tail:
            if text_block_open:
                filtered[-1]["text"] = _as_str(filtered[-1].get("text")) + tail
            else:
                filtered.append({"type": "text", "text": tail})

    for block in blocks:
        if stops.hit is not None:
            if isinstance(block, dict) and trace is not None:
                if block.get("type") == "thinking":
                    dropped = _read_claude_signature(block.get("signature"))
                    if dropped:
                        trace.record_client_out_drop(
                            carrier="thought",
                            signature=dropped,
                            reason="stop_truncated",
                        )
                elif block.get("type") == "tool_use":
                    dropped = _read_signature(block)
                    if dropped:
                        trace.record_client_out_drop(
                            carrier="function_call",
                            signature=dropped,
                            reason="stop_truncated",
                            call_id=_as_str(block.get("id")),
                            call_name=_as_str(block.get("name")),
                        )
            continue
        if isinstance(block, (str, list)):
            flush_pending()
            filtered.append(
                {"type": "text", "text": block}
                if isinstance(block, str)
                else {"type": "thinking", "thinking": "".join(block), "signature": ""}
            )
            text_block_open = False
        elif block.get("type") == "text":
            text = stops.feed(_as_str(block.get("text")))
            if text:
                filtered.append({**block, "text": text})
                text_block_open = True
        else:
            flush_pending()
            filtered.append(block)
            text_block_open = False
            if trace is not None:
                if block.get("type") == "thinking":
                    emitted = _read_claude_signature(block.get("signature"))
                    if emitted:
                        trace.record_client_out_sig(
                            carrier="thought",
                            wire_field="thinking.signature",
                            signature=emitted,
                        )
                elif block.get("type") == "tool_use":
                    emitted = _read_signature(block)
                    if emitted:
                        trace.record_client_out_sig(
                            carrier="function_call",
                            wire_field="extra_content",
                            signature=emitted,
                            call_id=_as_str(block.get("id")),
                            call_name=_as_str(block.get("name")),
                        )
    flush_pending()
    if trace is not None:
        trace.emit_inbound_log()
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
        "content": filtered or [{"type": "text", "text": ""}],
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

    @staticmethod
    def sse(event: str, payload: dict[str, object]) -> str:
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
            if (trace := get_signature_trace()) is not None:
                trace.record_client_out_sig(
                    carrier="thought",
                    wire_field="thinking.signature",
                    signature=self.thinking_signature,
                )
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
        if signature and (trace := get_signature_trace()) is not None:
            trace.record_client_out_sig(
                carrier="function_call",
                wire_field="extra_content",
                signature=signature,
                call_id=call_id,
                call_name=name,
            )
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

    def inline_output_notice(self, media_type: str) -> list[str]:
        # An image block is valid input, but not valid Messages output.
        # Keep the replacement text distinct from surrounding generated text.
        events = self._close_events()
        events += self.text_delta(_inline_output_notice(media_type))
        events += self._close_events()
        return events

    @classmethod
    def error_event(cls, status: int, message: str) -> str:
        return cls.sse(
            "error", claude_error_payload(status, message or "Upstream error")
        )

    def error(self, status: int, message: str) -> list[str]:
        self.saw_error = True
        return [*self._close_events(), self.error_event(status, message)]

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
        if (
            self.start_usage is not None
            and self.usage_frame is not None
            and (_as_int(self.start_usage.get("cachedContentTokenCount")) or 0) > 0
            and "cache_read_input_tokens" not in usage_payload
        ):
            # A missing field leaves the SDK accumulator's initial cache count
            # intact. Explicit zero clears it when the final meter says no hit.
            usage_payload["cache_read_input_tokens"] = 0
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
        # A deadline interrupts delivery. Queued blocks may never have reached
        # the client, so don't synthesize block stops or signature deltas here.
        lambda message: [_ClaudeStreamEmitter.error_event(504, message)],
        register_failure,
    ):
        # 只取首个可发布帧：本地流若已有输入计量，会附在该帧上；缓冲变体
        # 则已带终值。无计量时先用占位值，不为 usage 多等一帧。
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
            nonlocal first_frame
            if first_frame is not None:
                yield first_frame
                # The consumer has advanced; don't pin this payload until EOF.
                first_frame = None
            async for frame in frames:
                yield frame

        source_error: tuple[int, str] | None = None
        try:
            async for frame in _peeked_frames():
                error_obj = frame.get("error")
                if isinstance(error_obj, dict):
                    code = _as_int(error_obj.get("code")) or 500
                    message = _as_str(error_obj.get("message")) or "Upstream error"
                    source_error = code, message
                    break
                candidate = _frame_candidate(frame)
                finish = _as_str(candidate.get("finishReason"))
                if finish:
                    emitter.finish_reason_raw = finish
                usage_metadata = frame.get("usageMetadata")
                if isinstance(usage_metadata, dict):
                    emitter.usage_frame = usage_metadata
                for part in _frame_parts(candidate):
                    raw_sig = _as_str(part.get("thoughtSignature"))
                    if stops.hit is not None:
                        if raw_sig and (trace := get_signature_trace()) is not None:
                            carrier, _ = _dropped_part_reason(part)
                            trace.record_client_out_drop(
                                carrier=carrier,
                                signature=raw_sig,
                                reason="stop_truncated",
                            )
                        continue
                    function_call = _as_dict(part.get("functionCall"))
                    inline = _as_dict(part.get("inlineData"))
                    has_inline_output = bool(_as_str(inline.get("data")))
                    if (
                        function_call
                        or has_inline_output
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
                    if function_call:
                        call_id = _as_str(function_call.get("id"))
                        if not call_id:
                            call_id = f"toolu_{secrets.token_hex(12)}"
                        name = _as_str(function_call.get("name")) or "unknown"
                        args = function_call.get("args")
                        fc_sig = part_payload_signature(part, "functionCall")
                        if (
                            raw_sig
                            and not fc_sig
                            and (trace := get_signature_trace()) is not None
                        ):
                            trace.record_client_out_drop(
                                carrier="function_call",
                                signature=raw_sig,
                                reason="compound_part_unrepresentable",
                                call_id=call_id,
                                call_name=name,
                            )
                        for event in emitter.tool_use(
                            call_id,
                            name,
                            args,
                            fc_sig,
                        ):
                            yield event
                        continue
                    if has_inline_output:
                        if raw_sig and (trace := get_signature_trace()) is not None:
                            trace.record_client_out_drop(
                                carrier="inline_data",
                                signature=raw_sig,
                                reason="inline_data_unrepresentable",
                            )
                        mime = _as_str(inline.get("mimeType"))
                        for event in emitter.inline_output_notice(mime):
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
                                for event in emitter.thinking_delta(text, signature):
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
                            # stop sequence 命中后丢弃后续正文（Claude 语义是
                            # 文本在 stop sequence 处截断，不透传其后内容）；
                            # 命中点在本帧中间时，命中点之前的文本仍需下发
                            text = stops.feed(text)
                            emitter.stop_hit = stops.hit
                            if not text:
                                continue
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
            logger.warning("Claude 流式转换异常: %s", exc)
            record_result(
                "error", error=f"Stream conversion failed: {type(exc).__name__}: {exc}"
            )
            source_error = 500, f"{type(exc).__name__}: {exc}"
        # Only ordinary source exhaustion/failure reaches this point. Preserve
        # the literal tail before closing its block, never on downstream abort.
        tail = stops.feed("", final=True)
        # 收尾冲刷若恰好补完停止序列，同样要同步命中状态（见上方边界冲刷）。
        emitter.stop_hit = stops.hit
        if tail:
            for event in emitter.text_delta(tail):
                yield event
        if source_error is not None:
            for event in emitter.error(*source_error):
                yield event
            return
        for event in emitter.finish():
            yield event
        if (trace := get_signature_trace()) is not None:
            trace.emit_inbound_log()
