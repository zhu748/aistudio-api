"""OpenAI Chat Completions 协议兼容转换层。

自 compat_service.py 拆出：请求侧（Chat Completions → Gemini 请求模型）、
响应侧（Gemini 响应 / SSE 帧 → Chat Completion 对象与 chunk 流），以及
各生态客户端思考强度参数的归一化族（``reasoning_effort`` / ``reasoning.effort``
/ ``thinking`` / ``enable_thinking`` / ``chat_template_kwargs``）。

媒体管线、签名缓存、SSE 帧解析等双侧共用工具见 compat_shared；
错误码表与终结语义见 compat_errors。
"""

from __future__ import annotations

import json
import secrets
import time
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
from aistudio_api.api.schemas_compat import OpenAIChatCompletionRequest
from aistudio_api.application.compat_errors import (
    _GEMINI_TO_OPENAI_FINISH,
    _OPENAI_ERROR_TYPE,
)
from aistudio_api.application.compat_shared import (
    _as_dict,
    _as_int,
    _as_str,
    _assert_media_size,
    _code_text,
    _convert_items_concurrently,
    _dropped_part_reason,
    _file_block_to_part,
    _frame_candidate,
    _frame_parts,
    _is_signed_empty_thought,
    _iter_gemini_sse_frames,
    _iter_response_parts,
    _media_part_from_url,
    _read_signature_with_source,
    _signature_extra,
    _stream_adapter_lifecycle,
    _usage_from_gemini,
    decode_signature_envelope,
    resolve_compat_model,
)
from aistudio_api.application.media_session import has_remote_media, media_session
from aistudio_api.application.stream_utils import (
    StopSequenceBuffer,
    part_payload_signature,
)
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    remember_function_signature as _remember_function_signature,
    valid_thought_signature,
)
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.signature_trace import (
    ThoughtSignatureTrace,
    get_signature_trace,
    set_trace_protocol,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.openai")


def _openai_content_text(content: object) -> str:
    """提取 OpenAI content（str 或块数组）中的纯文本。

    多个文本块之间以空行分隔，避免无分隔符拼接导致相邻单词粘连
    （如 ``["You are a", "helpful assistant."]`` → ``"You are ahelpful..."``）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "\n\n".join(texts)
    return ""


def _openai_history_call(
    name: str,
    raw: object,
    call_id: str,
    signature: str | None,
    source: str,
    trace: ThoughtSignatureTrace | None,
    *,
    responses: bool = False,
) -> GeminiPart:
    """Preserve each API's argument fallback without treating it as a signed echo."""
    parsed = raw
    malformed_json = False
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            malformed_json = True
    # Chat also accepts array arguments; Responses has always required an
    # object and preserves malformed JSON text in a raw_arguments wrapper.
    exact = isinstance(parsed, dict) or (not responses and isinstance(parsed, list))
    if not exact:
        parsed = {"raw_arguments": raw} if responses and malformed_json else {}
        if signature and signature != SKIP_THOUGHT_SIGNATURE:
            if trace is not None:
                trace.record_client_drop(
                    carrier="function_call",
                    raw_signature=signature,
                    source=source,
                    call_id=call_id,
                    call_name=name,
                    note="rewritten_call_arguments",
                )
            signature = None
    call = GeminiFunctionCall(name=name, args=parsed, id=call_id or None)
    call._compat_args_rewritten = not exact
    part = GeminiPart(functionCall=call, thoughtSignature=signature)
    if trace is not None and signature and source:
        trace.tag_inbound_source(part, source)
    return part


async def _openai_parts_from_content(content: object) -> list[GeminiPart]:
    """OpenAI content（str 或多模态块数组）→ Gemini parts。"""
    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
    if len(content) > 1 and has_remote_media(content):
        return await _convert_items_concurrently(content, _openai_parts_from_content)
    parts: list[GeminiPart] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if block_type == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(GeminiPart(text=text))
        elif block_type == "image_url":
            image_url = _as_dict(item.get("image_url"))
            url = _as_str(image_url.get("url")).strip()
            if url:
                parts.append(await _media_part_from_url(url))
        elif block_type == "input_audio":
            audio = _as_dict(item.get("input_audio"))
            data = _as_str(audio.get("data"))
            if data:
                fmt = _as_str(audio.get("format")).lower() or "wav"
                _assert_media_size(data, kind="input_audio media")
                parts.append(
                    GeminiPart(
                        inlineData=GeminiInlineData(mimeType=f"audio/{fmt}", data=data)
                    )
                )
        elif block_type == "file":
            file_obj = _as_dict(item.get("file"))
            file_data = _as_str(file_obj.get("file_data"))
            part = _file_block_to_part(file_data, file_obj.get("filename"))
            if part is not None:
                parts.append(part)
        elif block_type == "refusal":
            refusal = item.get("refusal")
            if isinstance(refusal, str):
                parts.append(GeminiPart(text=refusal))
        else:
            logger.debug("忽略不支持的 OpenAI content 块类型: %s", block_type)
    return parts


def _openai_tool_content_to_response(content: object) -> dict[str, object]:
    """OpenAI tool 消息 content → Gemini functionResponse.response 载荷。"""
    if isinstance(content, list):
        texts = [
            _as_str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t)
    elif isinstance(content, str):
        text = content
    else:
        text = ""
    if text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"result": text}


