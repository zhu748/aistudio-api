"""Shared FastAPI dependencies."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from functools import lru_cache

from fastapi import HTTPException, Request

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.request_context import current_request
from aistudio_api.infrastructure.utils.logger import get_logger

from .state import runtime_state

logger = get_logger("auth")


# ---------------------------------------------------------------------------
# 管理端 401 速率限制（3.11）：固定窗口计数器，防止对管理密码的持续爆破。
# 管理端凭据不设上限会配合 0.0.0.0 默认绑定形成爆破面。
# ---------------------------------------------------------------------------
_AUTH_FAIL_WINDOW_SECONDS = 60.0
_AUTH_FAIL_MAX_PER_WINDOW = 10

_auth_fail_counts: dict[str, list[float]] = {}
_auth_fail_lock = threading.Lock()


def _register_auth_failure_and_check(client_ip: str) -> bool:
    """记录一次管理端鉴权失败。返回 True 表示该 IP 已超限应被限流。"""
    now = time.monotonic()
    with _auth_fail_lock:
        hits = [
            t
            for t in _auth_fail_counts.get(client_ip, [])
            if now - t < _AUTH_FAIL_WINDOW_SECONDS
        ]
        hits.append(now)
        _auth_fail_counts[client_ip] = hits
        if len(_auth_fail_counts) > 4096:
            # 防止任意 IP 洪水把计数表撑爆
            stale = [
                k
                for k, v in _auth_fail_counts.items()
                if not v or now - v[-1] > _AUTH_FAIL_WINDOW_SECONDS
            ]
            for k in stale[:1024]:
                _auth_fail_counts.pop(k, None)
        return len(hits) > _AUTH_FAIL_MAX_PER_WINDOW


def _reset_auth_failures(client_ip: str) -> None:
    with _auth_fail_lock:
        _auth_fail_counts.pop(client_ip, None)


def _all_configured_api_keys() -> frozenset[str]:
    """合并 config.yaml 与环境变量中的全部 API Key（结果缓存，P5）。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_keys,
    )

    return _merge_key_sets(get_configured_api_keys(), settings.api_keys)


def web_auth_required() -> bool:
    """管理控制台是否需要登录。

    鉴权对称化（2.1）：只要配置了管理密码或任意 API Key（config.yaml 或
    环境变量），管理端就必须鉴权——旧逻辑仅看管理密码，导致“只配置了
    API Key”这一最常见部署形态下管理面板完全开放。
    """
    return bool((settings.web_password or "").strip() or _all_configured_api_keys())


@lru_cache(maxsize=32)
def _encode_secret(value: str) -> bytes:
    """缓存凭据的 UTF-8 编码，避免每个请求对每个候选 Key 重复 encode。"""
    return value.encode("utf-8")


@lru_cache(maxsize=32)
def _encoded_keys(keys: frozenset[str]) -> frozenset[bytes]:
    return frozenset(_encode_secret(key) for key in keys)


@lru_cache(maxsize=8)
def _merge_key_sets(
    config_keys: frozenset[str], env_keys: frozenset[str]
) -> frozenset[str]:
    """合并 config.yaml 与环境变量 Key 集合（P5）。

    两个输入各自已是 mtime 缓存的稳定对象，配置不变时本缓存命中，
    require_api_key 每请求的集合并集与重编码开销归零；任一侧变化
    （新 frozenset 对象）自动落新缓存项，旧项被 LRU 淘汰。
    """
    return config_keys | env_keys


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


