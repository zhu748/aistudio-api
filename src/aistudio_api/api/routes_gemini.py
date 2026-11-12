"""Gemini-compatible API routes."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.api_service_gemini import (
    handle_gemini_count_tokens,
    handle_gemini_generate_content,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

from .dependencies import get_client
from .schemas import GeminiCountTokensRequest, GeminiGenerateContentRequest

logger = get_logger("routes.gemini")
router = APIRouter()

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f"})
# 日志注入防护（L6）：model_path 来自 URL，未过滤时控制字符可注入伪造日志行
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


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


@router.post(
    "/v1beta/{model_path:path}:generateContent",
    response_model=GeminiGenerateContentResponse,
)
async def generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    clean_model = _sanitize_model_path(model_path)
    logger.debug("收到 generateContent 请求: model=%s", clean_model)
    return await handle_gemini_generate_content(
        clean_model,
        req,
        client,
        stream=False,
        anti_truncation=_parse_anti_truncation_header(http_request),
    )


@router.post("/v1beta/{model_path:path}:countTokens")
async def count_tokens(
    model_path: str,
    req: GeminiCountTokensRequest,
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
    req: GeminiGenerateContentRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    clean_model = _sanitize_model_path(model_path)
    logger.debug("收到 streamGenerateContent 请求: model=%s", clean_model)
    return await handle_gemini_generate_content(
        clean_model,
        req,
        client,
        stream=True,
        anti_truncation=_parse_anti_truncation_header(http_request),
    )
