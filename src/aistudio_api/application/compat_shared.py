"""兼容层共享辅助（媒体 / 签名 / 数值 coerce / SSE 帧解析 / 模型映射）。

自 compat_service.py 收敛拆出：OpenAI 与 Claude 两侧转换器（及 Responses 层）
共用的基础工具全部集中于此，避免 compat_openai / compat_claude 互相 import：

- 数值与结构 coerce：``_as_dict`` / ``_as_str`` / ``_as_int``
- 媒体管线：data URL 解析、魔数嗅探、体积限额、远程下载与并发分批
- thought signature 透传/回读（OpenAI 与 Claude 两种 wire 形态）
- 流适配器生命周期骨架与 Gemini SSE 帧解析（三协议共用）
- ``resolve_compat_model`` 及 OpenAI / Claude 家族正则
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import json
import re
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    Awaitable,
    Callable,
    Coroutine,
)
from typing import cast

import httpx

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import GeminiInlineData, GeminiPart
from aistudio_api.application.anti_truncation import _close_stream_quietly
from aistudio_api.application.media_session import (
    current_media,
    media_session,
)
from aistudio_api.application.stream_utils import (
    SSEEventBuffer,
    coalesce_tool_frames,
    part_payload_signature,
)
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    remember_function_signature as _remember_function_signature,
    valid_thought_signature,
)
from aistudio_api.infrastructure.gateway.model_defaults import get_compat_settings
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.shared")


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

# 聚合网关（OpenRouter / Cline / Bedrock 自定义端点等）常用的 provider 前缀：
# "anthropic/claude-sonnet-4-5" 这类名字剥前缀后再走映射/家族回退，否则
# 原样透传上游必然 404（家族正则只认 ^ 或 . 边界，不认 / 边界）。
_PROVIDER_PREFIXES = (
    "anthropic/",
    "openai/",
    "google/",
    "gcp/",
    "meta/",
    "mistral/",
    "mistralai/",
    "qwen/",
    "deepseek/",
    "zhipu/",
    "moonshot/",
    "moonshotai/",
    "grok/",
    "x-ai/",
    "xai/",
    "amazon/",
    "openrouter/",
)

# 魔数嗅探：无 Content-Type / 无 data URL mime 时的兜底识别
_MAGIC_SNIFFERS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

# RFC 3986：scheme 大小写不敏感（"DATA:image/png;base64,..." 同样合法）；
# 参数允许带引号（;name="x";base64,...）。
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;[\w=-]+|;\"[^\"]*\")*;base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


# 远程媒体（http/https 下载）的分批并发上限：与媒体会话 semaphore 共同约束
# 单请求同时在途的下载数，4 是移动网络下的经验安全值。
MEDIA_CONCURRENCY = 4


async def _convert_items_concurrently(
    items: list[object],
    convert_one: Callable[[list[object]], Awaitable[list[GeminiPart]]],
    *,
    concurrency: int = MEDIA_CONCURRENCY,
) -> list[GeminiPart]:
    """分批并发转换媒体条目（每批最多 ``concurrency`` 个）。

    ``convert_one`` 收到单元素列表（与各 ``_*_parts_from_content`` 的入参
    形态一致，递归触发远端下载与体积校验）；批内任一 ValueError 透传首个
    消息，与原先 ``except* ValueError`` 行为一致。
    """
    parts: list[GeminiPart] = []
    for offset in range(0, len(items), concurrency):
        try:
            async with asyncio.TaskGroup() as group:
                # create_task 需要 Coroutine 而非裸 Awaitable（TaskGroup 泛型
                # 协变限制），这里用 cast 收窄协程类型。
                tasks = [
                    group.create_task(
                        cast(
                            "Coroutine[object, object, list[GeminiPart]]",
                            convert_one([item]),
                        )
                    )
                    for item in items[offset : offset + concurrency]
                ]
        except* ValueError as exc:
            raise ValueError(str(exc.exceptions[0])) from exc
        for task in tasks:
            parts.extend(task.result())
    return parts


def _file_block_to_part(file_data: str, filename: object) -> GeminiPart | None:
    """OpenAI ``file`` / Responses ``input_file`` 的 data URL → inlineData part。

    filename 透传到 wire inlineData 槽位 5（2026-11 AI Studio 真机抓包
    形态），空串视为未提供；非 data URL 形态返回 None（忽略）。
    """
    if not file_data.startswith("data:"):
        return None
    parsed = _parse_data_url(file_data)
    if parsed is None:
        return None
    mime, data = parsed
    clean = _as_str(filename).strip()
    return GeminiPart(
        inlineData=GeminiInlineData(mimeType=mime, data=data, filename=clean or None)
    )


@contextlib.asynccontextmanager
async def _stream_adapter_lifecycle(
    source: AsyncIterable[object],
    failure_events: Callable[[str], list[str]],
    register_failure: Callable[[Callable[[str], list[str]]], None] | None,
) -> AsyncGenerator[None]:
    """三个协议流适配器（OpenAI / Claude / Responses）共用的生命周期骨架。

    - 进入时把 ``failure_events``（message → 各协议自己的错误帧列表）注册
      给 ``register_failure``（上游重试耗尽后由传输层调用收束流）；
    - 退出时安静关闭内层 SSE 生成器（3.6）：适配器自身异常或客户端断连
      时，不再依赖事件循环的异步生成器终结器延迟释放上游连接。
    """
    if register_failure is not None:
        register_failure(failure_events)
    try:
        yield
    finally:
        await _close_stream_quietly(source)


def _signature_extra(signature: object) -> dict[str, object]:
    """Opaque Gemini metadata which clients can return with tool history."""
    if isinstance(signature, str) and signature:
        return {"extra_content": {"google": {"thought_signature": signature}}}
    return {}


def _is_signed_empty_thought(text: object, signature: object) -> bool:
    """Only an explicitly empty string with an opaque byte value is replayable."""
    return (
        text == ""
        and signature != SKIP_THOUGHT_SIGNATURE
        and valid_thought_signature(signature)
    )


def _read_signature(item: dict[str, object]) -> str | None:
    google = _as_dict(_as_dict(item.get("extra_content")).get("google"))
    return (
        _as_str(google.get("thought_signature"))
        or _as_str(google.get("thoughtSignature"))
        or _as_str(item.get("thought_signature"))
        or _as_str(item.get("thoughtSignature"))
        or None
    )


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


def resolve_compat_model(requested: str, *, channel: str) -> str:
    """解析兼容层模型名：配置映射（精确 + 最长前缀）→ 家族回退 → 原样透传。"""
    clean = (requested or "").strip().removeprefix("models/")
    if not clean:
        raise ValueError("model is required")
    # provider 前缀剥离（OpenRouter / Cline 等聚合客户端）：仅认白名单前缀，
    # 避免误伤含斜杠的自定义模型名；剥完为空则回退原名。
    for prefix in _PROVIDER_PREFIXES:
        if clean.lower().startswith(prefix):
            stripped = clean[len(prefix) :]
            if stripped:
                clean = stripped
            break
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
    for part in parts:
        function_call = getattr(part, "functionCall", None)
        if function_call is not None:
            _remember_function_signature(
                getattr(function_call, "id", None),
                getattr(function_call, "name", None),
                getattr(function_call, "args", None),
                part_payload_signature(part, "functionCall"),
            )
    return parts


async def _iter_gemini_sse_frames(
    source: AsyncIterable[object],
) -> AsyncGenerator[dict[str, object], None]:
    async for frame in coalesce_tool_frames(_parse_gemini_sse_frames(source)):
        _remember_frame_signatures(frame)
        yield frame


def _remember_frame_signatures(frame: dict[str, object]) -> None:
    for part in _frame_parts(_frame_candidate(frame)):
        function_call = _as_dict(part.get("functionCall"))
        if function_call:
            _remember_function_signature(
                _as_str(function_call.get("id")) or None,
                _as_str(function_call.get("name")) or None,
                function_call.get("args"),
                part_payload_signature(part, "functionCall"),
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
    raw = content.get("parts")
    if not isinstance(raw, list):
        return []
    parts: list[dict[str, object]] = []
    for part in raw:
        if not isinstance(part, dict):
            continue
        code = _code_text(part.get("executableCode"), part.get("codeExecutionResult"))
        payloads = [
            key for key in ("text", "inlineData", "functionCall") if key in part
        ]
        if len(payloads) < 2 and not code:
            parts.append(part)
            continue
        for key in payloads:
            # Each split payload is a projection, not the signed Gemini Part.
            separate = {key: part[key]}
            if key != "functionCall" and part.get("thought") is True:
                separate["thought"] = True
            parts.append(separate)
        if code:
            parts.append({"text": code})
    return parts


def _code_text(executable: object, result: object) -> str:
    """Present native code tool payloads as plain output for other protocols."""
    return "\n".join(
        text
        for value, key in ((executable, "code"), (result, "output"))
        if isinstance(
            text := value.get(key)
            if isinstance(value, dict)
            else getattr(value, key, None),
            str,
        )
        and text
    )


__all__ = [
    "MEDIA_CONCURRENCY",
    "resolve_compat_model",
]
