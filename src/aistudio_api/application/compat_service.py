"""OpenAI / Anthropic(Claude) 协议兼容转换层。

在既有 Gemini 管线（``GeminiGenerateContentRequest`` →
``normalize_gemini_request`` → ``AIStudioClient`` → Gemini SSE / 响应模型）
之上做双向转换，完整复用账号轮换、403 阶梯重试、429 冷却与抗截断传输：

- 请求侧：OpenAI Chat Completions / Claude Messages → Gemini 请求模型
- 响应侧：Gemini 响应 / SSE 帧 → OpenAI / Claude 格式（含流式）
- 模型映射：``gpt-*`` / ``claude-*`` 等家族名 → 配置的默认 Gemini 模型
"""

from __future__ import annotations

import asyncio
import base64
import codecs
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
from aistudio_api.application.media_session import (
    current_media,
    has_remote_media,
    media_session,
)
from aistudio_api.application.stream_utils import (
    SSEEventBuffer,
    StopSequenceBuffer,
    coalesce_tool_frames,
)
from aistudio_api.domain.tool_signatures import (
    is_server_side_tool_call as _is_server_side_tool_call,
    lookup_function_signature as _lookup_function_signature,
    remember_function_signature as _remember_function_signature,
    valid_thought_signature as _valid_thought_signature,
)
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
    # OTHER = streaming.py 检测到流截断时下发（无上游 finishReason）。
    # 映射 "stop" 会让 SDK 把残缺回答当完整答案；"length" 语义是"输出因
    # 上限提前终止"，客户端可据此提示用户重试，与截断的实际表现一致。
    "OTHER": "length",
    "FINISH_REASON_UNSPECIFIED": "length",
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
        # 工具调用异常终结（OpenAI 侧映射 content_filter，语义对齐 refusal）
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
        "NO_IMAGE",
        "MISSING_THOUGHT_SIGNATURE",
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
# 服务端工具调用防护（thought_signature 400 修复）：判定与校验函数统一由
# domain/tool_signatures.py 提供，此处保留别名供本模块各转换路径使用。
# 背景与修复策略详见该模块文档字符串。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _signature_extra(signature: object) -> dict[str, object]:
    """Opaque Gemini metadata which clients can return with tool history."""
    if isinstance(signature, str) and signature:
        return {"extra_content": {"google": {"thought_signature": signature}}}
    return {}


def _read_signature(item: dict[str, object]) -> str | None:
    google = _as_dict(_as_dict(item.get("extra_content")).get("google"))
    return _as_str(google.get("thought_signature")) or None


def _claude_signature(signature: object) -> str:
    if not isinstance(signature, str) or not signature:
        return _CLAUDE_DUMMY_SIGNATURE
    return base64.b64encode(
        json.dumps({"aistudio_thought_signature": signature}).encode()
    ).decode("ascii")


