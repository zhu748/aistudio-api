"""Pin token columns and last-used semantics on the stats route and raw records."""

from __future__ import annotations

import time
from contextlib import closing

import httpx
from fastapi import FastAPI

from aistudio_api.api.routes_operations import router
from aistudio_api.infrastructure.request_history import request_history
from aistudio_api.infrastructure.request_metrics import Metrics

_TOKENS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_tokens",
)


def _assert_tokens(group, expected):
    fields = [(key, value) for key, value in group.items() if key in _TOKENS]
    assert fields == list(zip(_TOKENS, expected, strict=True))
    assert all(type(value) is int for _, value in fields)


def test_raw_metrics_ignore_nonfinite_timestamps_and_keep_integer_fallback():
    metrics = Metrics()
    for stamp in (None, "42", float("nan"), float("inf"), float("-inf"), -1):
        metrics.add({"started_at": stamp, "cached_tokens": float("nan")})
    assert metrics.export()["last_used"] is None
    _assert_tokens(metrics.export(), (0, 0, 0, 0, 0))

    metrics.add(
        {
            "started_at": 42.5,
            "prompt_tokens": -2.75,
            "completion_tokens": -0.75,
            "total_tokens": -0.5,  # int(total) is zero; truncate the sum instead.
            "reasoning_tokens": float("inf"),
            "cached_tokens": True,
        }
    )
    for stamp in (42.5, 1, float("nan")):
        metrics.add({"started_at": stamp})
    exported = metrics.export()
    assert exported["last_used"] == "1970-01-01T00:00:42.500000+00:00"
    _assert_tokens(exported, (-2, 0, -3, 0, 1))


async def test_stats_route_keeps_distinct_sql_columns_in_each_group(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    start = (int(time.time()) // 3600 - 1) * 3600
    record = {"started_at": start, "result": "success", "status_code": 200}
    app = FastAPI()
    app.include_router(router)
    with closing(request_history()) as history:
        history.insert(
            {
                **record,
                "request_id": "rich",
                "model": "models/m",
                "account_id": "a",
                "key_id": "k",
                "usage": {
                    "prompt_tokens": 13,
                    "completion_tokens": 17,
                    "total_tokens": 120,
                    "completion_tokens_details": {"reasoning_tokens": 19},
                    "cached_tokens": 7,
                },
            }
        )
        history.insert(
            {
                **record,
                "request_id": "sparse",
                "model": "",
                "account_id": "",
                "key_id": "",
                "usage": {"prompt_tokens": -3, "cached_tokens": True},
            }
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/requests/stats", params={"since": start, "until": start + 3600}
            )
    assert response.status_code == 200
    report = response.json()
    for group in (report["totals"], report["series"][0]):
        _assert_tokens(group, (10, 17, 117, 19, 8))
    _assert_tokens(report["series"][1], (0, 0, 0, 0, 0))
    for model, account, key, expected in [
        ("m", "a", "k", (13, 17, 120, 19, 7)),
        ("未识别模型", "未调度", "匿名", (-3, 0, -3, 0, 1)),
    ]:
        for group in (
            report["models"][model],
            report["models"][model]["accounts"][0],
            report["accounts"][account],
            report["keys"][key],
        ):
            _assert_tokens(group, expected)
