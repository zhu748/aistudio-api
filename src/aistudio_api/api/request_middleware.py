"""Observe complete ASGI response lifetimes without buffering streaming output."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import cast
from uuid import uuid4

from fastapi import HTTPException
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
    dump_request_exchange,
    get_logger,
    is_dump_requests_enabled,
    is_dump_to_file_enabled,
)
from aistudio_api.infrastructure.utils.redaction import redact_text

logger = get_logger("requests")


class RequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        scope.setdefault("state", {})
        if len(path) > 1 and path.endswith("/"):
            # 规范化优先于 Starlette 的 307 尾斜杠重定向：重定向分支不执行
            # 路由 dependencies，受保护管理端点的尾斜杠变体会未鉴权拿到
            # 307 且 Location 原样回显查询串（含一次性票据值，会进反代日志）
            path = path.rstrip("/") or "/"
            scope = {**scope, "path": path, "raw_path": path.encode("latin-1")}
        model_call = scope["method"] == "POST" and (
            path.startswith("/v1beta/")
            or path in ("/v1/chat/completions", "/v1/messages", "/v1/responses")
        )
        # Management payloads can contain cookies, complete config and credentials.
        dump_enabled = model_call and (
            is_dump_requests_enabled() or is_dump_to_file_enabled()
        )
        context = RequestContext(
            request_id="req_" + uuid4().hex,
            category="model" if model_call else "system",
            protocol="gemini"
            if path.startswith("/v1beta/")
            else "claude"
            if path == "/v1/messages"
            else "openai"
            if model_call
            else "",
            method=scope["method"],
            path=redact_text(path),
        )
        if model_call and path.startswith("/v1beta/"):
            context.requested_model = path.removeprefix("/v1beta/").split(":", 1)[0]
            context.model = context.requested_model
        token = current_request.set(context)
        started_at, started = time.time(), time.perf_counter()
        status, response_started, completed = 500, False, False
        is_stream = False
        first_byte_ms: float | None = None
        received_bytes = 0
        metadata_body = bytearray()
        request_body = bytearray()
        response_body = bytearray()
        request_body_truncated = False
        response_body_truncated = False
        response_headers: dict[str, str] = {}
        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope["headers"]}
        admitted = False
        request_timeout = asyncio.timeout(settings.request_timeout)
        history_start: asyncio.Task[None] | None = None

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
            await observed_send(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await observed_send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(payload).encode(),
                }
            )

        async def observed_receive() -> Message:
            nonlocal received_bytes, request_body_truncated
            message = await receive()
            if message["type"] == "http.disconnect" and not completed:
                context.result = "cancelled"
            elif message["type"] == "http.request":
                body = message.get("body", b"")
                received_bytes += len(body)
                if (
                    model_call
                    and not context.requested_model
                    and received_bytes <= 65536
                ):
                    metadata_body.extend(body)
                    if not message.get("more_body", False):
                        try:
                            metadata = json.loads(metadata_body)
                            if isinstance(metadata, dict) and isinstance(
                                metadata.get("model"), str
                            ):
                                context.requested_model = redact_text(
                                    metadata["model"]
                                )[:256]
                                context.model = context.requested_model
                        except (ValueError, UnicodeDecodeError):
                            pass
                # 管理路径同样施加请求体上限（1MB）：写端点无界接收可把
                # 磁盘与 yaml 解析 CPU 打爆（实测 50MB config 被完整落盘）
                body_limit = settings.max_request_bytes if model_call else 1_048_576
                if received_bytes > body_limit:
                    raise HTTPException(413, "Request body exceeds configured limit")
                if dump_enabled and not request_body_truncated:
                    if len(request_body) + len(body) <= settings.dump_max_bytes:
                        request_body.extend(body)
                    else:
                        request_body.clear()
                        request_body_truncated = True
            return message

        async def observed_send(message: Message) -> None:
            nonlocal \
                status, \
                response_started, \
                completed, \
                is_stream, \
                first_byte_ms, \
                response_headers, \
                response_body_truncated
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                context.status_code = status
                raw_headers = list(message.get("headers", []))
                raw_headers = [
                    (k, v) for k, v in raw_headers if k.lower() != b"x-request-id"
                ]
                raw_headers.append((b"x-request-id", context.request_id.encode()))
                message = {**message, "headers": raw_headers}
                response_headers = {
                    k.decode("latin1"): v.decode("latin1") for k, v in raw_headers
                }
                is_stream = "text/event-stream" in response_headers.get(
                    "content-type", ""
                )
                context.is_stream = is_stream
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body and first_byte_ms is None:
                    first_byte_ms = (time.perf_counter() - started) * 1000
                    context.first_byte_ms = first_byte_ms
                    if model_call:
                        request_event(
                            "streaming" if is_stream else "response",
                            "首个响应数据已发送",
                        )
                if dump_enabled and not is_stream and not response_body_truncated:
                    if len(response_body) + len(body) <= settings.dump_max_bytes:
                        response_body.extend(body)
                    else:
                        response_body.clear()
                        response_body_truncated = True
                if not message.get("more_body", False):
                    completed = True
            await send(message)

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
                        try:
                            history_start = asyncio.create_task(
                                asyncio.to_thread(
                                    request_history().insert,
                                    request_snapshot(context, running=True),
                                )
                            )
                            await asyncio.shield(history_start)
                        except Exception:
                            logger.exception("Failed to persist request start")
                    raw_length = headers.get("content-length", "0")
                    if (
                        not raw_length.isdigit()
                        or int(raw_length) > settings.max_request_bytes
                    ):
                        await reply(413, "Request body exceeds configured limit")
                        return
                    await self.app(scope, observed_receive, observed_send)
            else:
                await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            context.result = "cancelled"
            raise
        except TimeoutError:
            if not request_timeout.expired():
                context.result, context.error = "error", "Upstream operation timed out"
                logger.exception("Request raised an upstream timeout")
                if not response_started:
                    await reply(500, "Internal server error")
                else:
                    raise
                return
            context.result, context.error = "timeout", "Request exceeded total timeout"
            if not response_started:
                await reply(504, context.error)
            else:
                failure = scope.get("state", {}).get("stream_failure")
                if is_stream and callable(failure):
                    chunks = cast(Callable[[str], list[str]], failure)(context.error)
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
                        if is_stream
                        else b"",
                    }
                )
        except OSError:
            context.result = "cancelled"
            raise
        except Exception:
            context.result, context.error = "error", "Internal server error"
            logger.exception("Request failed")
            if not response_started:
                await reply(500, context.error)
            else:
                raise
        finally:
            if admitted:
                self.active -= 1
            elapsed_ms = (time.perf_counter() - started) * 1000
            if not context.result:
                context.result = (
                    "rate_limited"
                    if status == 429
                    else "success"
                    if completed and status < 400
                    else "error"
                )
            if not completed and context.result == "success":
                context.result = "cancelled"
            if model_call and admitted:
                from aistudio_api.api.state import runtime_state

                service = runtime_state.account_service
                account = (
                    service.get_account(context.account_id)
                    if service and context.account_id
                    else None
                )
                if account:
                    context.account_name = account.email or account.name or account.id
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
                    "status_code": status,
                    "is_stream": is_stream,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "finished_at": time.time(),
                    "generation_ms": round(
                        max(0, elapsed_ms - context.first_content_ms), 2
                    )
                    if context.first_content_ms is not None
                    else None,
                    "first_byte_ms": first_byte_ms,
                }
                # Cancellation must not interrupt final bookkeeping after a client disconnect.
                task = asyncio.create_task(
                    self._persist(
                        record,
                        dump_enabled,
                        context,
                        headers,
                        request_body,
                        response_headers,
                        response_body,
                        request_body_truncated,
                        response_body_truncated,
                        is_stream,
                        completed,
                        status,
                        elapsed_ms,
                        path,
                        history_start,
                    )
                )
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                finally:
                    unregister_request(context.request_id)
            current_request.reset(token)

    @staticmethod
    async def _persist(
        record: dict[str, object],
        dump_enabled: bool,
        context: RequestContext,
        headers: dict[str, str],
        request_body: bytearray,
        response_headers: dict[str, str],
        response_body: bytearray,
        request_body_truncated: bool,
        response_body_truncated: bool,
        is_stream: bool,
        completed: bool,
        status: int,
        elapsed_ms: float,
        path: str,
        history_start: asyncio.Task[None] | None = None,
    ) -> None:
        # A cancelled to_thread await does not stop its worker. Order the final
        # record after the initial insert so a slow worker cannot resurrect 'running'.
        if history_start is not None:
            with contextlib.suppress(Exception):
                await history_start
        if settings.request_history:
            try:
                safe_record = _mask_json_value(record)
                if isinstance(safe_record, dict):
                    await asyncio.to_thread(request_history().insert, safe_record)
            except Exception:
                logger.exception("Failed to persist request history")
        if dump_enabled:
            try:
                await asyncio.to_thread(
                    dump_request_exchange,
                    req_id=context.request_id,
                    method="POST",
                    url=path,
                    client="",
                    headers=headers,
                    query_params={},
                    body_text=request_body.decode("utf-8", errors="replace"),
                    request_body_truncated=request_body_truncated,
                    status_code=status,
                    elapsed_ms=elapsed_ms,
                    response_headers=response_headers,
                    response_text=response_body.decode("utf-8", errors="replace"),
                    response_body_truncated=response_body_truncated,
                    is_stream=is_stream,
                    stream_complete=completed,
                    model=context.model,
                    account_id=context.account_id,
                    auth_user=context.auth_user,
                    token_usage=context.usage,
                    finish_reason=context.finish_reason,
                    error_detail=context.error,
                    console=is_dump_requests_enabled(),
                )
            except Exception:
                logger.exception("Failed to persist request dump")
        logger.info(
            "%s %s -> %d model=%s account=%s result=%s duration=%.1fms attempts=%d request_id=%s",
            record["method"],
            path,
            status,
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
