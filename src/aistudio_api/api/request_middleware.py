"""Observe complete ASGI response lifetimes without buffering streaming output."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO, cast
from uuid import uuid4

import anyio
from fastapi import HTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import (
    RequestContext,
    current_request,
    register_request,
    request_event,
    request_snapshot,
    unregister_request,
)
from aistudio_api.infrastructure.request_history import request_history
from aistudio_api.infrastructure.utils.logger import (
    _mask_json_value,
    _mask_sensitive_value,
    _mask_url,
    dump_request_exchange,
    get_logger,
    is_dump_requests_enabled,
    is_dump_to_file_enabled,
)
from aistudio_api.infrastructure.utils.redaction import redact_text

logger = get_logger("requests")
_DOWNSTREAM_DISCONNECT = "Downstream disconnected (client or reverse proxy)"
# 大请求体（>64KB）首个 64KB 前缀内轻量嗅探顶层 "model": "..."，
# 让提前返回 400/413 的调用仍能在请求历史里看到模型名。
_MODEL_SNIFF_RE = re.compile(rb'"model"\s*:\s*"([^"\\]{1,256})"')


# ── 小内存容器的 RSS 高水位治理 ──────────────────────────────────────────────
# 长代理会话（Claude Code 等）的请求体随轮次增长到 MB 级，多份正文/协议树
# 拷贝反复分配释放后，glibc 堆把空闲页留在高水位不归还 OS；512MB 档容器
# （Render 免费档等）上水位只升不降，最终被 OOMKiller SIGKILL（无优雅
# 关停）。大请求收尾后调用 malloc_trim(0) 让内核回收空闲页。
# 门槛按「正文字节」而非「请求是否大」：Claude Code 第 10 轮起正文就稳定
# 在数百 KB，1MB 门槛下这些请求永远不触发归还，碎片一路累积到 MB 级请求
# 出现才释放——而那时往往已经贴近 OOM 阈值。超大请求（≥8MB）跳过限频：
# 这种请求本身就把 RSS 顶到高水位，紧接着的并发请求没有 2s 可以等。
_MALLOC_TRIM_THRESHOLD_BYTES = 256 * 1024
_MALLOC_TRIM_FORCE_BYTES = 8 * 1024 * 1024
_MALLOC_TRIM_MIN_INTERVAL = 2.0
_malloc_trim_enabled: bool | None = None  # 延迟解析 env（AISTUDIO_MALLOC_TRIM=0 关闭）
_malloc_trim_fn: Callable[[int], int] | None = None
_malloc_trim_unavailable = False
_last_trim_monotonic = 0.0


def _resolve_malloc_trim() -> Callable[[int], int] | None:
    """glibc malloc_trim（解析一次并缓存）；musl/非 glibc/Windows 返回 None。

    Windows 的 ``ctypes.CDLL(None)`` 直接抛 ``TypeError``（LoadLibrary 不接受
    None 句柄），必须一并捕获：否则首个超阈值的请求会在收尾路径炸掉。
    """
    global _malloc_trim_fn, _malloc_trim_unavailable
    if _malloc_trim_unavailable:
        return None
    if _malloc_trim_fn is None:
        try:
            import ctypes

            libc = ctypes.CDLL(None)
            libc.malloc_trim.argtypes = [ctypes.c_size_t]
            libc.malloc_trim.restype = ctypes.c_int
            _malloc_trim_fn = libc.malloc_trim
        except (OSError, AttributeError, ValueError, TypeError):
            _malloc_trim_unavailable = True
            return None
    return _malloc_trim_fn


def _release_allocator_memory(received_bytes: int, sent_bytes: int) -> None:
    """体量超阈值的请求收尾后归还 glibc 堆碎片（throttle 限频）。

    事件循环线程内同步调用（malloc_trim 常态亚毫秒）；持有 arena 锁期间
    其他线程的分配会短暂等待，故仅大请求触发 + 2s 限频。解析失败
    （Alpine musl 无此符号）静默跳过，不影响请求路径。
    """
    global _malloc_trim_enabled, _last_trim_monotonic
    if _malloc_trim_enabled is None:
        _malloc_trim_enabled = os.environ.get(
            "AISTUDIO_MALLOC_TRIM", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
    if not _malloc_trim_enabled:
        return
    if max(received_bytes, sent_bytes) < _MALLOC_TRIM_THRESHOLD_BYTES:
        return
    trim = _resolve_malloc_trim()
    if trim is None:
        return
    now = time.monotonic()
    if (
        max(received_bytes, sent_bytes) < _MALLOC_TRIM_FORCE_BYTES
        and now - _last_trim_monotonic < _MALLOC_TRIM_MIN_INTERVAL
    ):
        return
    _last_trim_monotonic = now
    try:
        trim(0)
    except Exception:  # pragma: no cover - ctypes 调用的防御性兜底
        logger.debug("malloc_trim 调用失败，跳过内存归还", exc_info=True)


def _is_terminal_event(path: str, body: bytes) -> bool:
    """Recognize complete terminal frames emitted by our model routes.

    Our encoders emit each event in one ASGI body message. Inspect protocol
    fields, not model text or tool arguments.
    """
    if path == "/v1/chat/completions":
        # Official Chat clients raise and close on a top-level error without
        # consuming [DONE]. Our encoder places that object first; a nested
        # tool argument or text containing "error" cannot match this prefix.
        return body == b"data: [DONE]\n\n" or body.startswith(b'data: {"error":{')
    if path == "/v1/messages":
        return body.startswith((b"event: message_stop\n", b"event: error\n"))
    if path == "/v1/responses":
        return body.startswith(
            (
                b"event: response.completed\n",
                b"event: response.incomplete\n",
                b"event: response.failed\n",
                b"event: error\n",
            )
        )
    if path.startswith("/v1beta/") and path.endswith(":streamGenerateContent"):
        if body.startswith(b'data: {"error":'):
            return True
        # The ordinary usage/finish frame is compact and has no output parts.
        if body.startswith(
            b'data: {"candidates":[{"content":{"role":"model","parts":[]},"finishReason":'
        ):
            return True
        # Buffered variants may send the terminal metadata with their output.
        # Avoid parsing ordinary content frames; a tool's nested finishReason
        # must not certify that the surrounding stream has finished.
        if body.startswith(b"data: ") and b'"finishReason":' in body:
            try:
                frame = json.loads(body[6:])
            except ValueError:
                return False
            candidates = frame.get("candidates") if isinstance(frame, dict) else None
            return (
                isinstance(candidates, list)
                and bool(candidates)
                and isinstance(candidates[0], dict)
                and bool(candidates[0].get("finishReason"))
            )
    return False


@dataclass
class _ResponseObserver:
    """__call__ 内观测到的请求/响应状态（原 14 个 nonlocal 闭包变量）。

    observed_receive/observed_send 通过实例字段读写，取代 nonlocal 声明；
    elapsed/started 类时间戳不在此列（收尾 finally 现算）。
    """

    status: int = 500
    response_started: bool = False
    completed: bool = False
    sending: bool = False
    json_payload_sent: bool = False
    stream_terminal_sent: bool = False
    delivered_outcome: tuple[str, str, str] | None = None
    client_disconnected: bool = False
    is_stream: bool = False
    first_byte_ms: float | None = None
    received_bytes: int = 0
    # 累计下行字节（SSE 帧逐条累加，不缓冲内容）：大请求收尾的内存
    # 归还判断依据（长流式响应同样是内存高水位的贡献者）。
    sent_bytes: int = 0
    request_body_truncated: bool = False
    request_body_complete: bool = False
    response_body_truncated: bool = False
    metadata_body: bytearray = field(default_factory=bytearray)
    request_body: bytearray = field(default_factory=bytearray)
    request_body_file: BinaryIO | None = None
    response_body: bytearray = field(default_factory=bytearray)
    response_headers: dict[str, str] = field(default_factory=dict)

    @property
    def response_delivered(self) -> bool:
        return self.completed or self.json_payload_sent or self.stream_terminal_sent

    def spool_request_body(self, chunk: bytes) -> None:
        try:
            if self.request_body_file is None:
                # The middleware closes the spool after final persistence.
                self.request_body_file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
                self.request_body_file.write(self.request_body)
            self.request_body_file.write(chunk)
        except OSError:
            self.close_request_body_file()
            logger.warning("无法暂存超出采集上限的客户端正文", exc_info=True)
        finally:
            self.request_body.clear()

    def close_request_body_file(self) -> None:
        if self.request_body_file is not None:
            with contextlib.suppress(OSError):
                self.request_body_file.close()
            self.request_body_file = None


class RequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        original_scope = {**scope, "headers": list(scope["headers"])}
        scope.setdefault("state", {})
        if len(path) > 1 and path.endswith("/"):
            # 规范化优先于 Starlette 的 307 尾斜杠重定向：重定向分支不执行
            # 路由 dependencies，受保护管理端点的尾斜杠变体会未鉴权拿到
            # 307 且 Location 原样回显查询串（含一次性票据值，会进反代日志）
            path = path.rstrip("/") or "/"
            try:
                raw_path = path.encode("latin-1")
            except UnicodeEncodeError:
                # ASGI path 是百分号解码后的字符串，含非 Latin-1 字符（如
                # /v1beta/模型:generateContent/）时直接 encode 会在下方
                # try 块之前抛 UnicodeEncodeError 变成未分类 500；退回百分号
                # 编码重建（raw_path 仅信息性字段，路由按 path 进行）。
                raw_path = urllib.parse.quote(path, safe="/%").encode("ascii")
            scope = {**scope, "path": path, "raw_path": raw_path}
        # /v1/messages/count_tokens 是纯本地 token 估算接口（routes_compat 的
        # claude_count_tokens），不产生上游模型调用，不应占并发名额或进历史统计。
        model_call = (
            scope["method"] == "POST"
            and (
                path.startswith(("/v1beta/", "/v1/messages"))
                or path in ("/v1/chat/completions", "/v1/responses")
            )
            and not path.endswith("/count_tokens")
        )
        responses_call = model_call and path == "/v1/responses"
        if responses_call:
            scope["state"]["responses_accept_event"] = None
        # Management payloads can contain cookies, complete config and credentials.
        capture_client = model_call and is_dump_to_file_enabled()
        dump_enabled = capture_client or (model_call and is_dump_requests_enabled())
        context = RequestContext(
            request_id="req_" + uuid4().hex,
            category="model" if model_call else "system",
            protocol="gemini"
            if path.startswith("/v1beta/")
            else "claude"
            if path.startswith("/v1/messages")
            else "openai"
            if model_call
            else "",
            method=scope["method"],
            path=redact_text(path),
        )
        if model_call and path.startswith("/v1beta/"):
            context.requested_model = path.removeprefix("/v1beta/").split(":", 1)[0]
            context.model = context.requested_model
        observer = _ResponseObserver()
        token = current_request.set(context)
        started_at, started = time.time(), time.perf_counter()
        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope["headers"]}
        admitted = False
        request_timeout = asyncio.timeout(settings.request_timeout)
        history_start: asyncio.Task[None] | None = None
        disconnect_scope = anyio.CancelScope() if model_call else None
        disconnect_watch: asyncio.Task[Message] | None = None

        async def reply(code: int, message: str) -> None:
            from aistudio_api.application.compat_service import (
                claude_error_payload,
                openai_error_payload,
            )

            payload = (
                claude_error_payload(code, message)
                if context.protocol == "claude"
                else openai_error_payload(code, message)
            )
            if context.protocol == "gemini":
                payload = {
                    "error": {
                        "code": code,
                        "message": message,
                        "status": {
                            413: "INVALID_ARGUMENT",
                            429: "RESOURCE_EXHAUSTED",
                            504: "DEADLINE_EXCEEDED",
                        }.get(code, "INTERNAL"),
                    }
                }
            reply_headers = [(b"content-type", b"application/json")]
            # RequestMiddleware 位于 CORS 兼容层之外（send 超时必须约束真实
            # socket 而非 BaseHTTPMiddleware 的内存通道），本函数直接发出的
            # 429/413/504/500 不会经过 compat_cors_middleware；对 /v1/ 白名单
            # Origin 自行补 CORS 头，否则浏览器客户端只能看到被同源策略吞掉
            # 的 opaque 网络错误而非具体失败原因。
            if path.startswith("/v1/"):
                origin = headers.get("origin", "")
                if origin:
                    from aistudio_api.api.app import _allowed_cors_origins

                    if origin in _allowed_cors_origins():
                        reply_headers += [
                            (
                                b"access-control-allow-origin",
                                origin.encode("latin-1"),
                            ),
                            (b"access-control-allow-credentials", b"true"),
                            (b"vary", b"Origin"),
                        ]
            await observed_send(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": reply_headers,
                }
            )
            await observed_send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(payload).encode(),
                }
            )

        def mark_disconnect() -> None:
            if observer.response_delivered or observer.client_disconnected:
                return
            observer.client_disconnected = True
            context.result, context.error = "cancelled", _DOWNSTREAM_DISCONNECT
            if model_call:
                request_event("disconnected", context.error)

        async def watch_disconnect(cancel_scope: anyio.CancelScope) -> Message:
            # Once the last request-body message has been consumed, this is
            # the sole reader of the server's receive channel. In particular,
            # no heartbeat or concurrent Starlette receive call is required.
            message = await receive()
            if message["type"] == "http.disconnect" and not observer.completed:
                mark_disconnect()
                # Release work still running even if its terminal event was
                # already sent; in that case leave the delivered result alone.
                cancel_scope.cancel()
            return message

        async def observed_receive() -> Message:
            nonlocal disconnect_watch
            if disconnect_watch is not None:
                # Starlette cancels its own receive waiters when a response
                # completes. That must not cancel the shared watcher.
                return await asyncio.shield(disconnect_watch)
            message = await receive()
            if message["type"] == "http.disconnect":
                mark_disconnect()
            elif message["type"] == "http.request":
                body = message.get("body", b"")
                observer.received_bytes += len(body)
                observer.request_body_complete = not message.get("more_body", False)
                if model_call and not context.requested_model:
                    if observer.received_bytes <= 65536:
                        observer.metadata_body.extend(body)
                        if not message.get("more_body", False):
                            try:
                                metadata = json.loads(observer.metadata_body)
                                if isinstance(metadata, dict) and isinstance(
                                    metadata.get("model"), str
                                ):
                                    context.requested_model = redact_text(
                                        metadata["model"]
                                    )[:256]
                                    context.model = context.requested_model
                            except (ValueError, UnicodeDecodeError):
                                pass
                    elif observer.metadata_body:
                        # 超过 64KB 阈值：对已收到的前缀（补当前块前 4KB，
                        # 避免 "model" 键名跨块截断）做一次轻量正则嗅探，
                        # 之后丢弃缓冲（大请求体不再累积观测副本）。
                        sample = bytes(observer.metadata_body) + body[:4096]
                        observer.metadata_body.clear()
                        match = _MODEL_SNIFF_RE.search(sample)
                        if match is not None:
                            try:
                                sniffed = match.group(1).decode("utf-8")
                            except UnicodeDecodeError:
                                sniffed = ""
                            if sniffed:
                                context.requested_model = redact_text(sniffed)[:256]
                                context.model = context.requested_model
                # 管理路径同样施加请求体上限（1MB）：写端点无界接收可把
                # 磁盘与 yaml 解析 CPU 打爆（实测 50MB config 被完整落盘）
                body_limit = settings.max_request_bytes if model_call else 1_048_576
                if observer.received_bytes > body_limit:
                    observer.request_body_complete = False
                    observer.request_body_truncated = True
                    observer.request_body.clear()
                    observer.close_request_body_file()
                    raise HTTPException(413, "Request body exceeds configured limit")
                if dump_enabled and (
                    not observer.request_body_truncated
                    or observer.request_body_file is not None
                ):
                    if (
                        not observer.request_body_truncated
                        and observer.received_bytes <= settings.dump_max_bytes
                    ):
                        observer.request_body.extend(body)
                    else:
                        observer.request_body_truncated = True
                        if capture_client:
                            # Keep the original capture complete without holding
                            # a second large copy of the request in memory.
                            write = asyncio.create_task(
                                asyncio.to_thread(observer.spool_request_body, body)
                            )
                            try:
                                await asyncio.shield(write)
                            except asyncio.CancelledError:
                                with anyio.CancelScope(shield=True):
                                    await write
                                raise
                        else:
                            observer.request_body.clear()
                if observer.request_body_complete and disconnect_scope is not None:
                    disconnect_watch = asyncio.create_task(
                        watch_disconnect(disconnect_scope)
                    )
            return message

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                observer.response_started = True
                observer.status = message["status"]
                context.status_code = observer.status
                raw_headers = list(message.get("headers", []))
                raw_headers = [
                    (k, v) for k, v in raw_headers if k.lower() != b"x-request-id"
                ]
                raw_headers.append((b"x-request-id", context.request_id.encode()))
                message = {**message, "headers": raw_headers}
                observer.response_headers = {
                    k.decode("latin1"): v.decode("latin1") for k, v in raw_headers
                }
                observer.is_stream = "text/event-stream" in (
                    observer.response_headers.get("content-type", "")
                )
                context.is_stream = observer.is_stream
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                observer.sent_bytes += len(body)
                if body and observer.first_byte_ms is None:
                    observer.first_byte_ms = (time.perf_counter() - started) * 1000
                    context.first_byte_ms = observer.first_byte_ms
                    if model_call:
                        request_event(
                            "streaming" if observer.is_stream else "response",
                            "首个响应数据已发送",
                        )
                if (
                    dump_enabled
                    and not observer.is_stream
                    and not observer.response_body_truncated
                ):
                    if (
                        len(observer.response_body) + len(body)
                        <= settings.dump_max_bytes
                    ):
                        observer.response_body.extend(body)
                    else:
                        observer.response_body.clear()
                        observer.response_body_truncated = True
            observer.sending = True
            if model_call:
                try:
                    # Bound the actual socket send outside BaseHTTPMiddleware's
                    # memory channels, including the final timeout error body.
                    async with asyncio.timeout(settings.stream_send_timeout):
                        await send(message)
                except TimeoutError as exc:
                    detail = "Downstream response send timed out"
                    if not observer.response_delivered:
                        context.result, context.error = "cancelled", detail
                    raise OSError(detail) from exc
            else:
                await send(message)
            observer.sending = False
            if message["type"] == "http.response.body":
                if not message.get("more_body", False):
                    observer.completed = True
                if observer.is_stream:
                    body = message.get("body", b"")
                    if _is_terminal_event(path, body):
                        observer.stream_terminal_sent = True
                    if responses_call and body.startswith(b"event: response."):
                        # Internal ASGI channels may run ahead. Commit protocol
                        # progress only after the outer transport accepted it.
                        accepted = scope["state"].get("responses_accept_event")
                        if callable(accepted):
                            accepted(body)
                if (
                    model_call
                    and not observer.is_stream
                    and message.get("body", b"")
                    and (
                        # A buffered keepalive writes its complete JSON after
                        # zero or more whitespace chunks, with no fixed size.
                        (
                            scope.get("state", {}).get("json_keepalive_failure")
                            and message["body"].strip()
                        )
                        # Ordinary JSON may reach its full declared size before
                        # middleware sends the empty HTTP closing message.
                        or (
                            observer.response_headers.get(
                                "content-type", ""
                            ).startswith("application/json")
                            and observer.response_headers.get("content-length")
                            == str(observer.sent_bytes)
                        )
                    )
                ):
                    observer.json_payload_sent = True
                if observer.response_delivered and observer.delivered_outcome is None:
                    observer.delivered_outcome = (
                        context.result,
                        context.error,
                        context.finish_reason,
                    )

        try:
            # Reserve a slot before the first await, including history I/O.
            if model_call:
                if self.active >= settings.max_concurrent_requests:
                    context.error = "Concurrent request limit reached"
                    await reply(429, context.error)
                    return
                self.active += 1
                admitted = True
            if model_call:
                async with request_timeout:
                    register_request(context)
                    request_event("received", "收到模型调用")
                    logger.info(
                        "模型请求开始: protocol=%s model=%s",
                        context.protocol,
                        context.model or "待解析",
                    )
                    if settings.request_history:
                        # fire-and-forget：准入路径不再等待 SQLite 起始记录
                        # 落盘（每次 insert 新开连接 + fsync，高并发下排队会
                        # 直接推迟所有请求的 TTFB）。顺序由收尾 _persist 先
                        # await history_start 保证（慢 worker 不会复活
                        # 'running'），失败在收尾统一记日志。
                        history_start = asyncio.create_task(
                            asyncio.to_thread(
                                request_history().insert,
                                request_snapshot(context, running=True),
                            )
                        )
                    raw_length = headers.get("content-length", "0")
                    if (
                        not raw_length.isdigit()
                        or int(raw_length) > settings.max_request_bytes
                    ):
                        await reply(413, "Request body exceeds configured limit")
                        return
                    assert disconnect_scope is not None
                    with disconnect_scope:
                        await self.app(scope, observed_receive, observed_send)
            else:
                await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            if not observer.response_delivered:
                context.result = "cancelled"
            raise
        except TimeoutError:
            if observer.client_disconnected:
                return
            if observer.response_delivered:
                if observer.sending:
                    # The earlier terminal/JSON send succeeded. The trailing
                    # write may have partially completed: don't try it again.
                    raise OSError(
                        "Timeout interrupted trailing downstream send"
                    ) from None
                if not observer.completed:
                    await observed_send({"type": "http.response.body", "body": b""})
                return
            if not request_timeout.expired():
                context.result, context.error = "error", "Upstream operation timed out"
                logger.exception("Request raised an upstream timeout")
                if not observer.response_started:
                    await reply(500, "Internal server error")
                else:
                    raise
                return
            if observer.sending:
                # The transport may have delivered part of this frame already.
                # Never append an error to an interrupted JSON/SSE write.
                context.result = "cancelled"
                context.error = "Request timeout interrupted downstream send"
                raise OSError(context.error) from None
            context.result, context.error = "timeout", "Request exceeded total timeout"
            if not observer.response_started:
                await reply(504, context.error)
            else:
                json_failure = scope.get("state", {}).get("json_keepalive_failure")
                if not observer.is_stream and callable(json_failure):
                    body = cast(Callable[[str], bytes], json_failure)(context.error)
                    await observed_send({"type": "http.response.body", "body": body})
                    return
                failure = scope.get("state", {}).get("stream_failure")
                if observer.is_stream and callable(failure):
                    chunks = cast(Callable[[str], list[str]], failure)(context.error)
                    # An idempotent error handler may have nothing left to emit.
                    # Still send the final HTTP body so clients do not hang.
                    if not chunks:
                        chunks = [""]
                    for index, chunk in enumerate(chunks):
                        await observed_send(
                            {
                                "type": "http.response.body",
                                "body": chunk.encode("utf-8"),
                                "more_body": index < len(chunks) - 1,
                            }
                        )
                    return
                await observed_send(
                    {
                        "type": "http.response.body",
                        "body": b'data: {"error":{"code":504,"message":"Request exceeded total timeout"}}\n\n'
                        if observer.is_stream
                        else b"",
                    }
                )
        except (OSError, ClientDisconnect):
            if not observer.response_delivered:
                context.result = "cancelled"
                if not context.error:
                    context.error = _DOWNSTREAM_DISCONNECT
            if not observer.client_disconnected:
                raise
        except Exception:
            if not observer.response_delivered:
                context.result, context.error = "error", "Internal server error"
            logger.exception("Request failed")
            if observer.client_disconnected:
                return
            if not observer.response_started:
                await reply(500, context.error)
            else:
                raise
        finally:
            if admitted:
                self.active -= 1
            try:
                if disconnect_watch is not None:
                    disconnect_watch.cancel()
                    with anyio.CancelScope(shield=True):
                        await asyncio.gather(disconnect_watch, return_exceptions=True)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if observer.client_disconnected:
                    # Account/quota bookkeeping in generator teardown may
                    # record that upstream handled the call. It cannot turn
                    # an undelivered response into client-visible success.
                    context.result, context.error = "cancelled", _DOWNSTREAM_DISCONNECT
                    context.finish_reason = ""
                elif context.result == "cancelled" and observer.delivered_outcome:
                    # An inner response writer may also report its own send
                    # timeout. That cannot revoke an earlier successful send.
                    context.result, context.error, context.finish_reason = (
                        observer.delivered_outcome
                    )
                if not context.result:
                    context.result = (
                        "rate_limited"
                        if observer.status == 429
                        else "success"
                        if observer.response_delivered and observer.status < 400
                        else "error"
                    )
                if not observer.response_delivered:
                    if context.result == "success":
                        context.result = "cancelled"
                    if context.result == "cancelled":
                        context.finish_reason = ""
                if model_call and admitted:
                    from aistudio_api.api.state import runtime_state

                    service = runtime_state.account_service
                    account = (
                        service.get_account(context.account_id)
                        if service and context.account_id
                        else None
                    )
                    if account:
                        context.account_name = (
                            account.email or account.name or account.id
                        )
                    request_event(
                        "finished",
                        f"调用结束 · {context.result}"
                        + (f" · {context.error}" if context.error else ""),
                    )
                    record = {
                        **request_snapshot(context),
                        "started_at": started_at,
                        "method": scope["method"],
                        "path": redact_text(path),
                        "status_code": observer.status,
                        "is_stream": observer.is_stream,
                        "elapsed_ms": round(elapsed_ms, 2),
                        "finished_at": time.time(),
                        "generation_ms": round(
                            max(0, elapsed_ms - context.first_content_ms), 2
                        )
                        if context.first_content_ms is not None
                        else None,
                        "first_byte_ms": observer.first_byte_ms,
                    }
                    # Drain bookkeeping, then preserve cancellation semantics.
                    task = asyncio.create_task(
                        self._persist(
                            record,
                            context,
                            observer,
                            headers,
                            path,
                            elapsed_ms,
                            history_start,
                            original_scope,
                            dump_enabled,
                        )
                    )
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
            finally:
                observer.close_request_body_file()
                if model_call and admitted:
                    unregister_request(context.request_id)
                if responses_call:
                    # After the final send and cleanup, an idle HTTP connection
                    # can still hold this scope. Don't let its callbacks pin the
                    # Responses emitter and its accumulated output.
                    scope["state"].pop("stream_failure", None)
                    scope["state"].pop("responses_accept_event", None)
                if model_call and admitted:
                    # 大请求收尾后归还 glibc 堆碎片（小内存容器 RSS 高水位
                    # 治理，详见 _release_allocator_memory）；放行务落账之后。
                    _release_allocator_memory(
                        observer.received_bytes, observer.sent_bytes
                    )
                current_request.reset(token)

    @staticmethod
    async def _persist(
        record: dict[str, object],
        context: RequestContext,
        observer: _ResponseObserver,
        headers: dict[str, str],
        path: str,
        elapsed_ms: float,
        history_start: asyncio.Task[None] | None,
        original_scope: Scope,
        dump_enabled: bool,
    ) -> None:
        # A cancelled to_thread await does not stop its worker. Order the final
        # record after the initial insert so a slow worker cannot resurrect 'running'.
        if history_start is not None:
            try:
                await history_start
            except Exception:
                logger.exception("Failed to persist request start")
        if settings.request_history:
            try:

                def _insert_masked() -> None:
                    safe_record = _mask_json_value(record)
                    if isinstance(safe_record, dict):
                        request_history().insert(safe_record)

                # 递归脱敏对整棵请求树逐字符串跑正则（events/attempts 满额时
                # 毫秒级纯 CPU），与 SQLite 写入同在线程池执行，不再阻塞
                # 事件循环。
                await asyncio.to_thread(_insert_masked)
            except Exception:
                logger.exception("Failed to persist request history")
        if dump_enabled:
            try:
                client_request: dict[str, object] | None = None
                client_body_file = None
                if is_dump_to_file_enabled():
                    # Keep the ASGI body verbatim, before validation, defaults,
                    # model mapping or protocol conversion. Only transport
                    # credentials are masked; tool schemas/prompts are data.
                    encoding = "utf-8"
                    try:
                        body_text = observer.request_body.decode("utf-8")
                    except UnicodeDecodeError:
                        body_text = base64.b64encode(observer.request_body).decode(
                            "ascii"
                        )
                        encoding = "base64"
                    raw_path = original_scope.get("raw_path")
                    target = (
                        raw_path.decode("latin1")
                        if raw_path is not None
                        else original_scope["path"]
                    )
                    query = original_scope.get("query_string", b"").decode("latin1")
                    client_body_file = observer.request_body_file
                    client_body_truncated = (
                        observer.request_body_truncated and client_body_file is None
                    )
                    client_request = {
                        "source": "client",
                        "method": original_scope["method"],
                        "target": _mask_url(target + (f"?{query}" if query else "")),
                        "http_version": original_scope.get("http_version", ""),
                        "headers": [
                            [
                                key.decode("latin1"),
                                _mask_sensitive_value(
                                    key.decode("latin1"), value.decode("latin1")
                                ),
                            ]
                            for key, value in original_scope["headers"]
                        ],
                        "body": (
                            None if observer.request_body_truncated else body_text
                        ),
                        "body_encoding": encoding,
                        "body_bytes": observer.received_bytes,
                        "complete": observer.request_body_complete
                        and not client_body_truncated,
                        "truncated": client_body_truncated,
                    }
                await asyncio.to_thread(
                    dump_request_exchange,
                    req_id=context.request_id,
                    method="POST",
                    url=path,
                    client="",
                    headers=headers,
                    query_params={},
                    body_text=observer.request_body.decode("utf-8", errors="replace"),
                    request_body_truncated=observer.request_body_truncated,
                    status_code=observer.status,
                    elapsed_ms=elapsed_ms,
                    response_headers=observer.response_headers,
                    response_text=observer.response_body.decode(
                        "utf-8", errors="replace"
                    ),
                    response_body_truncated=observer.response_body_truncated,
                    is_stream=observer.is_stream,
                    stream_complete=observer.completed,
                    model=context.model,
                    account_id=context.account_id,
                    auth_user=context.auth_user,
                    token_usage=context.usage,
                    finish_reason=context.finish_reason,
                    error_detail=context.error,
                    console=is_dump_requests_enabled(),
                    client_request=client_request,
                    client_body_file=client_body_file,
                )
            except Exception:
                logger.exception("Failed to persist request dump")
        logger.info(
            "%s %s -> %d model=%s account=%s result=%s duration=%.1fms attempts=%d request_id=%s",
            record["method"],
            path,
            observer.status,
            context.model,
            context.account_name or context.account_id,
            context.result,
            elapsed_ms,
            len(context.attempts),
            context.request_id,
        )
        details = context.usage.get("completion_tokens_details")
        logger.info(
            "调用计量: 首包=%sms 首段回复=%sms 总耗时=%.1fms Token(输入/输出/思考/缓存)=%s/%s/%s/%s",
            record.get("first_byte_ms"),
            context.first_content_ms,
            elapsed_ms,
            context.usage.get("prompt_tokens", "未提供"),
            context.usage.get("completion_tokens", "未提供"),
            details.get("reasoning_tokens", "未提供")
            if isinstance(details, dict)
            else "未提供",
            context.usage.get("cached_tokens", "未提供"),
        )