def _openai_tools_to_gemini(
    tools: list[dict[str, object]] | None,
) -> list[GeminiTool] | None:
    """OpenAI/Responses function 工具声明 → Gemini functionDeclarations。

    兼容两种形态：Chat Completions 把 name/description/parameters 放在
    ``tool.function`` 子对象里，Responses API 直接放在 tool 顶层——两侧
    字段都做 fallback，只认单一形态会静默丢掉另一侧的参数 schema。
    空列表返回 ``[]`` 而非 None：``None`` 表示"未传 tools"（会触发
    default_tools 注入），``[]`` 表示客户端显式禁用工具。
    """
    if tools is None:
        return None
    # 显式空列表返回空列表（而非 None）：None 会触发 normalize 层注入
    # config.yaml 的 default_tools（Google Search 等），违背客户端
    # "显式禁用工具" 的意图
    if not tools:
        return []
    declarations: list[dict[str, object]] = []
    for tool in tools:
        # 只转换 function 工具；custom / web_search 等其他类型不误声明为函数
        tool_type = tool.get("type")
        if tool_type is not None and tool_type != "function":
            logger.debug("跳过非 function 类型的 OpenAI 工具: %s", tool_type)
            continue
        function = _as_dict(tool.get("function"))
        name = _as_str(function.get("name")) or _as_str(tool.get("name"))
        if not name:
            continue
        decl: dict[str, object] = {"name": name}
        description = function.get("description")
        if not (isinstance(description, str) and description):
            description = tool.get("description")
        if isinstance(description, str) and description:
            decl["description"] = description
        parameters = function.get("parameters")
        if not (isinstance(parameters, dict) and parameters):
            parameters = tool.get("parameters")
        if isinstance(parameters, dict) and parameters:
            decl["parameters"] = parameters
        # strict（OpenAI strict mode）透传：声明编码层据此把完整 JSON Schema
        # 附加到 description（wire 无法表达 strict 的闭合对象语义）
        strict = function.get("strict")
        if not isinstance(strict, bool):
            strict = tool.get("strict")
        if isinstance(strict, bool):
            decl["strict"] = strict
        declarations.append(decl)
    if not declarations:
        return []
    return [GeminiTool(functionDeclarations=declarations)]


def _openai_tool_choice_to_config(
    tool_choice: str | dict[str, object] | None,
    *,
    chat: bool = False,
) -> dict[str, object] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip().lower()
        mode_map = {"auto": "AUTO", "none": "NONE", "required": "ANY"}
        mode = mode_map.get(normalized)
        if mode is None:
            return None
        return {"functionCallingConfig": {"mode": mode}}
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "function":
            function = _as_dict(tool_choice.get("function"))
            name = _as_str(function.get("name")) or _as_str(tool_choice.get("name"))
            if name:
                return {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [name],
                    }
                }
        elif choice_type == "none":
            return {"functionCallingConfig": {"mode": "NONE"}}
        elif choice_type == "allowed_tools":
            # Chat nests this envelope; Responses and older Chat clients use
            # the flat form. The wire can restrict declarations, not force a call.
            names: list[str] = []
            raw_tools = tool_choice.get("tools")
            nested_chat = chat and "allowed_tools" in tool_choice
            if nested_chat:
                if "tools" in tool_choice:
                    raise ValueError(
                        "tool_choice cannot mix flat and nested allowed_tools"
                    )
                allowed = _as_dict(tool_choice.get("allowed_tools"))
                if allowed.get("mode") not in ("auto", "required"):
                    raise ValueError(
                        "tool_choice.allowed_tools.mode must be auto or required"
                    )
                raw_tools = allowed.get("tools")
            if not isinstance(raw_tools, list):
                if nested_chat:
                    raise ValueError("tool_choice.allowed_tools.tools must be a list")
                return None
            for entry in raw_tools:
                if nested_chat:
                    if not isinstance(entry, dict) or entry.get("type") != "function":
                        continue
                    name = _as_str(_as_dict(entry.get("function")).get("name"))
                elif isinstance(entry, dict):
                    fn = _as_dict(entry.get("function"))
                    name = _as_str(fn.get("name")) or _as_str(entry.get("name"))
                else:
                    name = _as_str(entry)
                if name:
                    names.append(name)
            if names:
                return {
                    "functionCallingConfig": {
                        "mode": "AUTO",
                        "allowedFunctionNames": names,
                    }
                }
            if nested_chat:
                raise ValueError(
                    "tool_choice.allowed_tools.tools must include a supported function"
                )
            return None
        elif choice_type in ("auto", "any", "required"):
            mode_map = {"auto": "AUTO", "any": "ANY", "required": "ANY"}
            return {
                "functionCallingConfig": {
                    "mode": mode_map.get(str(choice_type), "AUTO")
                }
            }
    return None


def _openai_response_format_to_config(
    response_format: dict[str, object] | None,
) -> dict[str, object]:
    """OpenAI response_format → Gemini responseMimeType / responseSchema。"""
    if not isinstance(response_format, dict):
        return {}
    format_type = response_format.get("type")
    if format_type == "json_object":
        return {"responseMimeType": "application/json"}
    if format_type == "json_schema":
        json_schema = _as_dict(response_format.get("json_schema"))
        config: dict[str, object] = {"responseMimeType": "application/json"}
        schema = json_schema.get("schema")
        if isinstance(schema, dict) and schema:
            config["responseSchema"] = schema
        return config
    return {}


