"""Gemini-compatible API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.api_service_gemini import handle_gemini_generate_content
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

from .dependencies import get_client
from .schemas import GeminiGenerateContentRequest

logger = get_logger("routes.gemini")
router = APIRouter()

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f"})


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
    logger.debug("收到 generateContent 请求: model=%s", model_path)
    return await handle_gemini_generate_content(
        model_path,
        req,
        client,
        stream=False,
        anti_truncation=_parse_anti_truncation_header(http_request),
    )


@router.post("/v1beta/{model_path:path}:streamGenerateContent")
async def stream_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    logger.debug("收到 streamGenerateContent 请求: model=%s", model_path)
    return await handle_gemini_generate_content(
        model_path,
        req,
        client,
        stream=True,
        anti_truncation=_parse_anti_truncation_header(http_request),
    )
