"""OpenAI / Anthropic(Claude) 协议兼容转换层。

在既有 Gemini 管线（``GeminiGenerateContentRequest`` →
``normalize_gemini_request`` → ``AIStudioClient`` → Gemini SSE / 响应模型）
之上做双向转换，完整复用账号轮换、403 阶梯重试、429 冷却与抗截断传输：

- 请求侧：OpenAI Chat Completions / Claude Messages → Gemini 请求模型
- 响应侧：Gemini 响应 / SSE 帧 → OpenAI / Claude 格式（含流式）
- 模型映射：``gpt-*`` / ``claude-*`` 等家族名 → 配置的默认 Gemini 模型
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterable, Awaitable, Callable

import httpx
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
)
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.model_defaults import get_compat_settings
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat")

_MAX_MEDIA_BYTES = 20 * 1024 * 1024
# 单个 SSE 事件的上限（防御上游不发空行导致缓冲区无限增长）
_MAX_SSE_EVENT_BYTES = 16 * 1024 * 1024

# OpenAI 家族模型名（命中则回退到配置的默认 Gemini 模型）
_OPENAI_FAMILY_RE = re.compile(
    r"^(?:ft:)?(?:gpt-|o\d|chatgpt|davinci|babbage|text-|tts-|whisper|dall-e|sora|computer-use)",
    re.IGNORECASE,
)

# Claude 家族（含 Bedrock/Vertex 的 "anthropic.claude-" / "us.anthropic.claude-"）
_CLAUDE_FAMILY_RE = re.compile(r"(?:^|\.)claude", re.IGNORECASE)

# 魔数嗅探：无 Content-Type / 无 data URL mime 时的兜底识别
_MAGIC_SNIFFERS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;[\w=-]+)*;base64,(?P<data>.+)$",
    re.DOTALL,
)

# Gemini finishReason → OpenAI finish_reason
_GEMINI_TO_OPENAI_FINISH: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
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
    # 上游 WIRE 枚举（非正常结束）统一按内容过滤/长度等异常终结处理，
    # 避免经 .get(..., "stop") 静默变成正常 "stop" 误导客户端。
    "MALFORMED_FUNCTION_CALL": "content_filter",
    "UNEXPECTED_TOOL_CALL": "content_filter",
    "TOO_MANY_TOOL_CALLS": "content_filter",
    "NO_IMAGE": "content_filter",
    "MISSING_THOUGHT_SIGNATURE": "content_filter",
    "OTHER": "stop",
    "FINISH_REASON_UNSPECIFIED": "stop",
}

# 视为内容安全拦截的 Gemini finishReason（Claude 侧映射为 refusal）
_CLAUDE_REFUSAL_FINISH = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_OTHER",
        "IMAGE_RECITATION",
        "LANGUAGE",
    }
)

_OPENAI_ERROR_TYPE: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
    500: "internal_error",
    502: "api_error",
    503: "api_error",
    529: "api_error",
}

_CLAUDE_ERROR_TYPE: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}

# Claude thinking 占位签名：满足协议必需的 signature 字段，
# 回传输入侧会被丢弃（不透传给上游，避免伪造 thoughtSignature 被风控）
_CLAUDE_DUMMY_SIGNATURE = base64.b64encode(b"aistudio-api-compat-thinking").decode(
    "ascii"
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() or (
            stripped[:1] in "+-" and stripped[1:].isdigit() and len(stripped) > 1
        ):
            return int(stripped)
    return None


def _sniff_mime(raw: bytes) -> str | None:
    for magic, mime in _MAGIC_SNIFFERS:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _assert_media_size(b64_data: str, *, kind: str) -> None:
    """按 base64 长度估算原始字节并做体积上限校验（与远程下载同限）。

    内联 base64 与远程 URL 走同一套 ``_MAX_MEDIA_BYTES`` 限额，避免超大
    媒体直透上游被拒后白白占用带宽与账号额度。
    """
    estimated = len(b64_data) * 3 // 4
    if estimated > _MAX_MEDIA_BYTES:
        raise ValueError(
            f"{kind} exceeds {_MAX_MEDIA_BYTES} bytes limit "
            f"(estimated {estimated} bytes)"
        )


def _parse_data_url(url: str) -> tuple[str, str] | None:
    """解析 data URL → (mime, base64 data)；非 data URL 返回 None。"""
    match = _DATA_URL_RE.match(url.strip())
    if match is None:
        return None
    mime = match.group("mime") or ""
    data = match.group("data")
    if not data:
        raise ValueError("Empty data URL payload")
    _assert_media_size(data, kind="data URL media")
    if not mime:
        try:
            decoded = base64.b64decode(data, validate=False)
        except Exception:
            decoded = b""
        mime = _sniff_mime(decoded) or "application/octet-stream"
    return mime, data


async def _fetch_remote_media(url: str) -> tuple[str, str]:
    """下载远程媒体 → (mime, base64 data)；失败抛 ValueError。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Unsupported media URL: {url[:128]}")
    try:
        from aistudio_api.infrastructure.safe_media import PublicMediaTransport

        async with (
            httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                max_redirects=5,
                transport=PublicMediaTransport(proxy=settings.proxy_url),
                trust_env=False,
            ) as http,
            http.stream("GET", url) as response,
        ):
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_MEDIA_BYTES:
                    raise ValueError("Remote media exceeds configured size limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
            mime = (response.headers.get("content-type") or "").split(";")[0].strip()
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to download media from {url[:128]}: {exc}") from exc
    if len(raw) > _MAX_MEDIA_BYTES:
        raise ValueError(
            f"Media from {url[:128]} exceeds {_MAX_MEDIA_BYTES} bytes limit"
        )
    if not mime or mime == "application/octet-stream":
        mime = _sniff_mime(raw) or "image/jpeg"
    return mime, base64.b64encode(raw).decode("ascii")


async def _media_part_from_url(url: str) -> GeminiPart:
    """data URL 或远程 http(s) URL → Gemini inlineData part。"""
    parsed = _parse_data_url(url)
    if parsed is not None:
        mime, data = parsed
    else:
        mime, data = await _fetch_remote_media(url)
    return GeminiPart(inlineData=GeminiInlineData(mimeType=mime, data=data))


# ---------------------------------------------------------------------------
# 模型映射
# ---------------------------------------------------------------------------


def resolve_compat_model(requested: str, *, channel: str) -> str:
    """解析兼容层模型名：配置映射（精确 + 最长前缀）→ 家族回退 → 原样透传。"""
    clean = (requested or "").strip().removeprefix("models/")
    if not clean:
        raise ValueError("model is required")
    compat = get_compat_settings()
    mapping = _as_dict(compat.get(f"{channel}_model_mapping"))
    lowered = clean.lower()
    # 1. 精确匹配（大小写不敏感）
    for key, value in mapping.items():
        if str(key).lower() == lowered:
            return str(value)
    # 2. 最长前缀匹配（键长优先，支持 "claude-sonnet" 命中 "claude-sonnet-4-5"）
    #    加 "-" 边界，避免 "claude-sonnet" 误命中 "claude-sonnetic-foo"。
    for key in sorted((str(k) for k in mapping), key=len, reverse=True):
        key_lower = str(key).lower()
        if lowered == key_lower or lowered.startswith(key_lower + "-"):
            return str(mapping[key])
    # 3. 家族回退
    default_model = _as_str(compat.get(f"{channel}_default_model"))
    if channel == "claude" and _CLAUDE_FAMILY_RE.search(lowered) and default_model:
        logger.info("Claude 模型映射: %s → %s（默认模型）", clean, default_model)
        return default_model
    if channel == "openai" and _OPENAI_FAMILY_RE.match(clean) and default_model:
        logger.info("OpenAI 模型映射: %s → %s（默认模型）", clean, default_model)
        return default_model
    # 4. 透传（客户端直接填 Gemini 模型名或抗截断变体名）
    return clean


# ---------------------------------------------------------------------------
# OpenAI 请求 → Gemini 请求
# ---------------------------------------------------------------------------


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


def _openai_tool_arguments(raw: object) -> dict[str, object] | list[object]:
    """OpenAI tool_calls function.arguments（JSON 字符串）→ 参数对象。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, (dict, list)):
            return parsed
    return {}


async def _openai_parts_from_content(content: object) -> list[GeminiPart]:
    """OpenAI content（str 或多模态块数组）→ Gemini parts。"""
    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
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
            if file_data.startswith("data:"):
                parsed = _parse_data_url(file_data)
                if parsed is not None:
                    mime, data = parsed
                    parts.append(
                        GeminiPart(
                            inlineData=GeminiInlineData(mimeType=mime, data=data)
                        )
                    )
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
    if not tools:
        return None
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
        if isinstance(description, str) and description:
            decl["description"] = description
        parameters = function.get("parameters")
        if isinstance(parameters, dict) and parameters:
            decl["parameters"] = parameters
        declarations.append(decl)
    if not declarations:
        return None
    return [GeminiTool(functionDeclarations=declarations)]


def _openai_tool_choice_to_config(
    tool_choice: str | dict[str, object] | None,
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


async def openai_to_gemini_request(
    req: OpenAIChatCompletionRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """OpenAI Chat Completions 请求 → (Gemini 请求, 解析后的模型名)。"""
    resolved_model = resolve_compat_model(req.model, channel="openai")
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
            text = _openai_content_text(message.content)
            if text:
                parts.append(GeminiPart(text=text))
            for tool_call in message.tool_calls or []:
                function = _as_dict(tool_call.get("function"))
                name = _as_str(function.get("name")) or "unknown"
                args = _openai_tool_arguments(function.get("arguments"))
                call_id = _as_str(tool_call.get("id"))
                parts.append(
                    GeminiPart(
                        functionCall=GeminiFunctionCall(
                            name=name, args=args, id=call_id or None
                        )
                    )
                )
                if call_id:
                    tool_name_by_id[call_id] = name
            contents.append(
                GeminiContent(role="model", parts=parts or [GeminiPart(text="")])
            )
            continue
        if role == "tool":
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
        if stops:
            generation_config["stopSequences"] = stops
    if req.logprobs:
        generation_config["responseLogprobs"] = True
        if req.top_logprobs is not None:
            generation_config["logprobs"] = req.top_logprobs
    generation_config.update(_openai_response_format_to_config(req.response_format))
    if req.reasoning_effort is not None:
        effort = req.reasoning_effort.strip().lower()
        if effort in ("minimal", "low", "medium", "high", "none"):
            generation_config["reasoningEffort"] = effort
        else:
            logger.debug("忽略无法识别的 reasoning_effort: %s", req.reasoning_effort)

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
        toolConfig=_openai_tool_choice_to_config(req.tool_choice),
        generationConfig=(
            GeminiGenerationConfig.model_validate(generation_config)
            if generation_config
            else None
        ),
    )
    return gemini_req, resolved_model


# ---------------------------------------------------------------------------
# Claude 请求 → Gemini 请求
# ---------------------------------------------------------------------------


async def _claude_blocks_to_parts(
    content: object,
    *,
    tool_name_by_id: dict[str, str],
) -> list[GeminiPart]:
    """Claude content（str 或块数组）→ Gemini parts。

    支持 text / thinking / image / document / tool_use / tool_result 块；
    tool_result 中的图片会展开为紧随其后的独立 inlineData parts。
    """
    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
    parts: list[GeminiPart] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(GeminiPart(text=_as_str(block.get("text"))))
        elif block_type == "thinking":
            # 回传的 thinking（含占位 signature）→ thought 文本；signature 不透传上游
            parts.append(GeminiPart(text=_as_str(block.get("thinking")), thought=True))
        elif block_type == "image":
            source = _as_dict(block.get("source"))
            source_type = source.get("type")
            if source_type == "base64":
                media_type = _as_str(source.get("media_type")) or "image/png"
                data = _as_str(source.get("data"))
                if data:
                    _assert_media_size(data, kind="image media")
                    parts.append(
                        GeminiPart(
                            inlineData=GeminiInlineData(mimeType=media_type, data=data)
                        )
                    )
            elif source_type == "url":
                url = _as_str(source.get("url")).strip()
                if url:
                    parts.append(await _media_part_from_url(url))
        elif block_type == "document":
            source = _as_dict(block.get("source"))
            if source.get("type") == "base64":
                media_type = _as_str(source.get("media_type")) or "application/pdf"
                data = _as_str(source.get("data"))
                if data:
                    _assert_media_size(data, kind="document media")
                    parts.append(
                        GeminiPart(
                            inlineData=GeminiInlineData(mimeType=media_type, data=data)
                        )
                    )
            elif source.get("type") == "url":
                url = _as_str(source.get("url")).strip()
                if url:
                    parts.append(await _media_part_from_url(url))
        elif block_type == "tool_use":
            name = _as_str(block.get("name")) or "unknown"
            tool_input = block.get("input")
            parts.append(
                GeminiPart(
                    functionCall=GeminiFunctionCall(
                        name=name,
                        args=tool_input if tool_input is not None else {},
                        id=_as_str(block.get("id")) or None,
                    )
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
                            media_type = (
                                _as_str(source.get("media_type")) or "image/png"
                            )
                            data = _as_str(source.get("data"))
                            if data:
                                _assert_media_size(data, kind="tool_result media")
                                extra_parts.append(
                                    GeminiPart(
                                        inlineData=GeminiInlineData(
                                            mimeType=media_type, data=data
                                        )
                                    )
                                )
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
    if not tools:
        return None
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
        return None
    return [GeminiTool(functionDeclarations=declarations)]


def _claude_tool_choice_to_config(
    tool_choice: dict[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(tool_choice, dict):
        return None
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


def _claude_thinking_to_level(thinking: dict[str, object] | None) -> str | None:
    """Claude thinking.budget_tokens → Gemini 思考等级（档位兼容由管线自动吸附）。"""
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return None
    budget = _as_int(thinking.get("budget_tokens"))
    if budget is None or budget <= 0:
        return "HIGH"
    if budget <= 2048:
        return "LOW"
    if budget <= 8192:
        return "MEDIUM"
    return "HIGH"


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
            message.content, tool_name_by_id=tool_name_by_id
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
        generation_config["stopSequences"] = list(req.stop_sequences)
    thinking_level = _claude_thinking_to_level(req.thinking)
    if thinking_level is not None:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}

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


# ---------------------------------------------------------------------------
# Gemini 响应 → OpenAI / Claude（非流式）
# ---------------------------------------------------------------------------


def _usage_from_gemini(
    usage_metadata: object,
) -> dict[str, int]:
    """GeminiUsageMetadata（对象或 SSE dict）→ 统一 token 计数。"""

    def pick(*names: str) -> int:
        for name in names:
            value = (
                getattr(usage_metadata, name, None)
                if not isinstance(usage_metadata, dict)
                else usage_metadata.get(name)
            )
            resolved = _as_int(value)
            if resolved is not None:
                return resolved
        return 0

    prompt = pick("promptTokenCount")
    completion = pick("candidatesTokenCount")
    total = pick("totalTokenCount")
    if total == 0 and (prompt or completion):
        total = prompt + completion
    reasoning = pick("thoughtsTokenCount")
    cached = pick("cachedContentTokenCount")
    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "reasoning": reasoning,
        "cached": cached,
    }


def _openai_finish_reason(finish_reason: str | None, *, has_tool_calls: bool) -> str:
    raw = finish_reason or "STOP"
    # 安全/异常终结优先于 tool_calls：同一帧带 functionCall 但被 SAFETY 拦截时
    # 必须报 content_filter，不能误报成成功完成的 tool_calls。
    mapped = _GEMINI_TO_OPENAI_FINISH.get(raw)
    if mapped == "content_filter":
        return "content_filter"
    if raw == "MAX_TOKENS":
        return "length"
    if has_tool_calls:
        return "tool_calls"
    return mapped or "stop"


def _iter_response_parts(
    response: GeminiGenerateContentResponse,
) -> list[object]:
    candidates = response.candidates or []
    if not candidates:
        return []
    content = candidates[0].content
    return list(content.parts) if content else []


def build_openai_completion(
    response: GeminiGenerateContentResponse, requested_model: str
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

    for part in _iter_response_parts(response):
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            if getattr(part, "thought", None) is True:
                reasoning_parts.append(text)
            else:
                text_parts.append(text)
            continue
        inline = getattr(part, "inlineData", None)
        if inline is not None:
            text_parts.append(
                f"\n\n![image](data:{inline.mimeType};base64,{inline.data})"
            )
            continue
        function_call = getattr(part, "functionCall", None)
        if function_call is not None:
            has_tool_calls = True
            call_id = function_call.id or f"call_{secrets.token_hex(12)}"
            args = function_call.args if function_call.args is not None else {}
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": function_call.name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )

    content_text = "".join(text_parts)
    usage = _usage_from_gemini(response.usageMetadata)
    message: dict[str, object] = {
        "role": "assistant",
        "content": content_text,
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage_payload: dict[str, object] = {
        "prompt_tokens": usage["prompt"],
        "completion_tokens": usage["completion"],
        "total_tokens": usage["total"],
    }
    if usage["reasoning"]:
        usage_payload["completion_tokens_details"] = {
            "reasoning_tokens": usage["reasoning"]
        }
    if usage["cached"]:
        usage_payload["prompt_tokens_details"] = {"cached_tokens": usage["cached"]}

    return {
        "id": f"chatcmpl-{secrets.token_hex(14)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _openai_finish_reason(
                    finish_reason_raw, has_tool_calls=has_tool_calls
                ),
                "logprobs": None,
            }
        ],
        "usage": usage_payload,
        "system_fingerprint": "aistudio-api",
    }


def _claude_stop_reason(
    finish_reason: str | None,
    *,
    has_tool_use: bool,
    full_text: str,
    stop_sequences: list[str] | None,
) -> tuple[str, str | None]:
    stop_seq: str | None = None
    if stop_sequences and full_text:
        for sequence in stop_sequences:
            if sequence and full_text.endswith(sequence):
                stop_seq = sequence
                break
    raw = finish_reason or "STOP"
    if stop_seq is not None:
        return "stop_sequence", stop_seq
    # 安全/异常拦截优先于 tool_use，避免把被风控终止的响应误报成正常工具调用。
    if raw in _CLAUDE_REFUSAL_FINISH:
        return "refusal", None
    if raw == "MAX_TOKENS":
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
    full_text_parts: list[str] = []
    has_tool_use = False

    for part in _iter_response_parts(response):
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            if getattr(part, "thought", None) is True:
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": text,
                        "signature": _CLAUDE_DUMMY_SIGNATURE,
                    }
                )
            else:
                blocks.append({"type": "text", "text": text})
                full_text_parts.append(text)
            continue
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
            continue
        function_call = getattr(part, "functionCall", None)
        if function_call is not None:
            has_tool_use = True
            call_id = function_call.id or f"toolu_{secrets.token_hex(12)}"
            tool_input = function_call.args if function_call.args is not None else {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": function_call.name,
                    "input": tool_input,
                }
            )

    stop_reason, stop_seq = _claude_stop_reason(
        finish_reason_raw,
        has_tool_use=has_tool_use,
        full_text="".join(full_text_parts),
        stop_sequences=stop_sequences,
    )
    usage = _usage_from_gemini(response.usageMetadata)
    usage_payload: dict[str, object] = {
        "input_tokens": usage["prompt"],
        "output_tokens": usage["completion"],
    }
    if usage["cached"]:
        usage_payload["cache_read_input_tokens"] = usage["cached"]
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


# ---------------------------------------------------------------------------
# Gemini SSE 帧解析
# ---------------------------------------------------------------------------


async def _iter_gemini_sse_frames(
    source: AsyncIterable[object],
) -> AsyncGenerator[dict[str, object], None]:
    """解析内部 Gemini SSE 字符串流 → JSON 帧对象流。

    按 SSE 事件边界（空行）缓冲，支持被 TCP/代理拆分的单个事件，以及同一
    事件内的多行 ``data:`` 合并。残帧不会静默丢弃整条消息。
    """

    def _parse_event(event: str) -> dict[str, object] | None:
        data_lines = []
        for line in event.splitlines():
            stripped = line.strip()
            # 先 strip 再切 "data:"：SSE 允许行首有空白，直接 line[5:] 会在
            # 前置空白时切错位置（把 payload 首字符一起吃掉）。
            if stripped.startswith("data:"):
                data_lines.append(stripped[5:].strip())
        if not data_lines:
            return None
        payload = "\n".join(data_lines)
        if not payload or payload == "[DONE]":
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("丢弃无法解析的 Gemini SSE 帧: %.120s", payload)
            return None
        return parsed if isinstance(parsed, dict) else None

    buffer = ""
    async for raw in source:
        if isinstance(raw, (bytes, bytearray)):
            buffer += bytes(raw).decode("utf-8", errors="replace")
        elif isinstance(raw, str):
            buffer += raw
        else:
            continue
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            parsed = _parse_event(event)
            if parsed is not None:
                yield parsed
        # 防御：上游若持续发送一个不含空行终止符的超大事件，缓冲区会无限增长。
        # 单帧超过 16MB 说明协议已损坏，直接清空并告警，避免内存被拖垮。
        if len(buffer) > _MAX_SSE_EVENT_BYTES:
            logger.warning(
                "Gemini SSE 缓冲区超过 %d 字节仍无事件边界，已丢弃累积内容",
                _MAX_SSE_EVENT_BYTES,
            )
            buffer = ""
    # 处理流结束时未以空行收尾的最后一个事件
    if buffer.strip():
        parsed = _parse_event(buffer)
        if parsed is not None:
            yield parsed


def _frame_candidate(frame: dict[str, object]) -> dict[str, object]:
    candidates = frame.get("candidates")
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            return first
    return {}


def _frame_parts(candidate: dict[str, object]) -> list[dict[str, object]]:
    content = _as_dict(candidate.get("content"))
    parts = content.get("parts")
    if isinstance(parts, list):
        return [p for p in parts if isinstance(p, dict)]
    return []


# ---------------------------------------------------------------------------
# 流式适配：Gemini SSE → OpenAI chunk 流
# ---------------------------------------------------------------------------


def _openai_usage_from_frame(
    usage_metadata: dict[str, object] | None,
) -> dict[str, object]:
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
) -> AsyncGenerator[str, None]:
    """Gemini SSE → OpenAI chat.completion.chunk SSE 流（含 data: [DONE]）。"""
    completion_id = f"chatcmpl-{secrets.token_hex(14)}"
    created = int(time.time())

    def _sse(payload: dict[str, object]) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

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
        payload = _chunk({}, "stop")
        payload["error"] = {
            "message": message,
            "type": error_type,
            "param": None,
            "code": code,
        }
        return payload

    yield _sse(_chunk({"role": "assistant", "content": ""}, None))

    finish_reason_raw: str | None = None
    usage_frame: dict[str, object] | None = None
    tool_index = 0
    has_tool_calls = False
    saw_error = False

    # 确定性关闭内层 SSE 生成器（3.6）：适配器自身异常或客户端断连时，
    # 不再依赖事件循环的异步生成器终结器延迟释放上游连接。
    source_aclose: Callable[[], Awaitable[None]] | None = getattr(
        source, "aclose", None
    )
    try:
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
                    function_call = _as_dict(part.get("functionCall"))
                    if function_call:
                        has_tool_calls = True
                        call_id = _as_str(function_call.get("id")) or (
                            f"call_{secrets.token_hex(12)}"
                        )
                        name = _as_str(function_call.get("name")) or "unknown"
                        args = function_call.get("args")
                        if args is None:
                            args = {}
                        yield _sse(
                            _chunk(
                                {
                                    "tool_calls": [
                                        {
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
                    if isinstance(text, str) and text:
                        if part.get("thought") is True:
                            yield _sse(_chunk({"reasoning_content": text}, None))
                        else:
                            yield _sse(_chunk({"content": text}, None))
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

        finish_reason = _openai_finish_reason(
            finish_reason_raw, has_tool_calls=has_tool_calls
        )
        usage_payload = (
            _openai_usage_from_frame(usage_frame) if usage_frame is not None else None
        )
        final_chunk = _chunk({}, finish_reason)
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
    finally:
        if callable(source_aclose):
            with contextlib.suppress(Exception):
                await source_aclose()


# ---------------------------------------------------------------------------
# 流式适配：Gemini SSE → Claude 事件流
# ---------------------------------------------------------------------------


class _ClaudeStreamEmitter:
    """Claude SSE 事件发射器：管理 content block 的开闭与索引状态。"""

    def __init__(self, message_id: str, model: str) -> None:
        self.message_id = message_id
        self.model = model
        self.index = -1
        self.open_kind: str | None = None
        self.text_parts: list[str] = []
        self.has_tool_use = False
        self.finish_reason_raw: str | None = None
        self.usage_frame: dict[str, object] | None = None
        self.saw_error = False

    def sse(self, event: str, payload: dict[str, object]) -> str:
        return (
            f"event: {event}\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        )

    def message_start(self) -> str:
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
                    "usage": {"input_tokens": 0, "output_tokens": 1},
                },
            },
        )

    def ping(self) -> str:
        return self.sse("ping", {"type": "ping"})

    def _close_events(self) -> list[str]:
        events: list[str] = []
        if self.open_kind == "thinking":
            events.append(
                self.sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.index,
                        "delta": {
                            "type": "signature_delta",
                            "signature": _CLAUDE_DUMMY_SIGNATURE,
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
        if self.open_kind == kind:
            return []
        events = self._close_events()
        self.index += 1
        block: dict[str, object]
        if kind == "thinking":
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

    def thinking_delta(self, text: str) -> list[str]:
        events = self._ensure_block("thinking")
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
        self.text_parts.append(text)
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

    def tool_use(self, call_id: str, name: str, args: object) -> list[str]:
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
                            args if args is not None else {},
                            ensure_ascii=False,
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

    def finish(self, stop_sequences: list[str] | None) -> list[str]:
        events = self._close_events()
        if self.saw_error:
            return events
        stop_reason, stop_seq = _claude_stop_reason(
            self.finish_reason_raw,
            has_tool_use=self.has_tool_use,
            full_text="".join(self.text_parts),
            stop_sequences=stop_sequences,
        )
        usage = _usage_from_gemini(self.usage_frame)
        usage_payload: dict[str, object] = {
            "input_tokens": usage["prompt"],
            "output_tokens": usage["completion"],
        }
        if usage["cached"]:
            usage_payload["cache_read_input_tokens"] = usage["cached"]
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
) -> AsyncGenerator[str, None]:
    """Gemini SSE → Claude Messages SSE 事件流。"""
    emitter = _ClaudeStreamEmitter(f"msg_{secrets.token_hex(12)}", requested_model)
    yield emitter.message_start()
    yield emitter.ping()
    # 确定性关闭内层 SSE 生成器（3.6）：自身异常或客户端断连时立即释放上游连接
    source_aclose: Callable[[], Awaitable[None]] | None = getattr(
        source, "aclose", None
    )
    try:
        async for frame in _iter_gemini_sse_frames(source):
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
                function_call = _as_dict(part.get("functionCall"))
                if function_call:
                    call_id = _as_str(function_call.get("id")) or (
                        f"toolu_{secrets.token_hex(12)}"
                    )
                    name = _as_str(function_call.get("name")) or "unknown"
                    args = function_call.get("args")
                    for event in emitter.tool_use(call_id, name, args):
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
                if isinstance(text, str) and text:
                    if part.get("thought") is True:
                        for event in emitter.thinking_delta(text):
                            yield event
                    else:
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
    else:
        for event in emitter.finish(stop_sequences):
            yield event
    finally:
        if callable(source_aclose):
            with contextlib.suppress(Exception):
                await source_aclose()


# ---------------------------------------------------------------------------
# 错误格式化（供 app.py 全局异常处理器按路径分流）
# ---------------------------------------------------------------------------


def openai_error_payload(status: int, message: str) -> dict[str, object]:
    return {
        "error": {
            "message": message or "Unknown error",
            "type": _OPENAI_ERROR_TYPE.get(status, "api_error"),
            "param": None,
            "code": status,
        }
    }


def claude_error_payload(status: int, message: str) -> dict[str, object]:
    return {
        "type": "error",
        "error": {
            "type": _CLAUDE_ERROR_TYPE.get(status, "api_error"),
            "message": message or "Unknown error",
        },
    }


def is_claude_compat_request(request: Request) -> bool:
    """判断请求是否走 Claude 兼容格式（/v1/messages 或带 anthropic-version 的 /v1/models）。"""
    path = request.url.path
    if path == "/v1/messages" or path.startswith("/v1/messages/"):
        return True
    if path == "/v1/models" or path.startswith("/v1/models/"):
        return bool(request.headers.get("anthropic-version"))
    return False


def is_compat_request(request: Request) -> bool:
    """判断请求是否属于 OpenAI / Claude 兼容层路径（区别于 /v1beta Gemini 路径）。"""
    path = request.url.path
    if not path.startswith("/v1/") or path.startswith("/v1beta"):
        return False
    return (
        path == "/v1/chat/completions"
        or path == "/v1/models"
        or path.startswith(("/v1/models/", "/v1/messages/"))
        or path == "/v1/messages"
    )


def _compat_validation_message(exc: RequestValidationError) -> str:
    """把 RequestValidationError 提炼为简洁的字段级错误消息（剥离堆栈上下文）。"""
    parts: list[str] = []
    for error in exc.errors()[:3]:
        loc = ".".join(str(x) for x in error.get("loc", ()) if x != "body")
        msg = str(error.get("msg") or "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    if not parts:
        return "Request validation failed"
    count = len(exc.errors())
    unit = "validation error" if count == 1 else "validation errors"
    return f"{count} {unit}: " + "; ".join(parts)


def build_compat_error_response(
    request: Request, exc: Exception
) -> JSONResponse | None:
    """兼容层路径的异常 → 对应协议格式的 JSONResponse；非兼容路径返回 None。"""
    if not is_compat_request(request):
        return None
    from aistudio_api.application.api_service_gemini import (
        classify_gemini_error_payload,
    )

    code, message, _status = classify_gemini_error_payload(exc)
    if isinstance(exc, RequestValidationError):
        message = _compat_validation_message(exc) or message
    headers = getattr(exc, "headers", None)
    if is_claude_compat_request(request):
        return JSONResponse(
            status_code=code,
            content=claude_error_payload(code, message),
            headers=headers,
        )
    return JSONResponse(
        status_code=code,
        content=openai_error_payload(code, message),
        headers=headers,
    )


__all__ = [
    "build_claude_message",
    "build_compat_error_response",
    "build_openai_completion",
    "claude_error_payload",
    "claude_stream_adapter",
    "claude_to_gemini_request",
    "is_claude_compat_request",
    "is_compat_request",
    "openai_error_payload",
    "openai_stream_adapter",
    "openai_to_gemini_request",
    "resolve_compat_model",
]