def _read_claude_signature(signature: object) -> str | None:
    if not isinstance(signature, str) or not signature:
        return None
    try:
        envelope = json.loads(base64.b64decode(signature, validate=True))
    except (ValueError, UnicodeDecodeError):
        return None
    return _as_str(_as_dict(envelope).get("aistudio_thought_signature")) or None


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
    padding = 2 if b64_data.endswith("==") else int(b64_data.endswith("="))
    estimated = max(0, len(b64_data) * 3 // 4 - padding)
    if estimated > _MAX_MEDIA_BYTES:
        raise ValueError(
            f"{kind} exceeds {_MAX_MEDIA_BYTES} bytes limit "
            f"(estimated {estimated} bytes)"
        )
    session = current_media.get()
    if session is not None:
        session.account_bytes(estimated)


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
            # Magic sniffers need at most twelve bytes. Avoid decoding the
            # entire image merely to infer its MIME type.
            decoded = base64.b64decode(data[:32], validate=False)
        except ValueError:
            decoded = b""
        mime = _sniff_mime(decoded) or "application/octet-stream"
    return mime, data


async def _fetch_remote_media(url: str) -> tuple[str, str]:
    """下载远程媒体 → (mime, base64 data)；失败抛 ValueError。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Unsupported media URL: {url[:128]}")
    session = current_media.get()
    if session is None:
        async with media_session():
            return await _fetch_remote_media(url)
    session.downloads += 1
    if session.downloads > 64:
        raise ValueError("A request may download at most 64 remote media items")
    try:
        async with (
            session.semaphore,
            (await session.get_client()).stream("GET", url) as response,
        ):
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_MEDIA_BYTES:
                    raise ValueError("Remote media exceeds configured size limit")
                session.account_bytes(len(chunk))
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
    #    兼容未清洗的通配符键（"claude-sonnet-*"）：剥掉尾部 "*" 与悬挂
    #    "-" 后再匹配（get_compat_settings 已清洗，这里兜底直连配置场景）。
    for key in sorted((str(k) for k in mapping), key=len, reverse=True):
        key_clean = str(key).lower().rstrip("*").rstrip("-")
        if lowered == key_clean or lowered.startswith(key_clean + "-"):
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
    if len(content) > 1 and has_remote_media(content):
        parts: list[GeminiPart] = []
        for offset in range(0, len(content), 4):
            try:
                async with asyncio.TaskGroup() as group:
                    tasks = [
                        group.create_task(_openai_parts_from_content([item]))
                        for item in content[offset : offset + 4]
                    ]
            except* ValueError as exc:
                raise ValueError(str(exc.exceptions[0])) from exc
            for task in tasks:
                parts.extend(task.result())
        return parts
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
        declarations.append(decl)
    if not declarations:
        return []
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


@media_session()
async def openai_to_gemini_request(
    req: OpenAIChatCompletionRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """OpenAI Chat Completions 请求 → (Gemini 请求, 解析后的模型名)。"""
    resolved_model = resolve_compat_model(req.model, channel="openai")
    if req.n not in (None, 1):
        raise ValueError("Only n=1 is supported")
    if req.parallel_tool_calls is False:
        raise ValueError(
            "parallel_tool_calls=false cannot be enforced by this upstream"
        )
    contents: list[GeminiContent] = []
    system_texts: list[str] = []
    tool_name_by_id: dict[str, str] = {}
    # 被剥离的服务端工具调用 id 集合：对应的 tool result 也要一并跳过，
    # 避免孤立 functionResponse 触发上游 MALFORMED_FUNCTION_CALL。
    dropped_server_tool_ids: set[str] = set()

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
                signature = _read_signature(tool_call)
                if not _valid_thought_signature(signature):
                    # 客户端丢失签名（OpenAI 协议无官方字段）→ 缓存回注
                    # （2026-11-14 实测：客户端函数同样强制签名，缺失即 400）
                    signature = _lookup_function_signature(call_id or None, name)
                if not _valid_thought_signature(signature):
                    # 回注未命中：任何 functionCall 回传都会被上游拒 400
                    # （服务端工具与客户端函数同规则），剥离调用及配对响应；
                    # 服务端工具状态已在上游持久化，客户端工具模型会重新
                    # 发起调用，功能降级但不阻断对话。
                    if call_id:
                        dropped_server_tool_ids.add(call_id)
                    tool_name_by_id.pop(call_id, None)
                    logger.info(
                        "剥离无签名的工具调用（缓存未命中）: name=%s, call_id=%s,"
                        " server_side=%s",
                        name,
                        call_id or "(无id)",
                        _is_server_side_tool_call(name),
                    )
                    continue
                parts.append(
                    GeminiPart(
                        thoughtSignature=signature,
                        functionCall=GeminiFunctionCall(
                            name=name, args=args, id=call_id or None
                        ),
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
            # 跳过被剥离的工具调用的结果（无对应的 functionCall）
            if message.tool_call_id and message.tool_call_id in dropped_server_tool_ids:
                continue
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
    if req.reasoning_effort is not None:
        effort = req.reasoning_effort.strip().lower()
        if effort in ("minimal", "low", "medium", "high", "very_high", "xhigh", "none"):
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
    dropped_server_tool_ids: set[str] | None = None,
) -> list[GeminiPart]:
    """Claude content（str 或块数组）→ Gemini parts。

    支持 text / thinking / image / document / tool_use / tool_result 块；
    tool_result 中的图片会展开为紧随其后的独立 inlineData parts。
    无有效签名的服务端工具调用及其结果会被剥离（见 _is_server_side_tool_call）。
    """
    if dropped_server_tool_ids is None:
        dropped_server_tool_ids = set()
    if isinstance(content, str):
        return [GeminiPart(text=content)]
    if not isinstance(content, list):
        return []
    if len(content) > 1 and has_remote_media(content):
        parts: list[GeminiPart] = []
        for offset in range(0, len(content), 4):
            try:
                async with asyncio.TaskGroup() as group:
                    tasks = [
                        group.create_task(
                            _claude_blocks_to_parts(
                                [block],
                                tool_name_by_id=tool_name_by_id,
                                dropped_server_tool_ids=dropped_server_tool_ids,
                            )
                        )
                        for block in content[offset : offset + 4]
                    ]
            except* ValueError as exc:
                raise ValueError(str(exc.exceptions[0])) from exc
            for task in tasks:
                parts.extend(task.result())
        return parts
    parts: list[GeminiPart] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(GeminiPart(text=_as_str(block.get("text"))))
        elif block_type == "thinking":
            parts.append(
                GeminiPart(
                    text=_as_str(block.get("thinking")),
                    thought=True,
                    thoughtSignature=_read_claude_signature(block.get("signature")),
                )
            )
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
            signature = _read_signature(block)
            if not _valid_thought_signature(signature):
                # 客户端丢失签名（Claude 协议 tool_use 无官方字段）→ 缓存回注
                signature = _lookup_function_signature(
                    _as_str(block.get("id")) or None, name
                )
            if not _valid_thought_signature(signature):
                # 回注未命中：任何 functionCall 回传都会被上游拒 400
                # （服务端工具与客户端函数同规则，2026-11-14 实测），
                # 剥离调用及配对 tool_result。
                tool_use_id = _as_str(block.get("id"))
                if tool_use_id:
                    dropped_server_tool_ids.add(tool_use_id)
                tool_name_by_id.pop(tool_use_id, None)
                logger.info(
                    "剥离无签名的工具调用（缓存未命中）: name=%s, tool_use_id=%s,"
                    " server_side=%s",
                    name,
                    tool_use_id or "(无id)",
                    _is_server_side_tool_call(name),
                )
            else:
                parts.append(
                    GeminiPart(
                        thoughtSignature=signature,
                        functionCall=GeminiFunctionCall(
                            name=name,
                            args=tool_input if tool_input is not None else {},
                            id=_as_str(block.get("id")) or None,
                        ),
                    )
                )
        elif block_type == "tool_result":
            tool_use_id = _as_str(block.get("tool_use_id"))
            # 跳过被剥离的工具调用的结果（无对应的 functionCall）
            if tool_use_id and tool_use_id in dropped_server_tool_ids:
                continue
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


@media_session()
async def claude_to_gemini_request(
    req: ClaudeMessagesRequest,
) -> tuple[GeminiGenerateContentRequest, str]:
    """Claude Messages 请求 → (Gemini 请求, 解析后的模型名)。"""
    resolved_model = resolve_compat_model(req.model, channel="claude")
    contents: list[GeminiContent] = []
    tool_name_by_id: dict[str, str] = {}
    # 被剥离的服务端工具调用 id 集合（无有效签名，见 _is_server_side_tool_call）
    dropped_server_tool_ids: set[str] = set()

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
            dropped_server_tool_ids=dropped_server_tool_ids,
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
    reasoning = pick("thoughtsTokenCount")
    completion = pick("candidatesTokenCount") + reasoning
    total = pick("totalTokenCount")
    if total == 0 and (prompt or completion):
        total = prompt + completion
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


def _iter_response_parts(
    response: GeminiGenerateContentResponse,
) -> list[object]:
    candidates = response.candidates or []
    if not candidates:
        return []
    content = candidates[0].content
    if not content:
        return []
    parts: list[object] = list(content.parts) if content else []
    # 签名缓存：凡见到 functionCall part 携带有效 thoughtSignature 就缓存
    # （call_id 首选 + 函数名兜底），供客户端丢失签名的下轮请求回注。
    for part in parts:
        function_call = getattr(part, "functionCall", None)
        if function_call is not None:
            _remember_function_signature(
                getattr(function_call, "id", None),
                getattr(function_call, "name", None),
                getattr(part, "thoughtSignature", None),
            )
    return parts


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
        inline = getattr(part, "inlineData", None)
        if inline is not None:
            text_parts.append(
                f"\n\n![image](data:{inline.mimeType};base64,{inline.data})"
            )
        function_call = getattr(part, "functionCall", None)
        if function_call is not None and finish_reason_raw in (None, "STOP"):
            has_tool_calls = True
            call_id = function_call.id or f"call_{secrets.token_hex(12)}"
            args = function_call.args if function_call.args is not None else {}
            tool_calls.append(
                {
                    **_signature_extra(getattr(part, "thoughtSignature", None)),
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
    full_text: str,
    stop_sequences: list[str] | None,
) -> tuple[str, str | None]:
    stops = StopSequenceBuffer(stop_sequences)
    stops.feed(full_text, final=True)
    stop_seq = stops.hit
    raw = finish_reason or "STOP"
    if stop_seq is not None:
        return "stop_sequence", stop_seq
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
        if isinstance(text, str) and text:
            if getattr(part, "thought", None) is True:
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": text,
                        "signature": _claude_signature(
                            getattr(part, "thoughtSignature", None)
                        ),
                    }
                )
            else:
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
        function_call = getattr(part, "functionCall", None)
        if function_call is not None and finish_reason_raw in (None, "STOP"):
            has_tool_use = True
            call_id = function_call.id or f"toolu_{secrets.token_hex(12)}"
            blocks.append(
                {
                    **_signature_extra(getattr(part, "thoughtSignature", None)),
                    "type": "tool_use",
                    "id": call_id,
                    "name": function_call.name,
                    "input": _coerce_tool_args(function_call.args),
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
    if stops.hit is not None:
        stop_reason, stop_seq = "stop_sequence", stops.hit
    else:
        stop_reason, stop_seq = _claude_stop_reason(
            finish_reason_raw,
            has_tool_use=has_tool_use,
            full_text="",
            stop_sequences=None,
        )
    usage = _usage_from_gemini(response.usageMetadata)
    usage_payload: dict[str, object] = {
        "input_tokens": max(0, usage["prompt"] - usage["cached"]),
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
    async for frame in coalesce_tool_frames(_parse_gemini_sse_frames(source)):
        _remember_frame_tool_signatures(frame)
        yield frame


def _remember_frame_tool_signatures(frame: dict[str, object]) -> None:
    """流式帧签名缓存：functionCall part 携带签名即缓存（同 _iter_response_parts）。"""
    candidate = _frame_candidate(frame)
    for part in _frame_parts(candidate):
        function_call = _as_dict(part.get("functionCall"))
        if function_call:
            _remember_function_signature(
                _as_str(function_call.get("id")) or None,
                _as_str(function_call.get("name")) or None,
                part.get("thoughtSignature"),
            )


async def _parse_gemini_sse_frames(
    source: AsyncIterable[object],
) -> AsyncGenerator[dict[str, object], None]:
    """解析内部 Gemini SSE 字符串流 → JSON 帧对象流。

    按 SSE 事件边界（空行）缓冲，支持被 TCP/代理拆分的单个事件，以及同一
    事件内的多行 ``data:`` 合并。残帧不会静默丢弃整条消息。
    """

    def _parse_event(event: str) -> dict[str, object] | None:
        data_lines = []
        # SSEEventBuffer has normalized only the protocol's CR/LF endings.
        # str.splitlines() also splits valid JSON string characters such as
        # U+2028, U+2029 and U+0085, corrupting otherwise valid text payloads.
        for line in event.split("\n"):
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
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid Gemini SSE JSON frame") from exc
        return parsed if isinstance(parsed, dict) else None

    buffer = SSEEventBuffer(_MAX_SSE_EVENT_BYTES)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    async for raw in source:
        if isinstance(raw, dict):
            yield raw
            continue
        if isinstance(raw, (bytes, bytearray)):
            text = decoder.decode(bytes(raw), final=False)
        elif isinstance(raw, str):
            text = raw
        else:
            continue
        for event in buffer.feed(text):
            parsed = _parse_event(event)
            if parsed is not None:
                yield parsed
    # 处理流结束时未以空行收尾的最后一个事件
    for event in buffer.feed(decoder.decode(b"", final=True)):
        parsed = _parse_event(event)
        if parsed is not None:
            yield parsed
    tail = buffer.finish()
    if tail.strip():
        parsed = _parse_event(tail)
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
    register_failure: Callable[[Callable[[str], list[str]]], None] | None = None,
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

    if register_failure is not None:
        register_failure(
            lambda message: [
                _sse(_error_chunk(message, "api_error", 504)),
                "data: [DONE]\n\n",
            ]
        )
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
                                            **_signature_extra(
                                                part.get("thoughtSignature")
                                            ),
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
        # stop sequence 命中状态：命中后丢弃后续正文增量（Claude 语义是
        # 在 stop sequence 处截断文本，透传其后内容会让客户端拿到多余文本）
        self.stop_hit: str | None = None
        self.thinking_signature = ""

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
                        "partial_json": json.dumps(args, ensure_ascii=False),
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
        # stop sequence 已在流中命中：直接用命中值（full_text 已被截断，
        # 重扫文本判定必然失败）
        if self.stop_hit is not None:
            stop_reason: str = "stop_sequence"
            stop_seq: str | None = self.stop_hit
        else:
            stop_reason, stop_seq = _claude_stop_reason(
                self.finish_reason_raw,
                has_tool_use=self.has_tool_use,
                full_text="",
                stop_sequences=None,
            )
        usage = _usage_from_gemini(self.usage_frame)
        usage_payload: dict[str, object] = {
            "input_tokens": max(0, usage["prompt"] - usage["cached"]),
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
    register_failure: Callable[[Callable[[str], list[str]]], None] | None = None,
) -> AsyncGenerator[str, None]:
    """Gemini SSE → Claude Messages SSE 事件流。"""
    emitter = _ClaudeStreamEmitter(f"msg_{secrets.token_hex(12)}", requested_model)
    stops = StopSequenceBuffer(stop_sequences)
    if register_failure is not None:
        register_failure(lambda message: emitter.error(504, message))
    # 确定性关闭内层 SSE 生成器（3.6）：自身异常或客户端断连时立即释放上游连接
    source_aclose: Callable[[], Awaitable[None]] | None = getattr(
        source, "aclose", None
    )
    try:
        yield emitter.message_start()
        yield emitter.ping()
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
                if stops.hit is not None:
                    continue
                if (
                    part.get("functionCall")
                    or part.get("inlineData")
                    or part.get("thought") is True
                ):
                    # A retained text prefix belongs before this next block.
                    tail = stops.feed("", final=True)
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
                        call_id, name, args, _as_str(part.get("thoughtSignature"))
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
                if isinstance(text, str) and text:
                    if part.get("thought") is True:
                        for event in emitter.thinking_delta(text):
                            yield event
                        signature = part.get("thoughtSignature")
                        if isinstance(signature, str):
                            emitter.thinking_signature = signature
                    else:
                        # stop sequence 命中后丢弃后续正文（Claude 语义是
                        # 文本在 stop sequence 处截断，不透传其后内容）；
                        # 命中点在本帧中间时，命中点之前的文本仍需下发
                        text = stops.feed(text)
                        emitter.stop_hit = stops.hit
                        if not text:
                            continue
                        for event in emitter.text_delta(text):
                            yield event
                elif emitter.open_kind == "thinking" and isinstance(
                    part.get("thoughtSignature"), str
                ):
                    emitter.thinking_signature = str(part["thoughtSignature"])
    except Exception as exc:
        logger.warning("Claude 流式转换异常: %s", exc)
        record_result(
            "error", error=f"Stream conversion failed: {type(exc).__name__}: {exc}"
        )
        for event in emitter.error(500, f"{type(exc).__name__}: {exc}"):
            yield event
        return
    else:
        tail = stops.feed("", final=True)
        if tail:
            for event in emitter.text_delta(tail):
                yield event
        for event in emitter.finish():
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
        or path == "/v1/responses"
        or path == "/v1/models"
        or path == "/v1/messages"
        or path.startswith(("/v1/responses/", "/v1/models/", "/v1/messages/"))
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
