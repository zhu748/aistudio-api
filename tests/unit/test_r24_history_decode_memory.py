"""History pages release consumed SQLite payloads without changing their contents."""

import gc
import json
import weakref
from contextlib import closing, contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from aistudio_api.api import routes_operations
from aistudio_api.infrastructure import request_history as history_module


@pytest.fixture
def history(tmp_path):
    with closing(history_module.RequestHistory(tmp_path / "history.sqlite3")) as store:
        yield store


def _seed(history, *payloads: str) -> None:
    # Seed the stored text directly, including legacy non-object or malformed rows.
    with history._connect() as connection:
        connection.executemany(
            "INSERT INTO requests (request_id, started_at, model, account_id, key_id, "
            "result, status_code, payload) VALUES (?, ?, '', '', '', 'success', 200, ?)",
            ((f"req_{i}", i, payload) for i, payload in enumerate(payloads)),
        )


@pytest.fixture
async def client(history, monkeypatch):
    monkeypatch.setattr(routes_operations, "request_history", lambda: history)
    app = FastAPI()
    app.include_router(routes_operations.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
        base_url="http://fixture.invalid",
    ) as client:
        yield client


def test_raw_payloads_are_released_between_decodes_after_reader_closes(
    history, monkeypatch
):
    _seed(history, "NaN", "[Infinity,-Infinity,-0.0]", '{"future":"雪"}')
    original_connect = history._connect
    events = []

    class RawPayload(str):
        pass

    @contextmanager
    def observed_connect():
        with original_connect() as db:
            db.text_factory = lambda raw: RawPayload(raw.decode())
            db.set_trace_callback(events.append)
            yield db
        events.append("closed")

    seen: list[weakref.ReferenceType[RawPayload]] = []

    def decode(payload):
        assert events[-2:] == ["COMMIT", "closed"]
        gc.collect()
        assert all(ref() is None for ref in seen), "consumed payload still live"
        seen.append(weakref.ref(payload))
        return json.loads(payload)

    monkeypatch.setattr(history, "_connect", observed_connect)
    monkeypatch.setattr(history_module, "json", SimpleNamespace(loads=decode))
    page = history.query()
    assert len(seen) == 3
    gc.collect()
    assert all(ref() is None for ref in seen)
    assert json.dumps(page["items"], ensure_ascii=False) == (
        '[{"future": "雪"}, [Infinity, -Infinity, -0.0], NaN]'
    )


async def test_export_and_dashboard_preserve_scalar_and_nested_bytes(history, client):
    _seed(
        history, "42", '"雪🙂"', "null", "[false,-0.0]", '{ "future": {"z":1,"a":2} }'
    )
    for url, expected in [
        (
            "/api/requests/export",
            (
                '{"items":[{"future":{"z":1,"a":2}},[false,-0.0],null,"雪🙂",42],'
                '"total":5,"page":1,"page_size":5000}'
            ),
        ),
        (
            "/api/requests?page=2&page_size=2",
            '{"items":[null,"雪🙂"],"total":5,"page":2,"page_size":2}',
        ),
        (
            "/api/requests?page=4&page_size=2",
            '{"items":[],"total":5,"page":4,"page_size":2}',
        ),
    ]:
        response = await client.get(url)
        assert response.status_code == 200
        assert response.content == expected.encode()
        assert response.headers["content-type"] == "application/json"
        assert int(response.headers["content-length"]) == len(response.content)


async def test_malformed_row_fails_without_returning_a_partial_page(history, client):
    malformed = '{"truncated":'
    _seed(history, "also invalid", malformed, "123")
    with pytest.raises(json.JSONDecodeError) as exc:
        history.query()
    assert (exc.value.doc, exc.value.pos) == (malformed, len(malformed))
    response = await client.get("/api/requests/export")
    assert (response.status_code, response.content) == (500, b"Internal Server Error")
