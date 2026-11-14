"""WAL readers preserve their snapshot while writers continue independently."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import chain
from typing import Any, cast
from unittest.mock import Mock

import pytest

from aistudio_api.infrastructure import request_history as module, request_metrics


def test_metrics_reader_does_not_block_writes_and_keeps_a_consistent_snapshot(
    tmp_path, monkeypatch
):
    history = module.RequestHistory(tmp_path / "requests.sqlite3")
    now = time.time()
    record: dict[str, object] = {
        "request_id": "first",
        "started_at": now,
        "model": "m",
        "account_id": "a",
        "key_id": "k",
        "result": "success",
        "status_code": 200,
    }
    history.insert(record)
    entered, release = threading.Event(), threading.Event()
    summarize = request_metrics.summarize

    def paused(rows, *, since, until):
        iterator = iter(rows)
        first = next(iterator)
        entered.set()
        assert release.wait(5)
        return summarize(chain([first], iterator), since=since, until=until)

    monkeypatch.setattr(request_metrics, "summarize", paused)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(history.metrics, since=now - 1, until=now + 1)
        assert entered.wait(5)
        try:
            writer = executor.submit(history.insert, {**record, "request_id": "second"})
            writer.result(timeout=2)
        finally:
            release.set()
        metrics = cast(dict[str, Any], reader.result(timeout=5))
    assert metrics["totals"]["requests"] == 1
    assert history.query()["total"] == 2


def test_schema_failure_still_closes_connection(tmp_path, monkeypatch):
    connection = Mock()
    monkeypatch.setattr(module.sqlite3, "connect", Mock(return_value=connection))
    history = module.RequestHistory(tmp_path / "requests.sqlite3")
    monkeypatch.setattr(
        history, "_ensure_schema", Mock(side_effect=RuntimeError("schema failed"))
    )
    with pytest.raises(RuntimeError, match="schema failed"), history._connect():
        pytest.fail("connection should not be yielded")
    connection.close.assert_called_once()
