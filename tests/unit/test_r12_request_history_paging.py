"""Result-only history pages keep payload work bounded and read one WAL snapshot."""

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
    with closing(RequestHistory(tmp_path / "requests.sqlite3")) as store:
        yield store


def _record(i: int, started_at: float, result: str = "success") -> dict[str, object]:
    return {
        "request_id": f"req_{i:03}",
        "started_at": started_at,
        "model": "m",
        "account_id": "a",
        "key_id": "k",
        "result": result,
        "status_code": 200 if result == "success" else 0,
        "protocol": "openai" if i % 2 else "gemini",
        "events": [{"n": i, "note": "测试"}],
    }


def _on_read_connection(history, monkeypatch, setup):
    original = history._connect

    @contextmanager
    def connect():
        with original() as db:
            setup(db)
            yield db

    monkeypatch.setattr(history, "_connect", connect)


async def test_dashboard_result_pages_match_complete_ordered_records(
    history, monkeypatch
):
    now = time.time()
    statuses = ("success", "timeout", "success", "running", "success", "error")
    records = [
        _record(i, now + i // 11, statuses[i % len(statuses)]) for i in range(135)
    ]
    records.append(_record(135, now + 12, "interrupted"))  # Entire group fits one page.
    # Ties span page boundaries, and insertion order is not request ID order.
    for record in chain(records[1::2], records[::2]):
        history.insert(record)
    finished = [r for r in reversed(records) if r["result"] != "running"]
    success = [r for r in finished if r["result"] == "success"]

    monkeypatch.setattr(routes_operations, "request_history", lambda: history)
    app = FastAPI()
    app.include_router(routes_operations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        size = 5
        for filters, expected in [
            ({"result": "success"}, success),
            ({"result": "error"}, [r for r in finished if r["result"] == "error"]),
            ({"result": "interrupted"}, [records[-1]]),
            ({"result": "missing"}, []),
            ({"result": "running"}, []),
            ({"result": "failed"}, [r for r in finished if r["result"] != "success"]),
            (
                {"result": "success", "protocol": "openai"},
                [r for r in success if r["protocol"] == "openai"],
            ),
            ({}, finished),
        ]:
            for page in (1, 2, 13, 100):
                response = await client.get(
                    "/api/requests",
                    params={"page": page, "page_size": size, **filters},
                )
                assert response.status_code == 200
                assert response.json() == {
                    "items": expected[(page - 1) * size : page * size],
                    "total": len(expected),
                    "page": page,
                    "page_size": size,
                }


def test_exact_result_materializes_only_the_returned_payloads(history, monkeypatch):
    now = time.time()
    records = [_record(i, now + i) for i in range(80)]
    for record in records:
        history.insert(record)
    payloads_read = 0

    def observe(payload):
        nonlocal payloads_read
        payloads_read += 1
        return payload

    def instrument(db):
        # Count actual payload evaluations without relying on timings, index
        # names, or a specific SQLite version's EXPLAIN output.
        db.create_function("observe", 1, observe)
        db.execute(
            "CREATE TEMP VIEW requests AS SELECT rowid AS rowid, request_id, "
            "started_at, result, observe(payload) AS payload FROM main.requests"
        )

    _on_read_connection(history, monkeypatch, instrument)
    for page in (12, 30):
        expected = records[::-1][(page - 1) * 5 : page * 5]
        payloads_read = 0
        assert (
            history.query(result="success", page=page, page_size=5)["items"] == expected
        )
        assert payloads_read == len(expected)


def test_commit_between_count_and_page_does_not_shift_the_current_page(
    history, monkeypatch
):
    now = time.time()
    records = [_record(i, now) for i in range(9)]
    for record in records:
        history.insert(record)
    added = _record(9, now + 1)
    after_count = inserted = False

    with closing(RequestHistory(history.path)) as writer:

        def trace(sql):
            nonlocal after_count, inserted
            if after_count:
                # This is a separate WAL connection. The count has been fetched;
                # commit just before the reader executes its next statement.
                after_count = False
                writer.insert(added)
                inserted = True
            elif not inserted and sql.startswith("SELECT COUNT("):
                after_count = True

        _on_read_connection(
            history, monkeypatch, lambda db: db.set_trace_callback(trace)
        )
        before = history.query(result="success", page=2, page_size=3)
        assert inserted
        assert (before["total"], before["items"]) == (9, records[5:2:-1])
        after = history.query(result="success", page=2, page_size=3)
        assert (after["total"], after["items"]) == (10, records[6:3:-1])
