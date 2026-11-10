"""Shared FastAPI dependencies."""

from __future__ import annotations

import secrets
import threading
import time
from functools import lru_cache

from fastapi import HTTPException, Request

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

from .state import runtime_state

logger = get_logger("auth")


@lru_cache(maxsize=32)
def _encode_secret(value: str) -> bytes:
    """缓存凭据的 UTF-8 编码，避免每个请求对每个候选 Key 重复 encode。"""
    return value.encode("utf-8")


@lru_cache(maxsize=32)
def _encoded_keys(keys: frozenset[str]) -> frozenset[bytes]:
    return frozenset(_encode_secret(key) for key in keys)


# ---------------------------------------------------------------------------
# 面板实时日志流（SSE）短时一次性票据
# ---------------------------------------------------------------------------
# 浏览器的 EventSource 无法携带自定义请求头，只能把凭据放进 URL。
# 若直接放管理密码，密码会长期残留在浏览器历史、反向代理访问日志与 Referer 头里。
# 因此改为：前端先用 Authorization 头调用 /api/logs/stream-ticket 换取票据，
# 再把票据附加到 SSE 的 URL 上。票据短时有效且仅能使用一次。

_STREAM_TICKET_TTL_SECONDS = 30.0
_STREAM_TICKET_MAX = 256

_stream_tickets: dict[str, float] = {}
_stream_tickets_lock = threading.Lock()


def issue_stream_ticket() -> str:
    """签发一次性 SSE 票据（调用方必须已通过管理端鉴权）。"""
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    with _stream_tickets_lock:
        # 顺带清理过期票据并限制总量，避免反复索取导致内存无界增长
        for key in [k for k, deadline in _stream_tickets.items() if deadline <= now]:
            _stream_tickets.pop(key, None)
        while len(_stream_tickets) >= _STREAM_TICKET_MAX:
            _stream_tickets.pop(next(iter(_stream_tickets)), None)
        _stream_tickets[ticket] = now + _STREAM_TICKET_TTL_SECONDS
    return ticket


def consume_stream_ticket(ticket: str) -> bool:
    """核销一次性 SSE 票据；核销成功即刻失效。"""
    clean = (ticket or "").strip()
    if not clean:
        return False
    with _stream_tickets_lock:
        deadline = _stream_tickets.pop(clean, None)
    return deadline is not None and deadline > time.monotonic()


def _extract_request_token(request: Request) -> str | None:
    # 1. Google Gemini standard query param: ?key=...
    query_key = request.query_params.get("key")
    if query_key and query_key.strip():
        return query_key.strip()

    # 2. Google Gemini standard header: x-goog-api-key
    goog_key = (request.headers.get("x-goog-api-key") or "").strip()
    if goog_key:
        return goog_key

    # 3. Standard x-api-key header
    api_key = (request.headers.get("x-api-key") or "").strip()
    if api_key:
        return api_key

    # 4. Bearer token in Authorization header
    authorization = (request.headers.get("authorization") or "").strip()
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None

    token = token.strip()
    return token or None


def require_web_auth(request: Request) -> None:
    """保护管理控制台路由 (通过环境变量 AISTUDIO_WEB_PASSWORD 配置密码)。"""
    if not settings.auth_enabled:
        return

    # SSE 端点例外：浏览器 EventSource 无法携带请求头，允许使用一次性票据。
    # 用精确路径比较（容忍尾斜杠），避免 endswith 这类前缀式判断被
    # /api/logs/stream/../accounts 之类的路径绕过或误命中子路径。
    if request.url.path.rstrip("/") == "/api/logs/stream":
        ticket = (request.query_params.get("ticket") or "").strip()
        if ticket and consume_stream_ticket(ticket):
            return

    token = _extract_request_token(request)
    web_password = settings.web_password
    if (
        token
        and web_password
        and web_password.strip()
        and secrets.compare_digest(token.encode("utf-8"), _encode_secret(web_password))
    ):
        return
    client_ip = request.client.host if request.client else "unknown"
    logger.warning("管理控制台鉴权失败: 客户端=%s", client_ip)
    raise HTTPException(
        status_code=401,
        detail="管理控制台鉴权失败，请输入正确的管理密码",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_key(request: Request) -> None:
    """保护对外 Gemini API 路由 (通过 config.yaml 中的 api_keys 配置)。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_keys,
    )

    configured_keys = get_configured_api_keys()
    all_valid_keys = configured_keys | settings.api_keys

    # 如果 config.yaml 与环境变量均未配置任何 Key，则 API 免密开放
    if not all_valid_keys:
        return

    token = _extract_request_token(request)
    if not token:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("API 鉴权失败: 客户端=%s, 缺少 API Key", client_ip)
        raise HTTPException(
            status_code=401,
            detail={
                "message": "Invalid or missing API key. Pass via 'x-goog-api-key' header, 'key' query parameter, or 'Authorization: Bearer <key>'",
                "type": "authentication_error",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 常量时间比较：避免通过逐字符响应时延枚举有效 Key。
    # 用 bytes 形式调用 compare_digest，兼容含非 ASCII 字符的 Key。
    token_bytes = token.encode("utf-8")
    if any(
        secrets.compare_digest(token_bytes, candidate)
        for candidate in _encoded_keys(all_valid_keys)
    ):
        return

    # 匹配管理密码（管理员具备超级 API 访问权限），同样用 bytes 常量时间比较
    web_password = settings.web_password
    if (
        web_password
        and web_password.strip()
        and secrets.compare_digest(token_bytes, _encode_secret(web_password))
    ):
        return
    client_ip = request.client.host if request.client else "unknown"
    logger.warning("API 鉴权失败: 客户端=%s, API Key 无效", client_ip)
    raise HTTPException(
        status_code=401,
        detail={
            "message": "Invalid API key provided",
            "type": "authentication_error",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_client() -> AIStudioClient:
    if runtime_state.client is None:
        logger.error("客户端未初始化，无法处理请求")
        raise HTTPException(
            503,
            detail={"message": "Client not initialized", "type": "service_unavailable"},
        )
    return runtime_state.client


def get_account_service():
    if runtime_state.account_service is None:
        logger.error("账号服务未初始化，无法处理请求")
        raise HTTPException(
            503,
            detail={
                "message": "Account service not initialized",
                "type": "service_unavailable",
            },
        )
    return runtime_state.account_service


def get_runtime_state():
    return runtime_state
