"""Final client outcomes and latency distributions, independent of retry penalties."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime


def _number(value: object) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0
    )


def _sorted_percentile(values: list[float], fraction: float) -> float | None:
    """Interpolate an already sorted sample without allocating another copy."""
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return round(values[lower] + (values[upper] - values[lower]) * (index - lower), 2)


@dataclass(slots=True)
class Metrics:
    requests: int = 0
    success: int = 0
    rate_limited: int = 0
    errors: int = 0
    attempts: int = 0
    retried: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    results: dict[str, int] = field(default_factory=dict)
    finish_reasons: dict[str, int] = field(default_factory=dict)
    latency: list[float] = field(default_factory=list)
    first_byte: list[float] = field(default_factory=list)
    last_used: float = 0

    def add(self, row: dict[str, object]) -> None:
        _add_to_groups(row, _number(row.get("started_at")), self)

    def export(self) -> dict[str, object]:
        self.latency.sort()
        self.first_byte.sort()
        return {
            "requests": self.requests,
            "success": self.success,
            "rate_limited": self.rate_limited,
            "errors": self.errors,
            "attempts": self.attempts,
            "retried": self.retried,
            "success_rate": round(self.success / self.requests * 100, 2)
            if self.requests
            else None,
            "retry_rate": round(self.retried / self.requests * 100, 2)
            if self.requests
            else None,
            "latency_p50_ms": _sorted_percentile(self.latency, 0.5),
            "latency_p95_ms": _sorted_percentile(self.latency, 0.95),
            "first_byte_p50_ms": _sorted_percentile(self.first_byte, 0.5),
            "first_byte_p95_ms": _sorted_percentile(self.first_byte, 0.95),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "results": self.results,
            "finish_reasons": self.finish_reasons,
            "last_used": datetime.fromtimestamp(self.last_used, tz=UTC).isoformat()
            if self.last_used
            else None,
        }


def _add_to_groups(row: dict[str, object], started_at: float, *groups: Metrics) -> None:
    """Count a row in every matching group without repeating its conversions."""
    result = str(row.get("result") or "error")
    success = result == "success"
    rate_limited = result == "rate_limited"
    error = not (success or rate_limited)
    attempts = int(_number(row.get("attempts")))
    retried = attempts > 1
    finish = str(row.get("finish_reason") or "")
    prompt = _number(row.get("prompt_tokens"))
    completion = _number(row.get("completion_tokens"))
    # A missing or truncated-zero total falls back to the untruncated sum.
    total = int(_number(row.get("total_tokens"))) or int(prompt + completion)
    prompt_tokens = int(prompt)
    completion_tokens = int(completion)
    reasoning_tokens = int(_number(row.get("reasoning_tokens")))
    cached_tokens = int(_number(row.get("cached_tokens")))
    elapsed = row.get("elapsed_ms")
    first_byte = row.get("first_byte_ms")
    latency_ms = None if elapsed is None else _number(elapsed)
    first_byte_ms = None if first_byte is None else _number(first_byte)
    for group in groups:
        group.requests += 1
        group.success += success
        group.rate_limited += rate_limited
        group.errors += error
        group.attempts += attempts
        group.retried += retried
        group.results[result] = group.results.get(result, 0) + 1
        if finish:
            group.finish_reasons[finish] = group.finish_reasons.get(finish, 0) + 1
        group.prompt_tokens += prompt_tokens
        group.completion_tokens += completion_tokens
        group.total_tokens += total
        group.reasoning_tokens += reasoning_tokens
        group.cached_tokens += cached_tokens
        if latency_ms is not None:
            group.latency.append(latency_ms)
        if first_byte_ms is not None:
            group.first_byte.append(first_byte_ms)
        if started_at > group.last_used:
            group.last_used = started_at


def summarize(
    rows: Iterable[dict[str, object]], *, since: float, until: float
) -> dict[str, object]:
    totals = Metrics()
    models: defaultdict[str, Metrics] = defaultdict(Metrics)
    accounts: defaultdict[str, Metrics] = defaultdict(Metrics)
    keys: defaultdict[str, Metrics] = defaultdict(Metrics)
    model_accounts: defaultdict[str, defaultdict[str, Metrics]] = defaultdict(
        lambda: defaultdict(Metrics)
    )
    names: dict[str, str] = {}
    interval = 3600 if until - since <= 2 * 86400 else 86400
    series: defaultdict[int, Metrics] = defaultdict(Metrics)
    for row in rows:
        model = str(row.get("model") or "未识别模型").removeprefix("models/")
        account = str(row.get("account_id") or "未调度")
        key = str(row.get("key_id") or "匿名")
        names[account] = str(row.get("account_name") or names.get(account) or account)
        started_at = _number(row.get("started_at"))
        bucket = int(started_at // interval) * interval
        _add_to_groups(
            row,
            started_at,
            totals,
            models[model],
            model_accounts[model][account],
            accounts[account],
            keys[key],
            series[bucket],
        )
    # Include empty buckets so idle periods remain visible.
    for bucket in range(
        int(since // interval) * interval,
        int(until // interval) * interval + 1,
        interval,
    ):
        series.setdefault(bucket, Metrics())
    return {
        "totals": totals.export(),
        "models": {
            key: {
                **value.export(),
                "accounts": [
                    {"account_id": account, "name": names[account], **metrics.export()}
                    for account, metrics in model_accounts[key].items()
                ],
            }
            for key, value in models.items()
        },
        "accounts": {
            key: {**value.export(), "name": names[key]}
            for key, value in accounts.items()
        },
        "keys": {key: value.export() for key, value in keys.items()},
        "series": [
            {"timestamp": stamp, **value.export()}
            for stamp, value in sorted(series.items())
        ],
        "since": since,
        "until": until,
        "interval_seconds": interval,
        "basis": "final_client_outcome",
    }