def _parse_openai_reasoning_details(
    details: list[dict[str, object]],
    *,
    tool_call_ids: set[str],
    trace: ThoughtSignatureTrace | None,
    scalar_present: bool,
) -> tuple[list[tuple[str, str | None, str]], dict[str, str]]:
    """Extract Google-compatible thought parts and tool-call signatures from reasoning_details."""
    thoughts: list[tuple[str, str | None, str]] = []
    tool_sigs: dict[str, str] = {}
    for entry in details:
        if not isinstance(entry, dict):
            continue
        entry_type = _as_str(entry.get("type")).strip().lower()
        fmt = _as_str(entry.get("format")).strip().lower()
        is_google_fmt = not fmt or "google" in fmt or "gemini" in fmt
        if entry_type in ("reasoning.text", "reasoning.summary"):
            text = entry.get("text" if entry_type == "reasoning.text" else "summary")
            if not isinstance(text, str):
                continue  # Missing, null or non-text is not a signed empty string.
            raw_sig = _as_str(entry.get("signature"))
            sig: str | None = None
            source = ""
            if raw_sig:
                decoded = decode_signature_envelope(raw_sig)
                if scalar_present:
                    # Overriding an existing display value requires a scoped
                    # opaque value, not just bytes from an unspecified format.
                    candidate = decoded or (
                        raw_sig if fmt == "google-gemini-v1" else None
                    )
                    if candidate != SKIP_THOUGHT_SIGNATURE and valid_thought_signature(
                        candidate
                    ):
                        sig = candidate
                elif decoded:
                    sig = decoded
                elif is_google_fmt and valid_thought_signature(raw_sig):
                    sig = raw_sig
                if sig:
                    source = "client_reasoning_details"
                elif trace is not None:
                    trace.record_client_drop(
                        carrier="thought",
                        raw_signature=raw_sig,
                        source="client_reasoning_details",
                        note="foreign_reasoning_details_ignored",
                    )
            if text or _is_signed_empty_thought(text, sig):
                thoughts.append((text, sig, source))
        elif entry_type == "reasoning.encrypted":
            data = _as_str(entry.get("data"))
            if not data:
                continue
            decoded = decode_signature_envelope(data)
            candidate_sig: str | None = None
            if decoded:
                candidate_sig = decoded
            elif is_google_fmt and valid_thought_signature(data):
                candidate_sig = data
            else:
                if trace is not None:
                    trace.record_client_drop(
                        carrier="thought",
                        raw_signature=data,
                        source="client_reasoning_details",
                        call_id=_as_str(entry.get("id")),
                        note="foreign_reasoning_details_ignored",
                    )
                continue
            entry_id = _as_str(entry.get("id"))
            if entry_id and entry_id in tool_call_ids:
                tool_sigs.setdefault(entry_id, candidate_sig)
            elif not scalar_present and _is_signed_empty_thought("", candidate_sig):
                thoughts.append(("", candidate_sig, "client_reasoning_details"))
    return thoughts, tool_sigs


