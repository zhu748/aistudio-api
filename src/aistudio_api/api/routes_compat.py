"""OpenAI / Anthropic(Claude) 兼容层路由。

- ``POST /v1/chat/completions`` — OpenAI Chat Completions（流式 / 非流式）
- ``POST /v1/responses`` — OpenAI Responses API（流式 / 非流式）
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
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aistudio_api.api.dependencies import get_client, get_runtime_state
from aistudio_api.api.keepalive import (
    STREAM_HEADERS,
    HeartbeatStreamingResponse,
    keepalive_response,
)
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application.api_service_gemini import (
    GeminiStreamResponse,
    handle_gemini_generate_content,
)
from aistudio_api.application.compat_openai import _normalized_stop_sequences
from aistudio_api.application.compat_service import (
    _openai_reasoning_effort,
    build_claude_message,
    build_openai_completion,
    claude_stream_adapter,
    claude_to_gemini_request,
    is_claude_compat_request,
    openai_stream_adapter,
    openai_to_gemini_request,
)
from aistudio_api.application.compat_shared import _DATA_URL_RE
from aistudio_api.application.responses_delivery import ResponsesStreamDelivery
from aistudio_api.application.responses_service import (
    ResponsesToolMetadata,
    build_responses_object,
    build_responses_preparation_failure,
    responses_stream_adapter,
    responses_to_gemini_request,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.request_context import (
    current_request,
    record_requested_parameters,
)
from aistudio_api.infrastructure.utils.logger import get_logger

from .routes_gemini import _parse_anti_truncation_header

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState

logger = get_logger("routes.compat")
router = APIRouter()

_STREAM_HEADERS = STREAM_HEADERS


def _truthy(value: object) -> bool:
    """宽松布尔解析：兼容部分客户端把布尔写成字符串 'true'/'1' 的情况。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "t")
    return False


_MEDIA_BASE64 = re.compile(r"[A-Za-z0-9+/]++={0,2}")


def _shrink_media_block(block: object) -> object:
    """Project just the encoded value of an inline Claude image or PDF block."""
    if not isinstance(block, dict) or block.get("type") not in ("image", "document"):
        return block
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") not in ("base64", "url"):
        return block
    key = "data" if source["type"] == "base64" else "url"
    value = source.get(key)
    if not isinstance(value, str) or len(value) <= 256:
        return block

    image = block["type"] == "image"
    mime = source.get("media_type", "image/png" if image else "application/pdf")
    encoded, start = value, 0
    if key == "url":
        # Use the converter's URL syntax, but never fetch, decode or charge media here.
        encoded = value.strip()
        match = _DATA_URL_RE.fullmatch(encoded)
        if match is None:
            return block
        mime, start = match["mime"], match.start("data")
    if not isinstance(mime, str) or not (
        mime.lower().startswith("image/")
        if image
        else mime.lower() == "application/pdf"
    ):
        return block
    if _MEDIA_BASE64.fullmatch(encoded, start) is None:
        return block
    return {**block, "source": {**source, key: f"<base64 media: {len(value)} chars>"}}


def _shrink_media_payloads(content: object) -> object:
    """Visit only message content blocks and a tool result's immediate content blocks."""
    if not isinstance(content, list):
        return content
    projected: list[object] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and isinstance(items := block.get("content"), list)
        ):
            projected.append(
                {**block, "content": [_shrink_media_block(i) for i in items]}
            )
        else:
            projected.append(_shrink_media_block(block))
    return projected


# CJK 单字符区间（与 _estimate_tokens 的加权估算配套）
_CJK_CHAR_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]"
)
# ASCII 折算除数（按 Claude Code 真实流量对照 Gemini promptTokenCount 校准，见 _estimate_tokens）
_ASCII_CHARS_PER_TOKEN = 3.5


