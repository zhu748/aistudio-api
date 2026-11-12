"""Final client outcomes and latency distributions, independent of retry penalties."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime


def _number(value: object) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 2
    )


@dataclass
class Metrics:
    requests: int = 0
    success: int = 0
    rate_limited: int = 0
    errors: int = 0
    attempts: int = 0
    retried: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    results: dict[str, int] = field(default_factory=dict)
    finish_reasons: dict[str, int] = field(default_factory=dict)
    latency: list[float] = field(default_factory=list)
    first_byte: list[float] = field(default_factory=list)
    last_used: float = 0

    def add(self, row: dict[str, object]) -> None:
        self.requests += 1
        result = str(row.get("result") or "error")
        self.success += int(result == "success")
        self.rate_limited += int(result == "rate_limited")
        self.errors += int(result not in ("success", "rate_limited"))
        attempts = int(_number(row.get("attempts")))
        self.attempts += attempts
        self.retried += int(attempts > 1)
        self.results[result] = self.results.get(result, 0) + 1
        finish = str(row.get("finish_reason") or "")
        if finish:
            self.finish_reasons[finish] = self.finish_reasons.get(finish, 0) + 1
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
        ):
            value = int(_number(row.get(key)))
            if key == "total_tokens" and not value:
                value = int(
                    _number(row.get("prompt_tokens"))
                    + _number(row.get("completion_tokens"))
                )
            self.tokens[key] = self.tokens.get(key, 0) + value
        if row.get("elapsed_ms") is not None:
            self.latency.append(_number(row["elapsed_ms"]))
        if row.get("first_byte_ms") is not None:
            self.first_byte.append(_number(row["first_byte_ms"]))
        self.last_used = max(self.last_used, _number(row.get("started_at")))

    def export(self) -> dict[str, object]:
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
            "latency_p50_ms": _percentile(self.latency, 0.5),
            "latency_p95_ms": _percentile(self.latency, 0.95),
            "first_byte_p50_ms": _percentile(self.first_byte, 0.5),
            "first_byte_p95_ms": _percentile(self.first_byte, 0.95),
            **{
                key: self.tokens.get(key, 0)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "reasoning_tokens",
                    "cached_tokens",
                )
            },
            "results": self.results,
            "finish_reasons": self.finish_reasons,
            "last_used": datetime.fromtimestamp(self.last_used, tz=UTC).isoformat()
            if self.last_used
            else None,
        }


def summarize(
    rows: list[dict[str, object]], *, since: float, until: float
) -> dict[str, object]:
    totals = Metrics()
    models: dict[str, Metrics] = {}
    accounts: dict[str, Metrics] = {}
    keys: dict[str, Metrics] = {}
    model_accounts: dict[str, dict[str, Metrics]] = {}
    names: dict[str, str] = {}
    interval = 3600 if until - since <= 2 * 86400 else 86400
    series: dict[int, Metrics] = {}
    for row in rows:
        totals.add(row)
        model = str(row.get("model") or "未识别模型").removeprefix("models/")
        account = str(row.get("account_id") or "未调度")
        key = str(row.get("key_id") or "匿名")
        names[account] = str(row.get("account_name") or names.get(account) or account)
        models.setdefault(model, Metrics()).add(row)
        model_accounts.setdefault(model, {}).setdefault(account, Metrics()).add(row)
        accounts.setdefault(account, Metrics()).add(row)
        keys.setdefault(key, Metrics()).add(row)
        bucket = int(_number(row.get("started_at")) // interval) * interval
        series.setdefault(bucket, Metrics()).add(row)
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