@media_session()
async def openai_to_gemini_request(
    req: OpenAIChatCompletionRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """OpenAI Chat Completions 请求 → (Gemini 请求, 解析后的模型名)。"""
    trace = set_trace_protocol("openai")
    resolved_model = resolve_compat_model(req.model, channel="openai")
    if req.n not in (None, 1):
        raise ValueError("Only n=1 is supported")
    if req.parallel_tool_calls is False:
        # OpenAI 官方语义是提示而非硬约束（不少框架默认发 false）；上游
        # 无法强制串行 ≠ 必须拒绝请求。接受+忽略+告警，与
        # responses 侧同策略。
        logger.warning("parallel_tool_calls=false 无法在上游强制串行，已忽略该提示")
    contents: list[GeminiContent] = []
    system_texts: list[str] = []
    tool_name_by_id: dict[str, str] = {}

    # 第一遍：先扫全部 assistant 消息建 tool_call_id → 函数名映射。
    # 与 Claude 侧一致的两遍扫描：容忍客户端把 tool 结果放在对应
    # assistant 消息之前（裁剪/乱序历史），避免函数名回退 "unknown"
    # 透传给上游 functionResponse 导致上游报错。
    for message in req.messages:
        if message.role.strip().lower() != "assistant":
            continue
        for tool_call in message.tool_calls or []:
            function = _as_dict(tool_call.get("function"))
            call_id = _as_str(tool_call.get("id"))
            name = _as_str(function.get("name"))
            if call_id and name:
                tool_name_by_id.setdefault(call_id, name)

    for message in req.messages:
        role = message.role.strip().lower()
        if role in ("system", "developer"):
            system_texts.append(_openai_content_text(message.content))
            continue
        if role == "assistant":
            parts: list[GeminiPart] = []
            # Read reasoning history and echoed signatures from message-level
            # carriers (extra_content, provider_specific_fields, top-level aliases)
            # as well as OpenRouter/Cline reasoning_details.
            reasoning_signature, reasoning_sig_source = _read_signature_with_source(
                {
                    "extra_content": message.extra_content,
                    "provider_specific_fields": message.provider_specific_fields,
                    "thought_signature": message.thought_signature,
                    "thoughtSignature": message.thoughtSignature,
                }
            )
            reasoning_text = message.reasoning_content
            if not reasoning_text and not _is_signed_empty_thought(
                reasoning_text, reasoning_signature
            ):
                reasoning_text = (
                    message.reasoning if isinstance(message.reasoning, str) else None
                )
            scalar_present = bool(reasoning_text) or _is_signed_empty_thought(
                reasoning_text, reasoning_signature
            )
            msg_tool_call_ids = {
                _as_str(tc.get("id"))
                for tc in (message.tool_calls or [])
                if _as_str(tc.get("id"))
            }
            detail_thoughts: list[tuple[str, str | None, str]] = []
            detail_tool_sigs: dict[str, str] = {}
            if message.reasoning_details:
                detail_thoughts, detail_tool_sigs = _parse_openai_reasoning_details(
                    message.reasoning_details,
                    tool_call_ids=msg_tool_call_ids,
                    trace=trace,
                    scalar_present=scalar_present,
                )
            # Some clients echo both original reasoning Parts and a flattened
            # display string. Only substitute original, scoped detail owners
            # for an unsigned scalar when their texts are its exact mirror.
            detail_mirror = (
                scalar_present
                and any(sig for _, sig, _ in detail_thoughts)
                and not valid_thought_signature(reasoning_signature)
                and "".join(text for text, _, _ in detail_thoughts) == reasoning_text
            )
            if scalar_present and not detail_mirror:
                thought_part = GeminiPart(
                    text=reasoning_text,
                    thought=True,
                    thoughtSignature=reasoning_signature,
                )
                if trace is not None and reasoning_sig_source:
                    trace.tag_inbound_source(thought_part, reasoning_sig_source)
                parts.append(thought_part)
            elif detail_thoughts:
                if detail_mirror and reasoning_signature and trace is not None:
                    trace.record_client_drop(
                        carrier="thought",
                        raw_signature=reasoning_signature,
                        source=reasoning_sig_source,
                        note="invalid_base64",
                    )
                for dt_text, dt_sig, dt_source in detail_thoughts:
                    thought_part = GeminiPart(
                        text=dt_text,
                        thought=True,
                        thoughtSignature=dt_sig,
                    )
                    if trace is not None and dt_source:
                        trace.tag_inbound_source(thought_part, dt_source)
                    parts.append(thought_part)
            text = _openai_content_text(message.content)
            if text:
                parts.append(GeminiPart(text=text))
            # assistant 历史中的图片（图片输出模型回传的多模态对话）保留：
            # 丢弃会让模型丢失自己上一轮产出的图片上下文（与 Claude 侧对齐）。
            if isinstance(message.content, list):
                for item in message.content:
                    if not isinstance(item, dict) or item.get("type") != "image_url":
                        continue
                    image_url = _as_dict(item.get("image_url"))
                    url = _as_str(image_url.get("url")).strip()
                    if url:
                        try:
                            media_part = await _media_part_from_url(url)
                        except Exception as e:
                            logger.debug("assistant 历史图片块转换失败，已跳过: %s", e)
                            continue
                        parts.append(media_part)
            for tool_call in message.tool_calls or []:
                function = _as_dict(tool_call.get("function"))
                name = _as_str(function.get("name")) or "unknown"
                call_id = _as_str(tool_call.get("id"))
                tc_sig, tc_source = _read_signature_with_source(tool_call)
                if not tc_sig and call_id and call_id in detail_tool_sigs:
                    tc_sig = detail_tool_sigs[call_id]
                    tc_source = "client_reasoning_details"
                parts.append(
                    _openai_history_call(
                        name,
                        function.get("arguments"),
                        call_id,
                        tc_sig,
                        tc_source,
                        trace,
                    )
                )
                if call_id:
                    tool_name_by_id[call_id] = name
            contents.append(
                GeminiContent(role="model", parts=parts or [GeminiPart(text="")])
            )
            continue
        if role in ("tool", "function"):
            # 旧版 "function" 角色（LangChain 等客户端）与 "tool" 同义：
            # 函数名优先取 tool_call_id 反查，其次 message.name 字段
            name = tool_name_by_id.get(
                message.tool_call_id or "", message.name or "unknown"
            )
            payload = _openai_tool_content_to_response(message.content)
            contents.append(
                GeminiContent(
                    role="user",
                    parts=[
                        GeminiPart(
                            functionResponse=GeminiFunctionResponse(
                                name=name,
                                response=payload,
                                id=message.tool_call_id,
                            )
                        )
                    ],
                )
            )
            continue
        # user 与未知角色按用户消息处理
        parts = await _openai_parts_from_content(message.content)
        contents.append(
            GeminiContent(role="user", parts=parts or [GeminiPart(text="")])
        )

    generation_config: dict[str, object] = {}
    if req.temperature is not None:
        generation_config["temperature"] = req.temperature
    if req.top_p is not None:
        generation_config["topP"] = req.top_p
    max_output_tokens = (
        req.max_completion_tokens
        if req.max_completion_tokens is not None
        else req.max_tokens
    )
    if max_output_tokens is not None:
        generation_config["maxOutputTokens"] = max_output_tokens
    if req.frequency_penalty is not None:
        generation_config["frequencyPenalty"] = req.frequency_penalty
    if req.presence_penalty is not None:
        generation_config["presencePenalty"] = req.presence_penalty
    if req.seed is not None:
        generation_config["seed"] = req.seed
    if req.stop is not None:
        stops = [req.stop] if isinstance(req.stop, str) else list(req.stop)
        # 过滤空串停止词：空 stopSequences 会被上游 400 拒绝
        stops = [s for s in stops if isinstance(s, str) and s]
        if stops:
            generation_config["stopSequences"] = stops
    if req.logprobs:
        generation_config["responseLogprobs"] = True
        if req.top_logprobs is not None:
            generation_config["logprobs"] = req.top_logprobs
    generation_config.update(_openai_response_format_to_config(req.response_format))
    # 思考强度归一化：reasoning_effort / reasoning.effort / thinking /
    # enable_thinking / chat_template_kwargs 全部形态统一为一个档位词。
    # "none" 会在下游抑制 profile 注入的浏览器默认思考档。
    effort = _openai_reasoning_effort(req)
    if effort is not None:
        generation_config["reasoningEffort"] = effort

    gemini_req = GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=(
            GeminiContent(
                role="user",
                parts=[GeminiPart(text="\n\n".join(t for t in system_texts if t))],
            )
            if any(system_texts)
            else None
        ),
        tools=_openai_tools_to_gemini(req.tools),
        toolConfig=_openai_tool_choice_to_config(req.tool_choice, chat=True),
        generationConfig=(
            GeminiGenerationConfig.model_validate(generation_config)
            if generation_config
            else None
        ),
    )
    return gemini_req, resolved_model