def _estimate_tokens(text: str) -> int:
    """按字符类别加权估算 token 数（不依赖外部 tokenizer）。

    经验折算（与主流 BPE 分词器偏差在 ±20% 内）：
    - ASCII 及拉丁字母：约 3.5 字符 / token
    - CJK（含日文假名、韩文）：约 1 字符 / token
    - 其它非 ASCII（emoji、带变音符的拉丁字母等）：约 1.5 字符 / token

    旧实现统一用 ``len(text) // 4``，对中文低估 4 倍以上。
    ASCII 除数由 4.0 校准为 3.5：Claude Code 流量以代码/JSON 为主，
    两份真实抓包（357KB 客户端体、1.49MB 上游线缆体）对照 Gemini 返回的
    promptTokenCount，4.0 分别低估 16% / 11%，3.5 时偏差在 ±2% 内。
    计数全部下沉到 C 层（P6）：逐字符 Python 循环在数 MB 文本上会
    占满 CPU 数秒——ASCII 用 encode-ignore 计数，CJK 用单字符正则
    sub 前后长度差计数，均无逐字符解释器开销。
    """
    if not text:
        return 0
    total = len(text)
    ascii_chars = len(text.encode("ascii", errors="ignore"))
    if ascii_chars == total:
        # 纯 ASCII 快路径：免做 CJK 正则
        return int(total / _ASCII_CHARS_PER_TOKEN + 0.5)
    cjk_chars = total - len(_CJK_CHAR_RE.sub("", text))
    other_chars = max(0, (total - ascii_chars) - cjk_chars)
    weighted = ascii_chars / _ASCII_CHARS_PER_TOKEN + cjk_chars + other_chars / 1.5
    return int(weighted + 0.5)


