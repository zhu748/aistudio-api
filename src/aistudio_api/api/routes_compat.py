"""OpenAI / Anthropic(Claude) 兼容层路由。

- ``POST /v1/chat/completions`` — OpenAI Chat Completions（流式 / 非流式）
- ``POST /v1/messages`` — Claude Messages API（流式 / 非流式）
- ``POST /v1/messages/count_tokens`` — Claude token 数估算
- ``GET /v1/models`` — 模型列表（按 ``anthropic-version`` 请求头自动区分
  OpenAI / Claude 响应格式）
- ``GET /v1/models/{model_id}`` — 模型详情（同上双格式）

鉴权复用 ``require_api_key``：同时接受 ``Authorization: Bearer``（OpenAI）、
``x-api-key``（Claude）与 ``x-goog-api-key`` / ``?key=``（Gemini）。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aistudio_api.api.dependencies import get_client, get_runtime_state
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
)
from aistudio_api.application.api_service_gemini import handle_gemini_generate_content
from aistudio_api.application.compat_service import (
    build_claude_message,
    build_openai_completion,
    claude_stream_adapter,
    claude_to_gemini_request,
    is_claude_compat_request,
    openai_stream_adapter,
    openai_to_gemini_request,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

from .routes_gemini import _parse_anti_truncation_header

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState

logger = get_logger("routes.compat")
router = APIRouter()

_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _truthy(value: object) -> bool:
    """宽松布尔解析：兼容部分客户端把布尔写成字符串 'true'/'1' 的情况。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "t")
    return value is True


def _shrink_media_payloads(node: object) -> object:
    """count_tokens 估算前把 base64 媒体数据替换为字节占位符。

    直接把几十万字符的 base64 计入字符数会让估算虚高三个数量级（Claude
    Code 等客户端会带图调用 count_tokens）；替换为占位符后估算回到
    文本量级，与真实 token 数量级一致。
    """
    if isinstance(node, dict):
        shrunk: dict[str, object] = {}
        for key, value in node.items():
            if key == "data" and isinstance(value, str) and len(value) > 256:
                shrunk[key] = f"<base64 media: {len(value)} chars>"
            else:
                shrunk[key] = _shrink_media_payloads(value)
        return shrunk
    if isinstance(node, list):
        return [_shrink_media_payloads(item) for item in node]
    return node


def _estimate_tokens(text: str) -> int:
    """按字符类别加权估算 token 数（不依赖外部 tokenizer）。

    经验折算（与主流 BPE 分词器偏差在 ±20% 内）：
    - ASCII 及拉丁字母：约 4 字符 / token
    - CJK（含日文假名、韩文）：约 1 字符 / token
    - 其它非 ASCII（emoji、带变音符的拉丁字母等）：约 1.5 字符 / token

    旧实现统一用 ``len(text) // 4``，对中文低估 4 倍以上。
    """
    ascii_chars = 0
    cjk_chars = 0
    other_chars = 0
    for ch in text:
        code = ord(ch)
        if code < 128:
            ascii_chars += 1
        elif (
            0x3040 <= code <= 0x30FF  # 日文假名
            or 0x3400 <= code <= 0x4DBF  # CJK 扩展 A
            or 0x4E00 <= code <= 0x9FFF  # CJK 基本区
            or 0xAC00 <= code <= 0xD7AF  # 韩文音节
            or 0xF900 <= code <= 0xFAFF  # CJK 兼容表意
        ):
            cjk_chars += 1
        else:
            other_chars += 1
    weighted = ascii_chars / 4.0 + cjk_chars + other_chars / 1.5
    return int(weighted + 0.5)


@router.post("/v1/chat/completions")
async def openai_chat_completions(
    req: OpenAIChatCompletionRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    """OpenAI Chat Completions 兼容端点。"""
    try:
        gemini_req, resolved_model = await openai_to_gemini_request(req)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"message": str(exc), "type": "bad_request"}
        ) from exc

    include_usage = False
    if isinstance(req.stream_options, dict):
        include_usage = _truthy(req.stream_options.get("include_usage"))

    logger.info(
        "OpenAI 兼容请求: model=%s → %s, stream=%s, 轮数=%d",
        req.model,
        resolved_model,
        req.stream,
        len(req.messages),
    )
    response = await handle_gemini_generate_content(
        resolved_model,
        gemini_req,
        client,
        stream=req.stream,
        anti_truncation=_parse_anti_truncation_header(http_request),
    )
    if req.stream:
        if isinstance(response, StreamingResponse):
            return StreamingResponse(
                openai_stream_adapter(
                    response.body_iterator,
                    requested_model=req.model,
                    include_usage=include_usage,
                ),
                media_type="text/event-stream",
                headers=_STREAM_HEADERS,
            )
    elif isinstance(response, GeminiGenerateContentResponse):
        return JSONResponse(
            content=build_openai_completion(response, requested_model=req.model)
        )
    raise HTTPException(
        500,
        detail={"message": "Unexpected upstream response type", "type": "server_error"},
    )


