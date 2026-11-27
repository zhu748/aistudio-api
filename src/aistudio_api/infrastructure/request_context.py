"""Mutable request context shared across ASGI tasks, without storing payloads."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
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
    thought_signature: dict[str, object] | None = None


# 每请求都会调 record_requested_parameters：两组白名单集合提到模块级，
# 避免每次调用重建（原实现每次 new 两个 set）。
_REQUESTED_PARAMETER_KEYS = frozenset(
    {
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
        "reasoning",
        "thinking",
        "thinkingConfig",
        "enable_thinking",
        # Claude effort 协议（2026-11）：Claude Code 的思考强度选择器走
        # 顶层 output_config.effort，一并进请求详情便于排障。
        "output_config",
    }
)
_THINKING_KEYS = frozenset(
    {
        "type",
        "effort",
        "enabled",
        "exclude",
        "budget_tokens",
        "thinkingBudget",
        "thinking_budget",
        "budget",
        "max_budget_tokens",
        "thinkingLevel",
        "thinking_level",
        "level",
        "mode",
        "includeThoughts",
        "include_thoughts",
    }
)


def record_requested_parameters(parameters: dict[str, object]) -> None:
    """Keep generation settings only; never retain prompts, tools or credentials."""
    context = current_request.get()
    if context is None:
        return
    allowed = _REQUESTED_PARAMETER_KEYS
    thinking_keys = _THINKING_KEYS

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


def active_request_count() -> int:
    """只取活跃请求数（面板统计用），避免为计数深拷贝每个上下文。"""
    with _active_lock:
        return len(_active)


# 请求详情的快照字段：按字段名直接取，列表/字典做一层浅拷贝。
# 原先用 dataclasses.asdict 递归深拷贝（100 事件/8 尝试/32 签名条目实测
# 128.9 µs，逐字段复制 4.7 µs），每请求两次且都在事件循环上。
_SNAPSHOT_FIELDS = tuple(
    field_info.name
    for field_info in fields(RequestContext)
    if field_info.name != "started_monotonic"
)
_SNAPSHOT_SHALLOW = frozenset(
    {"usage", "attempts", "events", "request_parameters", "effective_parameters"}
)


def request_snapshot(
    context: RequestContext, *, running: bool = False
) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for name in _SNAPSHOT_FIELDS:
        value = getattr(context, name)
        snapshot[name] = (
            list(value)
            if name in _SNAPSHOT_SHALLOW and isinstance(value, list)
            else dict(value)
            if isinstance(value, dict)
            else value
        )
    snapshot["result"] = "running" if running else context.result
    snapshot["elapsed_ms"] = round(
        (time.perf_counter() - context.started_monotonic) * 1000, 2
    )
    return snapshot


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