@router.post("/v1/chat/completions")
async def openai_chat_completions(
    req: OpenAIChatCompletionRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    """OpenAI Chat Completions 兼容端点。"""
    # 思考强度统一归一化后记录（优先），同时保留客户端原始形态便于面板排查
    record_requested_parameters(
        {
            "reasoning_effort": _openai_reasoning_effort(req) or req.reasoning_effort,
            "reasoning": req.reasoning,
            "thinking": req.thinking,
            "enable_thinking": req.enable_thinking,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "max_tokens": req.max_tokens,
            "max_completion_tokens": req.max_completion_tokens,
        }
    )
    observation = current_request.get()
    if observation is not None:
        observation.requested_model = req.model
        observation.is_stream = req.stream

    async def respond():
        try:
            gemini_req, resolved_model = await openai_to_gemini_request(req)
        except ValueError as exc:
            raise HTTPException(
                400, detail={"message": str(exc), "type": "bad_request"}
            ) from exc

        include_usage = False
        if isinstance(req.stream_options, dict):
            include_usage = _truthy(req.stream_options.get("include_usage"))
        # 响应侧停止词防御需要原始 stop（上游不执行 stopSequences 时截断）
        stop_sequences = _normalized_stop_sequences(req.stop)

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
                return HeartbeatStreamingResponse(
                    openai_stream_adapter(
                        response.frames
                        if isinstance(response, GeminiStreamResponse)
                        else response.body_iterator,
                        requested_model=req.model,
                        include_usage=include_usage,
                        stop_sequences=stop_sequences,
                        register_failure=lambda handler: setattr(
                            http_request.state, "stream_failure", handler
                        ),
                    ),
                    media_type="text/event-stream",
                    headers=_STREAM_HEADERS,
                )
        elif isinstance(response, GeminiGenerateContentResponse):
            return JSONResponse(
                content=build_openai_completion(
                    response,
                    requested_model=req.model,
                    stop_sequences=stop_sequences,
                )
            )
        raise HTTPException(
            500,
            detail={
                "message": "Unexpected upstream response type",
                "type": "server_error",
            },
        )

    return await keepalive_response(respond, http_request, stream=req.stream)


@router.post("/v1/responses")
async def openai_responses(
    req: OpenAIResponsesRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    """OpenAI Responses API 兼容端点（openai-python / Codex CLI 等）。"""
    record_requested_parameters(
        {
            "reasoning": req.reasoning,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "max_output_tokens": req.max_output_tokens,
        }
    )
    observation = current_request.get()
    if observation is not None:
        observation.requested_model = req.model
        observation.is_stream = req.stream

    def register_failure(handler: Callable[[str], list[str]]) -> None:
        http_request.state.stream_failure = handler

    delivery = None
    if req.stream and hasattr(http_request.state, "responses_accept_event"):
        delivery = ResponsesStreamDelivery()
        http_request.state.responses_accept_event = delivery.accept

    # The pre-conversion error callback needs only these fields, never input
    # history or attachments. Release the raw tool fields once converted.
    model = req.model
    unprepared_tools = req.tools, req.tool_choice
    tool_metadata: ResponsesToolMetadata | None = None

    def preparation_error(code: int, message: str) -> list[str]:
        return build_responses_preparation_failure(
            model,
            code,
            message,
            register_failure=register_failure,
            delivery=delivery,
            tool_metadata=tool_metadata
            or ResponsesToolMetadata.before_conversion(*unprepared_tools),
        )

    async def respond():
        nonlocal tool_metadata, unprepared_tools
        try:
            gemini_req, resolved_model = await responses_to_gemini_request(req)
        except ValueError as exc:
            raise HTTPException(
                400, detail={"message": str(exc), "type": "bad_request"}
            ) from exc

        tool_metadata = ResponsesToolMetadata.from_gemini(
            gemini_req.tools, gemini_req.toolConfig
        )
        unprepared_tools = (None, None)
        input_desc = "string" if isinstance(req.input, str) else "items"
        logger.info(
            "Responses 兼容请求: model=%s → %s, stream=%s, input=%s",
            req.model,
            resolved_model,
            req.stream,
            input_desc,
        )

        response = await handle_gemini_generate_content(
            resolved_model,
            gemini_req,
            client,
            stream=req.stream,
            anti_truncation=_parse_anti_truncation_header(http_request),
        )
        include_encrypted_reasoning = bool(
            req.include and "reasoning.encrypted_content" in req.include
        )
        if req.stream:
            if isinstance(response, StreamingResponse):
                return HeartbeatStreamingResponse(
                    responses_stream_adapter(
                        response.frames
                        if isinstance(response, GeminiStreamResponse)
                        else response.body_iterator,
                        requested_model=req.model,
                        register_failure=register_failure,
                        delivery=delivery,
                        include_encrypted_reasoning=include_encrypted_reasoning,
                        tool_metadata=tool_metadata,
                    ),
                    media_type="text/event-stream",
                    headers=_STREAM_HEADERS,
                )
        elif isinstance(response, GeminiGenerateContentResponse):
            return JSONResponse(
                content=build_responses_object(
                    response,
                    requested_model=req.model,
                    include_encrypted_reasoning=include_encrypted_reasoning,
                    tool_metadata=tool_metadata,
                )
            )
        raise HTTPException(
            500,
            detail={
                "message": "Unexpected upstream response type",
                "type": "server_error",
            },
        )

    return await keepalive_response(
        respond,
        http_request,
        stream=req.stream,
        preparation_error=preparation_error,
    )


@router.post("/v1/messages")
async def claude_messages(
    req: ClaudeMessagesRequest,
    http_request: Request,
    client: AIStudioClient = Depends(get_client),
):
    """Anthropic Claude Messages 兼容端点。"""
    record_requested_parameters(
        {
            "thinking": req.thinking,
            # Claude effort 协议：Claude Code Max 档的思考强度在
            # output_config.effort，记录进请求详情便于核对映射结果。
            "output_config": req.output_config,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "top_k": req.top_k,
            "max_tokens": req.max_tokens,
        }
    )
    observation = current_request.get()
    if observation is not None:
        observation.requested_model = req.model
        observation.is_stream = req.stream

    async def respond():
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
            include_start_usage=req.stream,
        )
        if req.stream:
            if isinstance(response, StreamingResponse):
                return HeartbeatStreamingResponse(
                    claude_stream_adapter(
                        response.frames
                        if isinstance(response, GeminiStreamResponse)
                        else response.body_iterator,
                        requested_model=req.model,
                        stop_sequences=req.stop_sequences,
                        register_failure=lambda handler: setattr(
                            http_request.state, "stream_failure", handler
                        ),
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
            detail={
                "message": "Unexpected upstream response type",
                "type": "server_error",
            },
        )

    return await keepalive_response(respond, http_request, stream=req.stream)


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
            "system": req.system,
            # tools 声明同样计入 input_tokens（Claude 官方口径）：Claude
            # Code 依赖本端点预判上下文余量，漏计会在工具多时系统性低估
            "tools": req.tools,
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