@router.post("/v1/messages")
async def claude_messages(
    req: ClaudeMessagesRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    """Anthropic Claude Messages 兼容端点。"""
    try:
        gemini_req, resolved_model = await claude_to_gemini_request(req)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"message": str(exc), "type": "bad_request"}
        ) from exc

    logger.info(
        "Claude 兼容请求: model=%s → %s, stream=%s, 轮数=%d",
        req.model,
        resolved_model,
        req.stream,
        len(req.messages),
    )
    response = await handle_gemini_generate_content(
        resolved_model,
        gemini_req,
        client,
        stream=req.stream,
        anti_truncation=_parse_anti_truncation_header(http_request),
    )
    if req.stream:
        if isinstance(response, StreamingResponse):
            return StreamingResponse(
                claude_stream_adapter(
                    response.body_iterator,
                    requested_model=req.model,
                    stop_sequences=req.stop_sequences,
                ),
                media_type="text/event-stream",
                headers=_STREAM_HEADERS,
            )
    elif isinstance(response, GeminiGenerateContentResponse):
        return JSONResponse(
            content=build_claude_message(
                response,
                requested_model=req.model,
                stop_sequences=req.stop_sequences,
            )
        )
    raise HTTPException(
        500,
        detail={"message": "Unexpected upstream response type", "type": "server_error"},
    )


@router.post("/v1/messages/count_tokens")
async def claude_count_tokens(req: ClaudeMessagesRequest):
    """Claude token 计数端点：按字符类别加权估算。

    早先实现是 ``len(json.dumps(payload)) // 4``，对中文会低估约 4~6 倍
    （Claude Code 用本端点预判上下文余量，低估会导致实际请求超限）。
    改为显式分类计数：ASCII 约 4 字符/token，CJK 等宽字符约 1 字符/token，
    其它非 ASCII 按 1.5 字符/token 折算，比转义长度启发式更贴近 BPE 实际。
    """
    try:
        payload = {
            "system": _shrink_media_payloads(req.system),
            "messages": [
                {"role": m.role, "content": _shrink_media_payloads(m.content)}
                for m in req.messages
            ],
        }
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(req.messages)
    estimate = max(1, _estimate_tokens(serialized))
    return JSONResponse(content={"input_tokens": estimate})


@router.get("/v1/models")
async def list_models_compat(
    http_request: Request,
    runtime_state: RuntimeState = Depends(get_runtime_state),
):
    """模型列表：OpenAI / Claude 双格式（按 anthropic-version 头自动识别）。"""
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    session = runtime_state.client.session if runtime_state.client else None
    discovered = await model_discovery.get_models(session=session)

    from .routes_models import _expand_anti_truncation_variants, _to_gemini_model

    gemini_models = [_to_gemini_model(item) for item in discovered]
    gemini_models.extend(_expand_anti_truncation_variants(discovered))

    if is_claude_compat_request(http_request):
        data = [
            {
                "type": "model",
                "id": model.name.removeprefix("models/"),
                "display_name": model.displayName or model.name,
                "created_at": "2025-01-01T00:00:00Z",
            }
            for model in gemini_models
        ]
        first_id = data[0]["id"] if data else None
        last_id = data[-1]["id"] if data else None
        return JSONResponse(
            content={
                "data": data,
                "first_id": first_id,
                "has_more": False,
                "last_id": last_id,
            }
        )

    data = [
        {
            "id": model.name.removeprefix("models/"),
            "object": "model",
            "created": 1735689600,
            "owned_by": "google",
        }
        for model in gemini_models
    ]
    return JSONResponse(content={"object": "list", "data": data})


@router.get("/v1/models/{model_id:path}")
async def get_model_compat(
    model_id: str,
    http_request: Request,
    runtime_state: RuntimeState = Depends(get_runtime_state),
):
    """模型详情：OpenAI / Claude 双格式。"""
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    session = runtime_state.client.session if runtime_state.client else None
    model_info = await model_discovery.get_model_by_id(model_id, session=session)
    if model_info is None:
        raise HTTPException(
            status_code=404,
            detail={"message": f"model '{model_id}' not found", "type": "not_found"},
        )

    from .routes_models import _to_gemini_model

    model = _to_gemini_model(model_info)
    clean_id = model.name.removeprefix("models/")
    if is_claude_compat_request(http_request):
        return JSONResponse(
            content={
                "type": "model",
                "id": clean_id,
                "display_name": model.displayName or clean_id,
                "created_at": "2025-01-01T00:00:00Z",
            }
        )
    return JSONResponse(
        content={
            "id": clean_id,
            "object": "model",
            "created": 1735689600,
            "owned_by": "google",
        }
    )