# 思考强度词表：兼容大小写 / 连字符 / 下划线 / 别名，统一归一化为
# 管线档位词（minimal / low / medium / high / very_high / xhigh / none）。
# "none" 表示显式关闭思考（下游会抑制 profile 注入的浏览器默认档）。
_EFFORT_ALIASES: dict[str, str] = {
    "none": "none",
    "off": "none",
    "disabled": "none",
    "disable": "none",
    "false": "none",
    "0": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "very_high": "very_high",
    "xhigh": "xhigh",
    "max": "xhigh",
}


def normalize_reasoning_effort_word(raw: object) -> str | None:
    """把任意 effort 词归一化为管线档位词，无法识别返回 None。

    容忍大小写、``very-high`` / ``VERYHIGH`` 等分隔符变体与 off/max 等别名。
    与 chat_service._parse_thinking_level 的档位词汇保持同一集合。
    """
    if not isinstance(raw, str):
        return None
    word = raw.strip().lower().replace("-", "_")
    if not word:
        return None
    if word in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[word]
    compact = word.replace("_", "")
    for key, value in _EFFORT_ALIASES.items():
        if key.replace("_", "") == compact:
            return value
    return None


def _budget_to_effort(budget: int) -> str:
    """思考预算（token 数）→ 档位词（与 Claude budget_tokens 映射同语义）。"""
    if budget <= 2048:
        return "low"
    if budget <= 8192:
        return "medium"
    if budget <= 32768:
        return "high"
    return "xhigh"


def _openai_reasoning_effort(req: OpenAIChatCompletionRequest) -> str | None:
    """归一化 OpenAI 兼容端点的全部思考强度参数形态。

    优先级（高 → 低）：
    1. ``reasoning_effort``（OpenAI 官方顶层字符串）
    2. ``reasoning.effort``（OpenRouter / 新版网关）
    3. ``thinking``（智谱 GLM：type=enabled/disabled + 可选 budget_tokens /
       max_budget_tokens，预算按 token 数映射档位）
    4. ``enable_thinking``（Qwen / DashScope 布尔开关）
    5. ``chat_template_kwargs.thinking``（vLLM 布尔或档位词）

    返回 None 表示未指定任何思考强度（profile 浏览器默认档照常生效）；
    "none" 表示显式关闭。无法识别的非空值记 warning 后按未指定处理。
    """
    # 1) OpenAI 官方顶层 reasoning_effort
    if req.reasoning_effort is not None:
        effort = normalize_reasoning_effort_word(req.reasoning_effort)
        if effort is not None:
            return effort
        # 词表外（如客户端自定义档位词）：记 warning 后继续尝试后续形态
        # （thinking / enable_thinking 等）。显式词表外值不构成"显式关闭"，
        # 提前 return None 会把客户端同时携带的其它思考参数一并丢弃。
        if req.reasoning_effort.strip():
            logger.warning("忽略无法识别的 reasoning_effort: %r", req.reasoning_effort)

    # 2) OpenRouter / 新版网关：reasoning: {"effort": ...}
    if isinstance(req.reasoning, dict):
        effort = normalize_reasoning_effort_word(req.reasoning.get("effort"))
        if effort is not None:
            return effort
        enabled = req.reasoning.get("enabled")
        if isinstance(enabled, bool):
            return "medium" if enabled else "none"

    # 3) 智谱 GLM：thinking: {"type": ..., "budget_tokens" / "max_budget_tokens": N}
    if isinstance(req.thinking, dict):
        thinking_type = _as_str(req.thinking.get("type")).strip().lower()
        budget = _as_int(req.thinking.get("budget_tokens"))
        if budget is None:
            budget = _as_int(req.thinking.get("max_budget_tokens"))
        if thinking_type in ("disabled", "off", "none"):
            return "none"
        if thinking_type in ("enabled", "on", "custom", "auto"):
            if budget is not None and budget > 0:
                return _budget_to_effort(budget)
            return "medium"
        if budget is not None:
            return "none" if budget <= 0 else _budget_to_effort(budget)

    # 4) Qwen / DashScope：enable_thinking
    if req.enable_thinking is not None:
        return "medium" if req.enable_thinking else "none"

    # 5) vLLM：chat_template_kwargs: {"thinking": true}
    if isinstance(req.chat_template_kwargs, dict):
        raw = req.chat_template_kwargs.get("thinking")
        if isinstance(raw, bool):
            return "medium" if raw else "none"
        if isinstance(raw, str):
            effort = normalize_reasoning_effort_word(raw)
            if effort is not None:
                return effort

    return None


def _openai_finish_reason(finish_reason: str | None, *, has_tool_calls: bool) -> str:
    raw = finish_reason or "OTHER"
    # 安全/异常终结优先于 tool_calls：同一帧带 functionCall 但被 SAFETY 拦截时
    # 必须报 content_filter，不能误报成成功完成的 tool_calls。
    mapped = _GEMINI_TO_OPENAI_FINISH.get(raw)
    if mapped is None:
        # 未来新增的上游枚举不得静默变 "stop"（客户端会误信回答完整）
        logger.warning("未识别的 Gemini finishReason: %s，按 length 处理", raw)
        mapped = "length"
    if mapped == "content_filter":
        return "content_filter"
    if mapped == "length":
        return "length"
    if has_tool_calls:
        return "tool_calls"
    return mapped or "stop"


def _normalized_stop_sequences(stop: object) -> list[str]:
    """OpenAI ``stop`` 参数 → 非空停止词列表（供请求编码与响应侧截断共用）。"""
    if stop is None:
        return []
    if isinstance(stop, str):
        stops: list[object] = [stop]
    elif isinstance(stop, list):
        stops = list(stop)
    else:
        return []
    return [s for s in stops if isinstance(s, str) and s]


