"""Scan-filtered history pages avoid a second scan only when the total is known."""

from __future__ import annotations

import time
from contextlib import closing, contextmanager
from itertools import chain

import httpx
import pytest
from fastapi import FastAPI

from aistudio_api.api import routes_operations
from aistudio_api.infrastructure.request_history import RequestHistory


@pytest.fixture
def history(tmp_path):
    now = time.time()
    statuses = {2: "error", 8: "error", 12: "error", 16: "running"}
    records = [
        {
            "request_id": f"req_{i:02d}",
            "started_at": now + i // 6,  # Ties across page boundaries.
            "model": ("m", "models/m", "other")[i % 3],
            "account_id": "a",
            "account_name": "青" if i % 4 < 2 else "other",
            "key_id": "k",
            "result": statuses.get(i, "success"),
            "status_code": 500 if statuses.get(i) == "error" else 200,
            "protocol": "openai" if i % 2 == 0 else "gemini",
            "attempts": [{"n": 1}, {"n": 2}] if i % 3 == 0 else [{"n": 1}],
            "elapsed_ms": 10 * i if i % 5 else None,
        }
        for i in range(18)
    ]
    with closing(RequestHistory(tmp_path / "history.sqlite3")) as store:
        for record in chain(records[1::2], records[::2]):
            store.insert(record)
        yield store, records


def _trace_reader(store, monkeypatch, callback):
    original = store._connect

    @contextmanager
    def connect():
        with original() as connection:
            connection.set_trace_callback(callback)
            yield connection

    monkeypatch.setattr(store, "_connect", connect)


def _assert_page_first(statements, page, size, *, count):
    assert statements[0] == "BEGIN DEFERRED"
    assert statements[1].startswith("SELECT payload ")
    assert statements[1].endswith(f" LIMIT {size} OFFSET {(page - 1) * size}")
    assert len(statements) == (4 if count else 3)
    if count:
        assert statements[2].startswith("SELECT COUNT(*)")
    assert statements[-1] == "COMMIT"


async def test_dashboard_and_export_skip_count_for_each_short_scan(
    history, monkeypatch
):
    store, records = history
    statements = []
    _trace_reader(store, monkeypatch, statements.append)
    monkeypatch.setattr(routes_operations, "request_history", lambda: store)
    app = FastAPI()
    app.include_router(routes_operations.router)
    cases = [
        ("/api/requests", {"search": "青"}, [17, 13, 12, 9, 8, 5, 4, 1, 0]),
        ("/api/requests", {"protocol": "openai"}, [14, 12, 10, 8, 6, 4, 2, 0]),
        ("/api/requests", {"retried": True}, [15, 12, 9, 6, 3, 0]),
        ("/api/requests", {"min_elapsed_ms": 130}, [17, 14, 13]),
        (
            "/api/requests/export",
            {
                "model": "models/m",
                "result": "failed",
                "search": "青",
                "protocol": "openai",
                "retried": True,
                "min_elapsed_ms": 100,
            },
            [12],
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for path, filters, ids in cases:
            statements.clear()
            response = await client.get(path, params=filters)
            size = 5000 if path.endswith("/export") else 50
            assert response.status_code == 200
            assert response.json() == {
                "items": [records[i] for i in ids],
                "total": len(ids),
                "page": 1,
                "page_size": size,
            }
            _assert_page_first(statements, 1, size, count=False)


@pytest.mark.parametrize(
    ("protocol", "page", "size", "count"),
    [
        ("missing", 1, 9, False),  # Empty first page.
        ("openai", 1, 9, False),
        ("openai", 2, 5, False),  # Later, nonempty short page.
        ("openai", 1, 4, True),
        ("openai", 2, 4, True),  # Exactly full final page.
        ("openai", 3, 4, True),  # Later empty page.
    ],
)
def test_filtered_page_and_total_share_snapshot(
    history, monkeypatch, protocol, page, size, count
):
    store, records = history
    matches = [
        r
        for r in reversed(records)
        if r["protocol"] == protocol and r["result"] != "running"
    ]
    added = {**records[-1], "request_id": "req_new", "protocol": protocol}
    statements = []
    inserted = False
    with closing(RequestHistory(store.path)) as writer:

        def trace(sql):
            nonlocal inserted
            if len(statements) == 2:
                # After fetchall(), before either COUNT or COMMIT.
                writer.insert(added)
                inserted = True
            statements.append(sql)

        _trace_reader(store, monkeypatch, trace)
        kwargs = {"protocol": protocol, "page": page, "page_size": size}
        response = store.query(**kwargs)
        assert inserted
        _assert_page_first(statements, page, size, count=count)
        assert response == {
            "items": matches[(page - 1) * size : page * size],
            "total": len(matches),
            "page": page,
            "page_size": size,
        }
        # A fresh snapshot sees the write, including any offset shift.
        matches.insert(0, added)
        assert writer.query(**kwargs) == {
            **response,
            "items": matches[(page - 1) * size : page * size],
            "total": len(matches),
        }


def test_count_first_for_other_plans_and_out_of_range_direct_calls(
    history, monkeypatch
):
    store, records = history
    statements = []
    _trace_reader(store, monkeypatch, statements.append)
    for filters in ({}, {"model": "m", "account_id": "a"}, {"result": "success"}):
        statements.clear()
        store.query(**filters, page_size=3)
        assert statements[1].startswith("SELECT COUNT(*)")
        prefix = "SELECT r.payload " if filters.get("result") else "SELECT payload "
        assert statements[2].startswith(prefix)

    matches = [
        r
        for r in reversed(records)
        if r["protocol"] == "openai" and r["result"] != "running"
    ]
    for page, size in [(0, 4), (-1, 4), (1, 0), (4, 0), (1, -1), (2, -1), (-2, -4)]:
        statements.clear()
        response = store.query(protocol="openai", page=page, page_size=size)
        assert statements[1].startswith("SELECT COUNT(*)")
        assert statements[2].startswith("SELECT payload ")
        offset = max(0, (page - 1) * size)
        assert response == {
            "items": matches[offset : None if size < 0 else offset + size],
            "total": len(matches),
            "page": page,
            "page_size": size,
        }
