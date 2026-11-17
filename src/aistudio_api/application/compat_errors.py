"""兼容层错误映射与协议错误载荷。

自 compat_service.py 拆出：OpenAI / Claude 两协议的错误类型码表、
Gemini finishReason 终结语义映射，以及全局异常处理器使用的
路径判定与协议格式化函数。无内部依赖（仅引用 compat_shared 的 coerce）。

- ``_GEMINI_TO_OPENAI_FINISH`` / ``_CLAUDE_REFUSAL_FINISH``：异常终结语义
- ``_OPENAI_ERROR_TYPE`` / ``_CLAUDE_ERROR_TYPE``：HTTP 状态 → 协议错误类型
- ``_CLAUDE_DUMMY_SIGNATURE`` 与 ``_claude_signature`` / ``_read_claude_signature``：
  Claude thinking 占位签名的写入/回读（与占位常量直接绑定）
- ``openai_error_payload`` / ``claude_error_payload`` / ``build_compat_error_response``
  等错误格式化入口（供 app.py / request_middleware.py 按路径分流）
"""

from __future__ import annotations

import base64
import json

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aistudio_api.application.compat_shared import _as_dict, _as_str
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.errors")


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