def build_openai_completion(
    response: GeminiGenerateContentResponse,
    requested_model: str,
    stop_sequences: list[str] | None = None,
) -> dict[str, object]:
    """Gemini 完整响应 → OpenAI Chat Completion 对象。"""
    finish_reason_raw: str | None = None
    candidates = response.candidates or []
    if candidates:
        finish_reason_raw = candidates[0].finishReason

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    has_tool_calls = False
    reasoning_signature = ""
    dropped_thought_sigs: list[tuple[str, str]] = []
    saw_body = False
    trace = get_signature_trace()
    # 与 Claude 侧同源的双重防御：上游若不执行 stopSequences（或拒绝该
    # 参数），停止词及其后的全部内容不得泄漏给客户端（Agent 客户端用
    # stop 切分输出/观察边界，泄漏会打穿解析）。
    stops = StopSequenceBuffer(stop_sequences)

    def flush_pending() -> None:
        tail = stops.feed("", final=True)
        if tail:
            text_parts.append(tail)

    for part in _iter_response_parts(response):
        raw_sig = _as_str(getattr(part, "thoughtSignature", None))
        if stops.hit is not None:
            if raw_sig and trace is not None:
                carrier, _ = _dropped_part_reason(part)
                trace.record_client_out_drop(
                    carrier=carrier, signature=raw_sig, reason="stop_truncated"
                )
            continue
        emitted_sig = False
        text = getattr(part, "text", None)
        if isinstance(text, str):
            if getattr(part, "thought", None) is True:
                signature = part_payload_signature(part, "text")
                if text or _is_signed_empty_thought(text, signature):
                    reasoning_parts.append(text)
                    if len(reasoning_parts) == 1 and not saw_body:
                        reasoning_signature = signature
                        emitted_sig = bool(signature)
                    elif raw_sig:
                        dropped_thought_sigs.append(
                            (
                                raw_sig,
                                "multi_thought_scalar_limit"
                                if signature
                                else "compound_part_unrepresentable",
                            )
                        )
                        emitted_sig = True
            elif text:
                saw_body = True
                filtered = stops.feed(text)
                if filtered:
                    text_parts.append(filtered)
                if stops.hit is not None:
                    if raw_sig and trace is not None:
                        trace.record_client_out_drop(
                            carrier="text",
                            signature=raw_sig,
                            reason="text_part_unrepresentable",
                        )
                    continue
        inline = getattr(part, "inlineData", None)
        if inline is not None:
            saw_body = True
            flush_pending()
            text_parts.append(
                f"\n\n![image](data:{inline.mimeType};base64,{inline.data})"
            )
        function_call = getattr(part, "functionCall", None)
        code = _code_text(
            getattr(part, "executableCode", None),
            getattr(part, "codeExecutionResult", None),
        )
        if code:
            saw_body = True
            if filtered_code := stops.feed(code):
                text_parts.append(filtered_code)
        saw_body |= getattr(part, "functionResponse", None) is not None
        if (
            function_call is not None
            and finish_reason_raw == "STOP"
            and stops.hit is None
        ):
            flush_pending()
            has_tool_calls = True
            saw_body = True
            signature = part_payload_signature(part, "functionCall")
            call_id = function_call.id or f"call_{secrets.token_hex(12)}"
            args = function_call.args if function_call.args is not None else {}
            if function_call.id is None:
                # Record synthesized call_id metadata for legacy lookup compatibility.
                _remember_function_signature(
                    call_id, function_call.name, args, signature
                )
            tool_calls.append(
                {
                    **_signature_extra(signature),
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": function_call.name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
            if signature:
                emitted_sig = True
                if trace is not None:
                    trace.record_client_out_sig(
                        carrier="function_call",
                        wire_field="extra_content",
                        signature=signature,
                        call_id=call_id,
                        call_name=function_call.name,
                    )
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
    flush_pending()
    content_text = "".join(text_parts)
    message: dict[str, object] = {
        "role": "assistant",
        "content": content_text,
    }
    reasoning_text = "".join(reasoning_parts)
    if len(reasoning_parts) == 1:
        message.update(_signature_extra(reasoning_signature))
        if reasoning_signature and trace is not None:
            trace.record_client_out_sig(
                carrier="thought",
                wire_field="extra_content",
                signature=reasoning_signature,
            )
    elif reasoning_signature and trace is not None:
        trace.record_client_out_drop(
            carrier="thought",
            signature=reasoning_signature,
            reason="multi_thought_scalar_limit",
        )
    if trace is not None:
        for dropped_sig, reason in dropped_thought_sigs:
            trace.record_client_out_drop(
                carrier="thought", signature=dropped_sig, reason=reason
            )
        trace.emit_inbound_log()
    if reasoning_text or "extra_content" in message:
        message["reasoning_content"] = reasoning_text
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage_payload = _openai_usage_from_frame(response.usageMetadata)

    return {
        "id": f"chatcmpl-{secrets.token_hex(14)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": (
                    "stop"
                    if stops.hit is not None and finish_reason_raw == "STOP"
                    else _openai_finish_reason(
                        finish_reason_raw, has_tool_calls=has_tool_calls
                    )
                ),
                "logprobs": None,
            }
        ],
        "usage": usage_payload,
        "system_fingerprint": "aistudio-api",
    }


def _openai_usage_from_frame(usage_metadata: object) -> dict[str, object]:
    usage = _usage_from_gemini(usage_metadata or {})
    payload: dict[str, object] = {
        "prompt_tokens": usage["prompt"],
        "completion_tokens": usage["completion"],
        "total_tokens": usage["total"],
    }
    if usage["reasoning"]:
        payload["completion_tokens_details"] = {"reasoning_tokens": usage["reasoning"]}
    if usage["cached"]:
        payload["prompt_tokens_details"] = {"cached_tokens": usage["cached"]}
    return payload