def _extract_request_token(
    request: Request, *, include_query: bool = True
) -> str | None:
    if include_query:
        # 1. Google Gemini standard query param: ?key=...
        # 仅限对外 API 路由使用：查询参数会进入反代访问日志与浏览器历史，
        # 管理密码绝不走此通道（管理端调用 include_query=False）。
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
    """保护管理控制台路由。

    鉴权层级（2.1 对称化）：
    1. 未配置任何凭据：仅当显式启用 AISTUDIO_ALLOW_NO_AUTH 时开放，
       否则拒绝访问；
    2. 已配置管理密码：仅接受管理密码（API Key 持有者不可越权进入管理端）；
    3. 仅配置了 API Key：接受任意有效 API Key 登录管理端（封堵匿名访问洞，
       同时不把未设管理密码的运维锁在面板外）；但 /config* 与 /api-keys*
       的写操作一律拒绝——写 config.yaml 可自签新 Key 完成权限升级，
       该能力必须由管理密码会话独占。

    管理端凭据只接受 Header 形式（3.11）：?key= 查询参数会残留在反代
    访问日志与浏览器历史中，SSE 票据端点除外。
    """
    web_password = (settings.web_password or "").strip()
    if not web_password and not _all_configured_api_keys():
        if settings.allow_no_auth:
            return
        raise HTTPException(503, "Authentication is not configured")

    client_ip = request.client.host if request.client else "unknown"

    # SSE 端点例外：浏览器 EventSource 无法携带请求头，允许使用一次性票据。
    # 用精确路径比较（容忍尾斜杠），避免 endswith 这类前缀式判断被
    # /api/logs/stream/../accounts 之类的路径绕过或误命中子路径。
    if request.url.path.rstrip("/") == "/api/logs/stream":
        ticket = (request.query_params.get("ticket") or "").strip()
        if ticket and consume_stream_ticket(ticket):
            _reset_auth_failures(client_ip)
            return

    # 管理端不采信 ?key= 查询参数，只接受 Header 凭据
    token = _extract_request_token(request, include_query=False)
    if not token:
        # 无凭据请求不进入失败计数：反代后所有用户共享同一个 client_ip，
        # 匿名刷请求会让合法用户的面板被锁定 60 秒（DoS 放大）
        raise HTTPException(
            status_code=401,
            detail="管理控制台鉴权失败，请输入正确的管理密码",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if _register_auth_failure_and_check(client_ip):
        raise HTTPException(
            status_code=429,
            detail="管理端鉴权失败次数过多，请 1 分钟后重试",
        )

    if web_password:
        if secrets.compare_digest(token.encode("utf-8"), _encode_secret(web_password)):
            _reset_auth_failures(client_ip)
            return
    else:
        # 无管理密码时的对称回退：任意有效 API Key 可进入管理端（只读）
        token_bytes = token.encode("utf-8")
        if any(
            secrets.compare_digest(token_bytes, candidate)
            for candidate in _encoded_keys(_all_configured_api_keys())
        ):
            req_method = getattr(request, "method", "GET")
            if req_method in ("POST", "PUT", "DELETE", "PATCH") and (
                request.url.path.rstrip("/").startswith("/config")
                or request.url.path.rstrip("/").startswith("/api-keys")
            ):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "敏感配置写操作需要先设置管理密码"
                        "（AISTUDIO_WEB_PASSWORD）并使用管理密码会话"
                    ),
                )
            _reset_auth_failures(client_ip)
            return
    logger.warning("管理控制台鉴权失败: 客户端=%s", client_ip)
    raise HTTPException(
        status_code=401,
        detail="管理控制台鉴权失败，请输入正确的管理密码",
        headers={"WWW-Authenticate": "Bearer"},
    )


def mask_api_key(key: str) -> str:
    """脱敏展示 API Key：保留首尾少量字符，中间以 *** 代替。"""
    clean = (key or "").strip()
    if not clean:
        return ""
    if len(clean) <= 8:
        return f"{clean[:2]}***{clean[-2:]}" if len(clean) > 4 else clean[:1] + "***"
    return f"{clean[:6]}***{clean[-4:]}"


def is_management_session(request: Request) -> bool:
    """当前请求是否持有管理级凭据（层级判定与 require_web_auth 一致）。

    供 force_refresh 等仅限管理端的能力做只读校验，不产生 401 副作用。
    """
    if not web_auth_required():
        # 未配置任何凭据的开放部署：一切能力本来就开放
        return True
    token = _extract_request_token(request, include_query=False)
    if not token:
        return False
    web_password = (settings.web_password or "").strip()
    if web_password:
        return secrets.compare_digest(
            token.encode("utf-8"), _encode_secret(web_password)
        )
    token_bytes = token.encode("utf-8")
    return any(
        secrets.compare_digest(token_bytes, candidate)
        for candidate in _encoded_keys(_all_configured_api_keys())
    )


def is_admin_password_session(request: Request) -> bool:
    """当前请求是否以管理密码（而非 API Key 回退）通过管理端鉴权。

    用于敏感只读端点（/api-keys、/config）判定是否返回 Key 明文：
    仅配置了 API Key 的部署下，任意 Key 持有者不应看到其他 Key 的明文。
    """
    web_password = (settings.web_password or "").strip()
    if not web_password:
        return False
    token = _extract_request_token(request, include_query=False)
    return bool(
        token
        and secrets.compare_digest(token.encode("utf-8"), _encode_secret(web_password))
    )


def require_api_key(request: Request) -> None:
    """保护对外 Gemini API 路由 (通过 config.yaml 中的 api_keys 配置)。"""
    all_valid_keys = _all_configured_api_keys()

    web_password = (settings.web_password or "").strip()
    if not all_valid_keys and not web_password and settings.allow_no_auth:
        return
    if not all_valid_keys and not web_password:
        raise HTTPException(503, "Authentication is not configured")

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
        context = current_request.get()
        if context is not None:
            context.key_id = hashlib.sha256(token_bytes).hexdigest()[:12]
        return

    # 匹配管理密码（管理员具备超级 API 访问权限），同样用 bytes 常量时间比较
    web_password = settings.web_password
    if (
        web_password
        and web_password.strip()
        and secrets.compare_digest(token_bytes, _encode_secret(web_password))
    ):
        context = current_request.get()
        if context is not None:
            context.key_id = "admin"
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
