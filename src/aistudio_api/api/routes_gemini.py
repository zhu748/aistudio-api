"""Gemini-compatible API routes."""

from __future__ import annotations

import re
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator

from aistudio_api.api.keepalive import keepalive_response
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.api_service_gemini import (
    handle_gemini_count_tokens,
    handle_gemini_generate_content,
    record_native_gemini_parts,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.signature_trace import (
    get_signature_trace,
    set_trace_protocol,
)
from aistudio_api.infrastructure.utils.logger import get_logger

from .dependencies import get_client
from .schemas import GeminiCountTokensRequest, GeminiGenerateContentRequest

logger = get_logger("routes.gemini")
router = APIRouter()

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f"})
# 日志注入防护（L6）：model_path 来自 URL，未过滤时控制字符可注入伪造日志行
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
# @google/genai uses Node's process.version here, but sends gl-node/web in browsers.
_NODE_RUNTIME_RE = re.compile(r"gl-node/v[0-9]+\.[0-9]+\.[0-9]+(?:[-+].+)?")


_VIDEO_SETTINGS = ("startOffset", "start_offset", "endOffset", "end_offset", "fps")
_RESOLUTION_SETTINGS = ("level", "numTokens", "num_tokens")
_PART_MEDIA_SETTINGS = (
    ("videoMetadata", _VIDEO_SETTINGS),
    ("video_metadata", _VIDEO_SETTINGS),
    ("mediaResolution", _RESOLUTION_SETTINGS),
    ("media_resolution", _RESOLUTION_SETTINGS),
)


def _check_content_media(content: object, path: str, turn: int | None = None) -> None:
    if not isinstance(content, dict) or not isinstance(
        parts := content.get("parts"), list
    ):
        return
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        for name, fields in _PART_MEDIA_SETTINGS:
            options = part.get(name)
            if not isinstance(options, dict):
                continue
            for field in fields:
                value = options.get(field)
                if isinstance(value, str):
                    value = value.strip()
                    if not value:
                        continue
                elif isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if field == "level" and value in (
                    0,
                    "0",
                    "MEDIA_RESOLUTION_UNSPECIFIED",
                ):
                    continue
                # Report only the field path, never the Part's media or user data.
                location = path if turn is None else f"{path}[{turn}]"
                raise HTTPException(
                    400,
                    detail=f"{location}.parts[{index}].{name}.{field} is not supported on a Gemini Part",
                )


def _check_native_media(body: object, *, count: bool = False) -> object:
    """Check only direct Content.parts before GeminiPart drops unsupported fields."""
    if not isinstance(body, dict):
        return body
    contents = body.get("contents")
    contents_path = "contents"
    generation = body
    prefix = ""
    if count:
        # Count consumes top-level contents if present, otherwise the wrapped
        # contents, plus the wrapped system instruction in either case.
        key = (
            "generateContentRequest"
            if "generateContentRequest" in body
            else "generate_content_request"
        )
        generation = body.get(key)
        prefix = f"{key}."
        if not contents and isinstance(generation, dict):
            contents = generation.get("contents")
            contents_path = prefix + "contents"
    if isinstance(contents, list):
        for i, content in enumerate(contents):
            _check_content_media(content, contents_path, i)
    if isinstance(generation, dict):
        key = (
            "systemInstruction"
            if "systemInstruction" in generation
            else "system_instruction"
        )
        _check_content_media(generation.get(key), prefix + key)
    return body


_NativeGenerate = Annotated[
    GeminiGenerateContentRequest, BeforeValidator(_check_native_media)
]
_NativeCount = Annotated[
    GeminiCountTokensRequest, BeforeValidator(partial(_check_native_media, count=True))
]


def _sanitize_model_path(model_path: str) -> str:
    """清洗路径参数中的控制字符，避免日志伪造（CRLF 注入）。"""
    return _CONTROL_CHARS_RE.sub("", model_path)


def _parse_anti_truncation_header(request: Request) -> bool | None:
    """解析 ``X-Anti-Truncation`` 请求头：真值强制开启，假值强制关闭，
    缺省或无法识别时返回 None（跟随 config.yaml 全局配置）。"""
    value = request.headers.get("x-anti-truncation")
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return None


def _needs_sdk_terminal_part(request: Request) -> bool:
    # The Python and JavaScript GenAI chat SDKs drop a turn with an empty final
    # candidate. Require both SDK and runtime labels in the same header.
    for name in ("x-goog-api-client", "user-agent"):
        labels = request.headers.get(name, "").lower().split()
        if any(label.startswith("google-genai-sdk/") for label in labels) and any(
            label.startswith("gl-python/")
            or _NODE_RUNTIME_RE.fullmatch(label)
            or label == "gl-node/web"
            for label in labels
        ):
            return True
    return False


@router.post(
    "/v1beta/{model_path:path}:generateContent",
    response_model=GeminiGenerateContentResponse,
)
async def generate_content(
    model_path: str,
    req: _NativeGenerate,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    clean_model = _sanitize_model_path(model_path)
    set_trace_protocol("gemini")
    logger.debug("收到 generateContent 请求: model=%s", clean_model)

    async def respond():
        response = await handle_gemini_generate_content(
            clean_model,
            req,
            client,
            stream=False,
            anti_truncation=_parse_anti_truncation_header(http_request),
            migrate_history=True,
        )
        assert isinstance(response, GeminiGenerateContentResponse)
        if response.candidates and response.candidates[0].content:
            record_native_gemini_parts(response.candidates[0].content.parts)
        if (trace := get_signature_trace()) is not None:
            trace.emit_inbound_log()
        # An absent field and a present empty-text signature carrier are
        # different Parts. Serialize only fields belonging to that Part.
        return JSONResponse(response.model_dump(mode="json", exclude_none=True))

    return await keepalive_response(respond, http_request)


@router.post("/v1beta/{model_path:path}:countTokens")
async def count_tokens(
    model_path: str,
    req: _NativeCount,
    client: AIStudioClient = Depends(get_client),
):
    """Gemini 协议 ``models.countTokens``：转发到上游真实 CountTokens 端点。

    2026-11 HAR 实测：上游该端点无需 WAA snapshot，响应首位即
    AI Studio 界面展示的 token 总数（含 1 个固定开销 token）。
    """
    clean_model = _sanitize_model_path(model_path)
    logger.debug("收到 countTokens 请求: model=%s", clean_model)
    return await handle_gemini_count_tokens(clean_model, req, client)


@router.post("/v1beta/{model_path:path}:streamGenerateContent")
async def stream_generate_content(
    model_path: str,
    req: _NativeGenerate,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    clean_model = _sanitize_model_path(model_path)
    set_trace_protocol("gemini")
    logger.debug("收到 streamGenerateContent 请求: model=%s", clean_model)
    return await handle_gemini_generate_content(
        clean_model,
        req,
        client,
        stream=True,
        anti_truncation=_parse_anti_truncation_header(http_request),
        sdk_terminal=_needs_sdk_terminal_part(http_request),
        migrate_history=True,
    )
