"""Retention must preserve recoverable in-flight request records."""

from __future__ import annotations

import time

import pytest

from aistudio_api.config import settings
from aistudio_api.infrastructure.request_history import RequestHistory


@pytest.mark.parametrize("old", [False, True])
def test_retention_only_removes_finished_records(tmp_path, monkeypatch, old):
    monkeypatch.setattr(settings, "history_max_rows", 1)
    monkeypatch.setattr(settings, "history_days", 1)
    history = RequestHistory(tmp_path / "requests.sqlite3")
    now = time.time()
    for index in range(4):
        history._last_cleanup = 0
        history.insert(
            {
                "request_id": f"req_{index}",
                "started_at": now - 2 * 86400 if old else now,
                "model": "m",
                "account_id": "a",
                "key_id": "k",
                "result": "running" if index < 3 else "success",
                "status_code": 0,
            }
        )
    assert all(history.get(f"req_{index}") is not None for index in range(3))
    assert history.recover_interrupted() == 3
