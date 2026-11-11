"""Observe complete ASGI response lifetimes without buffering streaming output."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from uuid import uuid4

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import RequestContext, current_request
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
        model_call = scope["method"] == "POST" and (
            path.startswith("/v1beta/")
            or path in ("/v1/chat/completions", "/v1/messages")
        )
        # Management payloads can contain cookies, complete config and credentials.
        dump_enabled = model_call and (
            is_dump_requests_enabled() or is_dump_to_file_enabled()
        )
        context = RequestContext(request_id="req_" + uuid4().hex)
        token = current_request.set(context)
        started_at, started = time.time(), time.perf_counter()
        status, response_started, completed = 500, False, False
        is_stream = False
        first_byte_ms: float | None = None
        received_bytes = 0
        request_body = bytearray()
        response_body = bytearray()
        request_body_truncated = False
        response_body_truncated = False
        response_headers: dict[str, str] = {}
        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope["headers"]}
        admitted = False
        request_timeout = asyncio.timeout(settings.request_timeout)

        async def reply(code: int, message: str) -> None:
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
                    "body": json.dumps(
                        {"error": {"message": message, "code": code}}
                    ).encode(),
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
                if model_call and received_bytes > settings.max_request_bytes:
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
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body and first_byte_ms is None:
                    first_byte_ms = (time.perf_counter() - started) * 1000
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
            if model_call and self.active >= settings.max_concurrent_requests:
                context.error = "Concurrent request limit reached"
                await reply(429, context.error)
                return
            if model_call:
                self.active += 1
                admitted = True
                raw_length = headers.get("content-length", "0")
                if (
                    not raw_length.isdigit()
                    or int(raw_length) > settings.max_request_bytes
                ):
                    await reply(413, "Request body exceeds configured limit")
                    return
                async with request_timeout:
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
            if model_call:
                record = {
                    **asdict(context),
                    "started_at": started_at,
                    "method": scope["method"],
                    "path": redact_text(path),
                    "status_code": status,
                    "is_stream": is_stream,
                    "elapsed_ms": round(elapsed_ms, 2),
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
                    )
                )
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
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
    ) -> None:
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
            "%s %s -> %d result=%s duration=%.1fms request_id=%s",
            record["method"],
            path,
            status,
            context.result,
            elapsed_ms,
            context.request_id,
        )
