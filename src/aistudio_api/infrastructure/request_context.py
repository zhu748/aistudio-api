"""Mutable request context shared across ASGI tasks, without storing payloads."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from aistudio_api.infrastructure.utils.redaction import redact_text


@dataclass
class RequestContext:
    request_id: str
    model: str = ""
    account_id: str = ""
    auth_user: str = ""
    key_id: str = ""
    proxy: str = ""
    signer: str = ""
    result: str = ""
    error: str = ""
    finish_reason: str = ""
    usage: dict[str, object] = field(default_factory=dict)
    attempts: list[dict[str, object]] = field(default_factory=list)


current_request: ContextVar[RequestContext | None] = ContextVar(
    "aistudio_request_context", default=None
)


def record_result(
    result: str,
    *,
    error: str = "",
    usage: dict[str, object] | None = None,
    finish_reason: str = "",
) -> None:
    context = current_request.get()
    if context is not None:
        context.result = result
        context.error = error
        if usage is not None:
            context.usage = dict(usage)
        if finish_reason:
            context.finish_reason = finish_reason


@contextmanager
def upstream_attempt(
    auth_file: str | None, auth_user: str, proxy: str
) -> Iterator[dict[str, object]]:
    context = current_request.get()
    attempt: dict[str, object] = {"status_code": 0}
    started = time.perf_counter()
    if context is not None:
        context.account_id = Path(auth_file).parent.name if auth_file else ""
        context.auth_user = auth_user
        context.proxy = redact_text(proxy)
        attempt.update(account_id=context.account_id, proxy=context.proxy)
        if len(context.attempts) < 32:
            context.attempts.append(attempt)
    try:
        yield attempt
    except BaseException as exc:
        attempt["error"] = redact_text(str(exc))[:500]
        raise
    finally:
        attempt["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
