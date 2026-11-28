"""Pin the dashboard's exact results while reusing one normalized observation."""

from __future__ import annotations

import time
from contextlib import closing
from datetime import UTC, datetime
from typing import Any, cast

from aistudio_api.infrastructure.request_history import RequestHistory
from aistudio_api.infrastructure.request_metrics import summarize


def test_filtered_history_keeps_exact_metrics_in_all_six_groups(tmp_path):
    start = int(time.time() // 3600) * 3600
    defaults = {
        "started_at": start,
        "model": "m",
        "account_id": "a",
        "key_id": "k",
        "result": "success",
        "status_code": 200,
    }
    records = [
        {
            "model": "models/m",
            "account_name": "Example",
            "attempts": [1],
            "first_byte_ms": 10,
            "finish_reason": "STOP",
            "usage": {
                "prompt_tokens": 1.75,
                "completion_tokens": 0.75,
                "total_tokens": 0.9,  # Truncates to zero; fallback is int(2.5).
                "cached_tokens": "400",  # Numeric strings are ignored.
            },
        },
        {
            "started_at": start + 1,
            "result": "rate_limited",
            "attempts": [1, 2],
            "elapsed_ms": 0,
            "first_byte_ms": "malformed",
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 17,
                "cached_tokens": True,
                "completion_tokens_details": {"reasoning_tokens": -1.5},
            },
        },
        {"started_at": start + 2, "result": "", "elapsed_ms": 10, "finish_reason": 23},
        {"result": "running"},
        {"account_id": "b"},
        {"model": "other"},
        {"started_at": start - 1},
    ]
    with closing(RequestHistory(tmp_path / "metrics.sqlite3")) as history:
        for i, record in enumerate(records):
            history.insert({**defaults, **record, "request_id": str(i)})
        report = cast(
            dict[str, Any],
            history.metrics(
                since=start, until=start + 7200, model="models/m", account_id="a"
            ),
        )

    expected = {
        "requests": 3,
        "success": 1,
        "rate_limited": 1,
        "errors": 1,
        "attempts": 3,
        "retried": 1,
        "success_rate": 33.33,
        "retry_rate": 33.33,
        "latency_p50_ms": 5.0,
        "latency_p95_ms": 9.5,
        "first_byte_p50_ms": 5.0,
        "first_byte_p95_ms": 9.5,
        "prompt_tokens": 3,
        "completion_tokens": 3,
        "total_tokens": 19,
        "reasoning_tokens": -1,
        "cached_tokens": 1,
        "results": {"success": 1, "rate_limited": 1, "error": 1},
        "finish_reasons": {"STOP": 1, "23": 1},
        "last_used": datetime.fromtimestamp(start + 2, tz=UTC).isoformat(),
    }
    assert report["totals"] == expected
    assert list(report["totals"]) == list(expected)
    assert report["keys"] == {"k": expected}
    assert report["accounts"] == {"a": {**expected, "name": "Example"}}
    assert report["models"] == {
        "m": {
            **expected,
            "accounts": [{"account_id": "a", "name": "Example", **expected}],
        }
    }
    first, *idle = report["series"]
    assert first == {"timestamp": start, **expected}
    assert [bucket["timestamp"] for bucket in idle] == [start + 3600, start + 7200]
    for bucket in idle:
        assert bucket["requests"] == bucket["total_tokens"] == 0
        assert bucket["success_rate"] is bucket["latency_p95_ms"] is None
        assert bucket["first_byte_p50_ms"] is bucket["last_used"] is None


def test_group_order_latest_names_and_daily_empty_buckets():
    rows: list[dict[str, object]] = [
        {"model": "models/z", "account_id": "b", "account_name": "Old", "key_id": "t"},
        {"model": "a", "account_id": "b", "account_name": "New", "key_id": "k"},
        {"model": "z", "account_id": "a", "started_at": 86400.5},
        {"model": "z", "account_id": "b", "elapsed_ms": float("inf")},
        {},
    ]
    report = cast(dict[str, Any], summarize(iter(rows), since=0, until=172801))
    assert list(report["models"]) == ["z", "a", "未识别模型"]
    assert list(report["accounts"]) == ["b", "a", "未调度"]
    assert list(report["keys"]) == ["t", "k", "匿名"]
    assert [
        (entry["account_id"], entry["name"], entry["requests"])
        for entry in report["models"]["z"]["accounts"]
    ] == [("b", "New", 2), ("a", "a", 1)]
    assert report["models"]["a"]["accounts"][0]["name"] == "New"
    assert report["accounts"]["b"]["latency_p95_ms"] == 0
    assert report["accounts"]["未调度"]["latency_p95_ms"] is None
    assert report["interval_seconds"] == 86400
    assert [(b["timestamp"], b["requests"]) for b in report["series"]] == [
        (0, 4),
        (86400, 1),
        (172800, 0),
    ]
