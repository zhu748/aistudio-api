"""Task 1: request_middleware.__call__ 拆分（_ResponseObserver + _persist 参数收敛）。

锚点精确匹配替换；失败时复位重试。用法：
    uv run python /home/z/my-project/aistudio-api/scripts/split_middleware.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path("/home/z/my-project/aistudio-api/src/aistudio_api/api/request_middleware.py")

text = SRC.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Anchor 1: dataclass import
# ---------------------------------------------------------------------------
A1_OLD = "from collections.abc import Callable\n"
A1_NEW = (
    "from collections.abc import Callable\n"
    "from dataclasses import dataclass, field\n"
)
assert text.count(A1_OLD) == 1, "anchor1 import"
text = text.replace(A1_OLD, A1_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 2: insert _ResponseObserver between _is_terminal_event and RequestMiddleware
# ---------------------------------------------------------------------------
A2_OLD = '''class RequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.active = 0
'''
A2_NEW = '''@dataclass
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
    is_stream: bool = False
    first_byte_ms: float | None = None
    received_bytes: int = 0
    request_body_truncated: bool = False
    request_body_complete: bool = False
    response_body_truncated: bool = False
    metadata_body: bytearray = field(default_factory=bytearray)
    request_body: bytearray = field(default_factory=bytearray)
    response_body: bytearray = field(default_factory=bytearray)
    response_headers: dict[str, str] = field(default_factory=dict)


class RequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.active = 0
'''
assert text.count(A2_OLD) == 1, "anchor2 class head"
text = text.replace(A2_OLD, A2_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 3: state init block in __call__
# ---------------------------------------------------------------------------
A3_OLD = '''        token = current_request.set(context)
        started_at, started = time.time(), time.perf_counter()
        status, response_started, completed = 500, False, False
        sending = False
        json_payload_sent = False
        stream_terminal_sent = False
        is_stream = False
        first_byte_ms: float | None = None
        received_bytes = 0
        metadata_body = bytearray()
        request_body = bytearray()
        response_body = bytearray()
        request_body_truncated = False
        request_body_complete = False
        response_body_truncated = False
        response_headers: dict[str, str] = {}
        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope["headers"]}
        admitted = False
        request_timeout = asyncio.timeout(settings.request_timeout)
        history_start: asyncio.Task[None] | None = None
'''
A3_NEW = '''        observer = _ResponseObserver()
        token = current_request.set(context)
        started_at, started = time.time(), time.perf_counter()
        headers = {k.decode("latin1"): v.decode("latin1") for k, v in scope["headers"]}
        admitted = False
        request_timeout = asyncio.timeout(settings.request_timeout)
        history_start: asyncio.Task[None] | None = None
'''
assert text.count(A3_OLD) == 1, "anchor3 state init"
text = text.replace(A3_OLD, A3_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 4: reply() body — response_headers is now observer field
# ---------------------------------------------------------------------------
A4_OLD = '''            response_headers = [(b"content-type", b"application/json")]
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
                        response_headers += [
'''
A4_NEW = '''            reply_headers = [(b"content-type", b"application/json")]
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
'''
assert text.count(A4_OLD) == 1, "anchor4 reply headers"
text = text.replace(A4_OLD, A4_NEW, 1)

A4B_OLD = '''            await observed_send(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": response_headers,
                }
            )
'''
A4B_NEW = '''            await observed_send(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": reply_headers,
                }
            )
'''
assert text.count(A4B_OLD) == 1, "anchor4b reply send"
text = text.replace(A4B_OLD, A4B_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 5: observed_receive rewrite
# ---------------------------------------------------------------------------
A5_OLD = '''        async def observed_receive() -> Message:
            nonlocal received_bytes, request_body_truncated, request_body_complete
            message = await receive()
            if message["type"] == "http.disconnect" and not completed:
                context.result = "cancelled"
                context.error = "Downstream disconnected (client or reverse proxy)"
                if model_call:
                    request_event("disconnected", context.error)
            elif message["type"] == "http.request":
                body = message.get("body", b"")
                received_bytes += len(body)
                request_body_complete = not message.get("more_body", False)
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
                if dump_enabled and not request_body_truncated:
                    if len(request_body) + len(body) <= settings.dump_max_bytes:
                        request_body.extend(body)
                    else:
                        request_body.clear()
                        request_body_truncated = True
                if received_bytes > body_limit:
                    raise HTTPException(413, "Request body exceeds configured limit")
            return message
'''
A5_NEW = '''        async def observed_receive() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect" and not observer.completed:
                context.result = "cancelled"
                context.error = "Downstream disconnected (client or reverse proxy)"
                if model_call:
                    request_event("disconnected", context.error)
            elif message["type"] == "http.request":
                body = message.get("body", b"")
                observer.received_bytes += len(body)
                observer.request_body_complete = not message.get("more_body", False)
                if (
                    model_call
                    and not context.requested_model
                    and observer.received_bytes <= 65536
                ):
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
                # 管理路径同样施加请求体上限（1MB）：写端点无界接收可把
                # 磁盘与 yaml 解析 CPU 打爆（实测 50MB config 被完整落盘）
                body_limit = settings.max_request_bytes if model_call else 1_048_576
                if dump_enabled and not observer.request_body_truncated:
                    if (
                        len(observer.request_body) + len(body)
                        <= settings.dump_max_bytes
                    ):
                        observer.request_body.extend(body)
                    else:
                        observer.request_body.clear()
                        observer.request_body_truncated = True
                if observer.received_bytes > body_limit:
                    raise HTTPException(413, "Request body exceeds configured limit")
            return message
'''
assert text.count(A5_OLD) == 1, "anchor5 observed_receive"
text = text.replace(A5_OLD, A5_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 6: observed_send rewrite
# ---------------------------------------------------------------------------
A6_OLD = '''        async def observed_send(message: Message) -> None:
            nonlocal \\
                status, \\
                response_started, \\
                completed, \\
                is_stream, \\
                first_byte_ms, \\
                response_headers, \\
                response_body_truncated, \\
                sending, \\
                json_payload_sent, \\
                stream_terminal_sent
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
            sending = True
            if model_call:
                try:
                    # Bound the actual socket send outside BaseHTTPMiddleware's
                    # memory channels, including the final timeout error body.
                    async with asyncio.timeout(settings.stream_send_timeout):
                        await send(message)
                except TimeoutError as exc:
                    context.result = "cancelled"
                    context.error = "Downstream response send timed out"
                    raise OSError(context.error) from exc
            else:
                await send(message)
            sending = False
            if message["type"] == "http.response.body":
                if not message.get("more_body", False):
                    completed = True
                if is_stream and _is_terminal_event(path, message.get("body", b"")):
                    stream_terminal_sent = True
                if (
                    not is_stream
                    and scope.get("state", {}).get("json_keepalive_failure")
                    and message.get("body", b"").strip()
                ):
                    json_payload_sent = True
'''
A6_NEW = '''        async def observed_send(message: Message) -> None:
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
                    context.result = "cancelled"
                    context.error = "Downstream response send timed out"
                    raise OSError(context.error) from exc
            else:
                await send(message)
            observer.sending = False
            if message["type"] == "http.response.body":
                if not message.get("more_body", False):
                    observer.completed = True
                if observer.is_stream and _is_terminal_event(
                    path, message.get("body", b"")
                ):
                    observer.stream_terminal_sent = True
                if (
                    not observer.is_stream
                    and scope.get("state", {}).get("json_keepalive_failure")
                    and message.get("body", b"").strip()
                ):
                    observer.json_payload_sent = True
'''
assert text.count(A6_OLD) == 1, "anchor6 observed_send"
text = text.replace(A6_OLD, A6_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 7: except TimeoutError branch
# ---------------------------------------------------------------------------
A7_OLD = '''        except TimeoutError:
            if not request_timeout.expired():
                context.result, context.error = "error", "Upstream operation timed out"
                logger.exception("Request raised an upstream timeout")
                if not response_started:
                    await reply(500, "Internal server error")
                else:
                    raise
                return
            if sending:
                # The transport may have delivered part of this frame already.
                # Never append an error to an interrupted JSON/SSE write.
                context.result = "cancelled"
                context.error = "Request timeout interrupted downstream send"
                raise OSError(context.error) from None
            if completed:
                return
            if json_payload_sent or stream_terminal_sent:
                # The result or terminal event has reached ASGI send. Only end
                # HTTP; appending an error would corrupt or contradict it.
                await observed_send({"type": "http.response.body", "body": b""})
                return
            context.result, context.error = "timeout", "Request exceeded total timeout"
            if not response_started:
                await reply(504, context.error)
            else:
                json_failure = scope.get("state", {}).get("json_keepalive_failure")
                if not is_stream and callable(json_failure):
                    body = cast(Callable[[str], bytes], json_failure)(context.error)
                    await observed_send({"type": "http.response.body", "body": body})
                    return
                failure = scope.get("state", {}).get("stream_failure")
                if is_stream and callable(failure):
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
                        "body": b'data: {"error":{"code":504,"message":"Request exceeded total timeout"}}\\n\\n'
                        if is_stream
                        else b"",
                    }
                )
'''
A7_NEW = '''        except TimeoutError:
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
            if observer.completed:
                return
            if observer.json_payload_sent or observer.stream_terminal_sent:
                # The result or terminal event has reached ASGI send. Only end
                # HTTP; appending an error would corrupt or contradict it.
                await observed_send({"type": "http.response.body", "body": b""})
                return
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
                        "body": b'data: {"error":{"code":504,"message":"Request exceeded total timeout"}}\\n\\n'
                        if observer.is_stream
                        else b"",
                    }
                )
'''
assert text.count(A7_OLD) == 1, "anchor7 timeout branch"
text = text.replace(A7_OLD, A7_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 8: except Exception branch
# ---------------------------------------------------------------------------
A8_OLD = '''        except Exception:
            context.result, context.error = "error", "Internal server error"
            logger.exception("Request failed")
            if not response_started:
                await reply(500, context.error)
            else:
                raise
'''
A8_NEW = '''        except Exception:
            context.result, context.error = "error", "Internal server error"
            logger.exception("Request failed")
            if not observer.response_started:
                await reply(500, context.error)
            else:
                raise
'''
assert text.count(A8_OLD) == 1, "anchor8 exception branch"
text = text.replace(A8_OLD, A8_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 9: finally block
# ---------------------------------------------------------------------------
A9_OLD = '''        finally:
            if admitted:
                self.active -= 1
            try:
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
                    # Drain bookkeeping, then preserve cancellation semantics.
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
                            original_scope,
                            received_bytes,
                            request_body_complete,
                        )
                    )
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
            finally:
                if model_call and admitted:
                    unregister_request(context.request_id)
                current_request.reset(token)
'''
A9_NEW = '''        finally:
            if admitted:
                self.active -= 1
            try:
                elapsed_ms = (time.perf_counter() - started) * 1000
                if not context.result:
                    context.result = (
                        "rate_limited"
                        if observer.status == 429
                        else "success"
                        if observer.completed and observer.status < 400
                        else "error"
                    )
                if not observer.completed and context.result == "success":
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
                if model_call and admitted:
                    unregister_request(context.request_id)
                current_request.reset(token)
'''
assert text.count(A9_OLD) == 1, "anchor9 finally"
text = text.replace(A9_OLD, A9_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 10: _persist signature + body references
# ---------------------------------------------------------------------------
A10_OLD = '''    @staticmethod
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
        history_start: asyncio.Task[None] | None,
        original_scope: Scope,
        received_bytes: int,
        request_body_complete: bool,
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
                if is_dump_to_file_enabled():
                    # Keep the ASGI body verbatim, before validation, defaults,
                    # model mapping or protocol conversion. Only transport
                    # credentials are masked; tool schemas/prompts are data.
                    encoding = "utf-8"
                    try:
                        body_text = request_body.decode("utf-8")
                    except UnicodeDecodeError:
                        body_text = base64.b64encode(request_body).decode("ascii")
                        encoding = "base64"
                    raw_path = original_scope.get("raw_path")
                    target = (
                        raw_path.decode("latin1")
                        if raw_path is not None
                        else original_scope["path"]
                    )
                    query = original_scope.get("query_string", b"").decode("latin1")
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
                        "body": None if request_body_truncated else body_text,
                        "body_encoding": encoding,
                        "body_bytes": received_bytes,
                        "complete": request_body_complete
                        and not request_body_truncated,
                        "truncated": request_body_truncated,
                    }
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
                    client_request=client_request,
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
'''
A10_NEW = '''    @staticmethod
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
                if is_dump_to_file_enabled():
                    # Keep the ASGI body verbatim, before validation, defaults,
                    # model mapping or protocol conversion. Only transport
                    # credentials are masked; tool schemas/prompts are data.
                    encoding = "utf-8"
                    try:
                        body_text = observer.request_body.decode("utf-8")
                    except UnicodeDecodeError:
                        body_text = base64.b64encode(
                            observer.request_body
                        ).decode("ascii")
                        encoding = "base64"
                    raw_path = original_scope.get("raw_path")
                    target = (
                        raw_path.decode("latin1")
                        if raw_path is not None
                        else original_scope["path"]
                    )
                    query = original_scope.get("query_string", b"").decode("latin1")
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
                        and not observer.request_body_truncated,
                        "truncated": observer.request_body_truncated,
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
'''
assert text.count(A10_OLD) == 1, "anchor10 _persist"
text = text.replace(A10_OLD, A10_NEW, 1)

SRC.write_text(text, encoding="utf-8")
print("split_middleware.py: all 11 anchors replaced OK")
sys.exit(0)