async def openai_stream_adapter(
    source: AsyncIterable[object],
    *,
    requested_model: str,
    include_usage: bool,
    stop_sequences: list[str] | None = None,
    register_failure: Callable[[Callable[[str], list[str]]], None] | None = None,
) -> AsyncGenerator[str, None]:
    """Gemini SSE → OpenAI chat.completion.chunk SSE 流（含 data: [DONE]）。"""
    completion_id = f"chatcmpl-{secrets.token_hex(14)}"
    created = int(time.time())
    # 与 Claude 流适配器同源的响应侧停止词防御：上游不执行 stopSequences
    # 时，停止词及其后内容不得泄漏（Agent 客户端用 stop 切分输出/观察边界）。
    stops = StopSequenceBuffer(stop_sequences)

    def _sse(payload: dict[str, object]) -> str:
        # separators：SSE 每帧省去 ", " 与 ": " 的空格（约 2-4% 字节/分配）
        return (
            "data: "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n\n"
        )

    def _chunk(
        delta: dict[str, object], finish_reason: str | None
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": requested_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if include_usage:
            payload["usage"] = None
        return payload

    def _error_chunk(message: str, error_type: str, code: int) -> dict[str, object]:
        # 错误必须包装成合法的 ChatCompletionChunk：SDK 会用
        # ChatCompletionChunk.model_validate 校验每个 chunk，裸 error 帧缺
        # id/object/created/model/choices 会直接抛 ValidationError。error 字段
        # 作为附加信息保留（SDK 默认忽略未知字段），finish_reason 用终止态，
        # 保证流以正常终结块收尾而不是异常断流。
        # Put the top-level error first so HTTP delivery tracking can
        # distinguish it without reparsing ordinary (possibly large) deltas.
        return {
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            },
            **_chunk({}, "stop"),
        }

    finish_reason_raw: str | None = None
    usage_frame: dict[str, object] | None = None
    tool_index = 0
    has_tool_calls = False
    saw_error = False
    # Chat replays a scalar reasoning field before all other assistant Parts.
    # Sign only a single leading thought whose exact text can be replayed.
    reasoning_signature = ""
    reasoning_count = 0
    first_reasoning_empty = False
    saw_body = False
    trace = get_signature_trace()
    dropped_thought_sigs: list[tuple[str, str]] = []

    # 确定性关闭内层 SSE 生成器（3.6）：适配器自身异常或客户端断连时，
    # 不再依赖事件循环的异步生成器终结器延迟释放上游连接。
    async with _stream_adapter_lifecycle(
        source,
        lambda message: [
            _sse(_error_chunk(message, "api_error", 504)),
            "data: [DONE]\n\n",
        ],
        register_failure,
    ):
        yield _sse(_chunk({"role": "assistant", "content": ""}, None))
        try:
            async for frame in _iter_gemini_sse_frames(source):
                error_obj = frame.get("error")
                if isinstance(error_obj, dict):
                    saw_error = True
                    code = _as_int(error_obj.get("code")) or 500
                    message = _as_str(error_obj.get("message")) or "Upstream error"
                    yield _sse(
                        _error_chunk(
                            message, _OPENAI_ERROR_TYPE.get(code, "api_error"), code
                        )
                    )
                    break
                candidate = _frame_candidate(frame)
                finish = _as_str(candidate.get("finishReason"))
                if finish:
                    finish_reason_raw = finish
                usage_metadata = frame.get("usageMetadata")
                if isinstance(usage_metadata, dict):
                    usage_frame = usage_metadata
                for part in _frame_parts(candidate):
                    raw_sig = _as_str(part.get("thoughtSignature"))
                    function_call = _as_dict(part.get("functionCall"))
                    if function_call:
                        if stops.hit is not None:
                            if raw_sig and trace is not None:
                                trace.record_client_out_drop(
                                    carrier="function_call",
                                    signature=raw_sig,
                                    reason="stop_truncated",
                                )
                            continue
                        # 被停止词缓冲扣留的部分前缀属于此前的正文：必须先于
                        # 工具调用下发（与 Claude 侧 compat_claude 及非流式
                        # flush_pending 对齐），否则该前缀要等流终结才冲刷，
                        # 排到工具/图片内容之后破坏 content 顺序。
                        tail = stops.feed("", final=True)
                        if tail:
                            yield _sse(_chunk({"content": tail}, None))
                        has_tool_calls = True
                        saw_body = True
                        call_id = _as_str(function_call.get("id")) or (
                            f"call_{secrets.token_hex(12)}"
                        )
                        name = _as_str(function_call.get("name")) or "unknown"
                        args = function_call.get("args")
                        if args is None:
                            args = {}
                        fc_sig = part_payload_signature(part, "functionCall")
                        if trace is not None:
                            if fc_sig:
                                trace.record_client_out_sig(
                                    carrier="function_call",
                                    wire_field="extra_content",
                                    signature=fc_sig,
                                    call_id=call_id,
                                    call_name=name,
                                )
                            elif raw_sig:
                                trace.record_client_out_drop(
                                    carrier="function_call",
                                    signature=raw_sig,
                                    reason="compound_part_unrepresentable",
                                    call_id=call_id,
                                    call_name=name,
                                )
                        yield _sse(
                            _chunk(
                                {
                                    "tool_calls": [
                                        {
                                            **_signature_extra(fc_sig),
                                            "index": tool_index,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {
                                                "name": name,
                                                "arguments": json.dumps(
                                                    args, ensure_ascii=False
                                                ),
                                            },
                                        }
                                    ]
                                },
                                None,
                            )
                        )
                        tool_index += 1
                        continue
                    inline = _as_dict(part.get("inlineData"))
                    if inline:
                        saw_body = True
                        if raw_sig and trace is not None:
                            trace.record_client_out_drop(
                                carrier="inline_data",
                                signature=raw_sig,
                                reason="inline_data_unrepresentable",
                            )
                        if stops.hit is None:
                            # 同 functionCall 分支：先冲刷停止词缓冲中的部分
                            # 前缀，保持其在最终 content 字符串中的位置。
                            tail = stops.feed("", final=True)
                            if tail:
                                yield _sse(_chunk({"content": tail}, None))
                            mime = _as_str(inline.get("mimeType")) or "image/png"
                            data = _as_str(inline.get("data"))
                            if data:
                                yield _sse(
                                    _chunk(
                                        {
                                            "content": (
                                                f"\n\n![image](data:{mime};base64,{data})"
                                            )
                                        },
                                        None,
                                    )
                                )
                        continue
                    text = part.get("text")
                    saw_body |= part.get("functionResponse") is not None
                    if isinstance(text, str):
                        if part.get("thought") is True:
                            signature = part_payload_signature(part, "text")
                            if stops.hit is None and (
                                text or _is_signed_empty_thought(text, signature)
                            ):
                                reasoning_count += 1
                                if reasoning_count == 1:
                                    first_reasoning_empty = not text
                                    if not saw_body:
                                        reasoning_signature = signature
                                    elif raw_sig:
                                        dropped_thought_sigs.append(
                                            (raw_sig, "multi_thought_scalar_limit")
                                        )
                                    if raw_sig and not signature:
                                        dropped_thought_sigs.append(
                                            (raw_sig, "compound_part_unrepresentable")
                                        )
                                elif raw_sig:
                                    dropped_thought_sigs.append(
                                        (
                                            raw_sig,
                                            "multi_thought_scalar_limit"
                                            if signature
                                            else "compound_part_unrepresentable",
                                        )
                                    )
                                if text:
                                    yield _sse(
                                        _chunk({"reasoning_content": text}, None)
                                    )
                            elif raw_sig and trace is not None:
                                trace.record_client_out_drop(
                                    carrier="thought",
                                    signature=raw_sig,
                                    reason=(
                                        "stop_truncated"
                                        if stops.hit is not None
                                        else "compound_part_unrepresentable"
                                    ),
                                )
                            continue
                        if stops.hit is not None or not text:
                            if raw_sig and trace is not None:
                                trace.record_client_out_drop(
                                    carrier="tail_empty" if not text else "text",
                                    signature=raw_sig,
                                    reason=(
                                        "stop_truncated"
                                        if stops.hit is not None
                                        else "tail_empty_unrepresentable"
                                    ),
                                )
                            continue
                        if raw_sig and trace is not None:
                            trace.record_client_out_drop(
                                carrier="text",
                                signature=raw_sig,
                                reason="text_part_unrepresentable",
                            )
                        saw_body = True
                        filtered = stops.feed(text)
                        if filtered:
                            yield _sse(_chunk({"content": filtered}, None))
                    elif raw_sig and trace is not None:
                        carrier, reason = _dropped_part_reason(part)
                        trace.record_client_out_drop(
                            carrier=carrier, signature=raw_sig, reason=reason
                        )
        except Exception as exc:
            saw_error = True
            record_result(
                "error", error=f"Stream conversion failed: {type(exc).__name__}: {exc}"
            )
            logger.warning("OpenAI 流式转换异常: %s", exc)
            yield _sse(
                _error_chunk(f"{type(exc).__name__}: {exc}", "internal_error", 500)
            )

        if saw_error:
            yield "data: [DONE]\n\n"
            return

        # 流正常结束：冲刷缓冲中可能成为停止词前缀的尾部（无命中时）。
        if stops.hit is None:
            tail = stops.feed("", final=True)
            if tail:
                yield _sse(_chunk({"content": tail}, None))
        finish_reason = (
            "stop"
            if stops.hit is not None and finish_reason_raw == "STOP"
            else _openai_finish_reason(finish_reason_raw, has_tool_calls=has_tool_calls)
        )
        usage_payload = (
            _openai_usage_from_frame(usage_frame) if usage_frame is not None else None
        )
        final_delta: dict[str, object] = {}
        if reasoning_count == 1 and reasoning_signature:
            final_delta.update(_signature_extra(reasoning_signature))
            if first_reasoning_empty:
                final_delta["reasoning_content"] = ""
            if trace is not None:
                trace.record_client_out_sig(
                    carrier="thought",
                    wire_field="extra_content",
                    signature=reasoning_signature,
                )
        elif reasoning_signature and trace is not None:
            trace.record_client_out_drop(
                carrier="thought",
                signature=reasoning_signature,
                reason="multi_thought_scalar_limit",
            )
        if trace is not None:
            for dropped_sig, reason in dropped_thought_sigs:
                trace.record_client_out_drop(
                    carrier="thought", signature=dropped_sig, reason=reason
                )
            trace.emit_inbound_log()
        final_chunk = _chunk(final_delta, finish_reason)
        # OpenAI 语义：仅 include_usage=True 时发送 usage（且以 choices 为空的
        # 独立尾块形式）。False 时任何 chunk 都不得携带 usage 字段，否则 SDK
        # 会把带 finish_reason 的终结块解析成混合块（协议不允许）。
        yield _sse(final_chunk)
        if include_usage and usage_payload is not None:
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": requested_model,
                    "choices": [],
                    "usage": usage_payload,
                }
            )
        yield "data: [DONE]\n\n"
