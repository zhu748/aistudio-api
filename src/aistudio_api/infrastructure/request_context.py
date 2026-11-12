"""Mutable request context shared across ASGI tasks, without storing payloads."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aistudio_api.infrastructure.utils.redaction import redact_text


@dataclass
class RequestContext:
    request_id: str
    category: str = "system"
    protocol: str = ""
    requested_model: str = ""
    model: str = ""
    account_id: str = ""
    account_name: str = ""
    auth_user: str = ""
    key_id: str = ""
    proxy: str = ""
    signer: str = ""
    result: str = ""
    error: str = ""
    finish_reason: str = ""
    usage: dict[str, object] = field(default_factory=dict)
    attempts: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    stage: str = "received"
    started_at: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.perf_counter, repr=False)
    method: str = "POST"
    path: str = ""
    is_stream: bool = False
    request_parameters: dict[str, object] = field(default_factory=dict)
    effective_parameters: dict[str, object] = field(default_factory=dict)
    first_thought_ms: float | None = None
    first_content_ms: float | None = None
    first_byte_ms: float | None = None
    status_code: int = 0


def record_requested_parameters(parameters: dict[str, object]) -> None:
    """Keep generation settings only; never retain prompts, tools or credentials."""
    context = current_request.get()
    if context is None:
        return
    allowed = {
        "temperature",
        "top_p",
        "topP",
        "top_k",
        "topK",
        "max_tokens",
        "max_completion_tokens",
        "maxOutputTokens",
        "reasoning_effort",
        "reasoningEffort",
        "thinking",
        "thinkingConfig",
    }
    thinking_keys = {
        "type",
        "budget_tokens",
        "thinkingBudget",
        "thinking_budget",
        "budget",
        "thinkingLevel",
        "thinking_level",
        "level",
        "mode",
        "includeThoughts",
        "include_thoughts",
    }

    def scalar(value: object) -> object:
        return (
            redact_text(value)[:64]
            if isinstance(value, str)
            else value
            if isinstance(value, (bool, int, float))
            else None
        )

    context.request_parameters = {
        key: {str(k): scalar(v) for k, v in value.items() if k in thinking_keys}
        if isinstance(value, dict)
        else [scalar(item) for item in value[:4]]
        if isinstance(value, list)
        else scalar(value)
        for key, value in parameters.items()
        if key in allowed and value is not None
    }


def record_output_timing(kind: str) -> None:
    context = current_request.get()
    if context is None:
        return
    elapsed = round((time.perf_counter() - context.started_monotonic) * 1000, 2)
    if kind in ("thinking", "reasoning_images") and context.first_thought_ms is None:
        context.first_thought_ms = elapsed
        request_event("thinking", "首次收到思考输出")
    elif kind in ("body", "images", "tool_calls") and context.first_content_ms is None:
        context.first_content_ms = elapsed
        request_event("answering", "首次收到回复内容")


def record_attempt_error(code: int, message: str) -> None:
    context = current_request.get()
    if context is None:
        return
    safe_message = redact_text(message)[:500]
    if context.attempts:
        context.attempts[-1].update(error_code=code, error=safe_message)
    request_event("attempt_error", f"上游错误 {code} · {safe_message}")


_active: dict[str, RequestContext] = {}
_active_lock = threading.Lock()


def register_request(context: RequestContext) -> None:
    with _active_lock:
        _active[context.request_id] = context


def unregister_request(request_id: str) -> None:
    with _active_lock:
        _active.pop(request_id, None)


def active_requests() -> list[dict[str, object]]:
    with _active_lock:
        contexts = list(_active.values())
    return [request_snapshot(context, running=True) for context in contexts]


def request_snapshot(
    context: RequestContext, *, running: bool = False
) -> dict[str, object]:
    return {
        **{
            key: value
            for key, value in asdict(context).items()
            if key != "started_monotonic"
        },
        "result": "running" if running else context.result,
        "elapsed_ms": round(
            (time.perf_counter() - context.started_monotonic) * 1000, 2
        ),
    }


def request_event(stage: str, message: str) -> None:
    context = current_request.get()
    if context is None or context.category != "model":
        return
    context.stage = stage
    if len(context.events) < 100:
        context.events.append(
            {
                "stage": stage,
                "message": redact_text(message)[:500],
                "elapsed_ms": round(
                    (time.perf_counter() - context.started_monotonic) * 1000, 2
                ),
                "account_id": context.account_id,
            }
        )


def log_context() -> dict[str, str]:
    context = current_request.get()
    return {
        "request_id": context.request_id if context else "",
        "category": context.category if context else "system",
        "model": context.model if context else "",
        "account_id": context.account_id if context else "",
        "stage": context.stage if context else "",
    }


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
    request_event("upstream", "向上游发送请求")
    try:
        yield attempt
    except BaseException as exc:
        attempt["error"] = redact_text(str(exc))[:500]
        raise
    finally:
        attempt["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        request_event(
            "upstream_end",
            f"上游尝试结束 · HTTP {attempt['status_code']}"
            + (f" · {attempt['error']}" if attempt.get("error") else ""),
        )
