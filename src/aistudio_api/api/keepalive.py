"""HTTP-boundary keepalive for quiet model generations and buffered JSON."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Message, Send

from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import record_result

STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}
_PreparationError = Callable[[int, str], list[str]]


class HeartbeatStreamingResponse(StreamingResponse):
    """Send comments while the iterator waits, without cancelling its next read.

    Keep source iteration in one task (ContextVar tokens may span yields).
    Serialize sends and retain backpressure; never queue generated content.
    """

    heartbeat = b": keepalive\n\n"

    async def stream_response(self, send: Send) -> None:
        lock = anyio.Lock()

        async def bounded_send(message: Message) -> None:
            try:
                with anyio.fail_after(settings.stream_send_timeout):
                    await send(message)
            except TimeoutError as exc:
                record_result("cancelled", error="Downstream response send timed out")
                raise OSError("Downstream response send timed out") from exc

        async def heartbeat() -> None:
            while True:
                await anyio.sleep(settings.stream_heartbeat_interval)
                async with lock:
                    await bounded_send(
                        {
                            "type": "http.response.body",
                            "body": self.heartbeat,
                            "more_body": True,
                        }
                    )

        try:
            await bounded_send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            await bounded_send(
                {
                    "type": "http.response.body",
                    "body": self.heartbeat,
                    "more_body": True,
                }
            )
            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(heartbeat)
                    try:
                        async for chunk in self.body_iterator:
                            body = (
                                chunk
                                if isinstance(chunk, (bytes, memoryview))
                                else chunk.encode(self.charset)
                            )
                            async with lock:
                                await bounded_send(
                                    {
                                        "type": "http.response.body",
                                        "body": body,
                                        "more_body": True,
                                    }
                                )
                                # The next read can be idle indefinitely. Don't
                                # retain either form of an already accepted frame.
                                del chunk, body
                    finally:
                        group.cancel_scope.cancel()
            except BaseExceptionGroup as exc:
                # Preserve Starlette's OSError -> ClientDisconnect handling on
                # ASGI 2.4. A heartbeat task must not hide a socket error in a group.
                if len(exc.exceptions) == 1:
                    raise exc.exceptions[0] from None
                raise
            await bounded_send(
                {"type": "http.response.body", "body": b"", "more_body": False}
            )
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                # Disconnect cancellation must not interrupt resource cleanup.
                with anyio.move_on_after(5, shield=True):
                    await close()


async def _cancel_generation(task: asyncio.Task[Response]) -> None:
    if not task.done():
        task.cancel()
    with (
        anyio.move_on_after(5, shield=True),
        contextlib.suppress(asyncio.CancelledError, Exception),
    ):
        await task


class _DeferredResponse(HeartbeatStreamingResponse):
    """Own preparation until a response exists, then forward its body directly."""

    def __init__(
        self,
        task: asyncio.Task[Response],
        request: Request,
        *,
        stream: bool,
        preparation_error: _PreparationError | None,
    ) -> None:
        self.generation = task
        self.heartbeat = b": keepalive\n\n" if stream else b" \n"

        async def body():
            response = None
            try:
                try:
                    # Only stream_response's finalizer cancels and joins generation.
                    # Body cancellation must not interrupt its HTTP cleanup first.
                    response = await asyncio.shield(task)
                except Exception as exc:
                    if stream:
                        for chunk in _preparation_stream_error(
                            request, exc, preparation_error
                        ):
                            yield chunk
                    else:
                        yield _json_error(request, exc).body
                    return
                if isinstance(response, StreamingResponse):
                    async for chunk in response.body_iterator:
                        yield chunk
                        # The sender has resumed us; release our forwarding
                        # reference before waiting for the next source item.
                        del chunk
                else:
                    yield response.body
            finally:
                if isinstance(response, StreamingResponse):
                    close = getattr(response.body_iterator, "aclose", None)
                    if close is not None:
                        with anyio.move_on_after(5, shield=True):
                            await close()

        super().__init__(
            body(),
            media_type="text/event-stream" if stream else "application/json",
            headers=STREAM_HEADERS,
        )

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # Also covers disconnects before the body iterator was first entered.
            await _cancel_generation(self.generation)


def _record_error_details(exc: Exception) -> tuple[int, str, str]:
    # Lazy import: the Gemini service also uses HeartbeatStreamingResponse.
    from aistudio_api.application.api_service_gemini import (
        classify_gemini_error_payload,
    )

    code, message, status = classify_gemini_error_payload(exc)
    record_result(
        "timeout" if code == 504 else "rate_limited" if code == 429 else "error",
        error=message,
    )
    return code, message, status


def _json_error(request: Request, exc: Exception) -> JSONResponse:
    from aistudio_api.application.compat_service import build_compat_error_response

    code, message, status = _record_error_details(exc)
    return build_compat_error_response(request, exc) or JSONResponse(
        {"error": {"code": code, "message": message, "status": status}},
        status_code=code,
    )


def _preparation_stream_error(
    request: Request,
    exc: Exception,
    preparation_error: _PreparationError | None,
) -> list[str]:
    """Errors before protocol adapters start (and before any completion IDs exist)."""
    if preparation_error is not None:
        code, message, _ = _record_error_details(exc)
        return preparation_error(code, message)
    response = _json_error(request, exc)
    payload = bytes(response.body).decode("utf-8")
    if request.url.path == "/v1/messages":
        return [f"event: error\ndata: {payload}\n\n"]
    return [f"data: {payload}\n\n", "data: [DONE]\n\n"]


async def keepalive_response(
    generate: Callable[[], Awaitable[Response]],
    request: Request,
    *,
    stream: bool = False,
    preparation_error: _PreparationError | None = None,
) -> Response:
    """Preserve fast statuses; cover slow preparation with protocol keepalives.

    After the first whitespace, HTTP 200 is committed. Late errors use the
    protocol's JSON error body. SSE preparation errors use the route's terminal
    protocol until its normal adapter registers a completion-aware handler.
    """
    if not stream and not settings.nonstream_keepalive:
        return await generate()

    async def run() -> Response:
        return await generate()

    task = asyncio.create_task(run())

    try:
        done, _ = await asyncio.wait({task}, timeout=settings.stream_heartbeat_interval)
        if done:
            return await task
    except BaseException:
        await _cancel_generation(task)
        raise

    def timeout_body(message: str) -> bytes | memoryview:
        return _json_error(request, HTTPException(504, detail=message)).body

    if stream:
        request.state.stream_failure = lambda message: _preparation_stream_error(
            request, HTTPException(504, detail=message), preparation_error
        )
    else:
        request.state.json_keepalive_failure = timeout_body

    return _DeferredResponse(
        task, request, stream=stream, preparation_error=preparation_error
    )
