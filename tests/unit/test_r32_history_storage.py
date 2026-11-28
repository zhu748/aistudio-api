"""Writer-local WAL settings and exact clear counts across independent writers."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from unittest.mock import patch

import pytest

from aistudio_api.infrastructure.request_history import RequestHistory


@pytest.mark.parametrize("previous", ["reader", "closed-writer", "failed-setup"])
def test_new_writer_has_its_own_journal_limit(tmp_path, previous):
    path = tmp_path / "history.sqlite3"
    with closing(RequestHistory(path)) as history:
        if previous == "reader":
            assert history.query()["total"] == 0
        elif previous == "closed-writer":
            assert history.clear() == 0
            history.close()
        else:
            with closing(sqlite3.connect(path)) as failed:

                def deny_pragma(action, name, *_args):
                    if action == sqlite3.SQLITE_PRAGMA and name == "secure_delete":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                failed.set_authorizer(deny_pragma)
                with (
                    patch.object(sqlite3, "connect", return_value=failed),
                    pytest.raises(sqlite3.DatabaseError),
                ):
                    history.clear()
                assert history._write_conn is None
                with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                    failed.execute("SELECT 1")

        # Query the actual cached writer: inspecting a fresh connection would
        # only tell us SQLite's default setting.
        assert history.clear() == 0
        writer = history._write_conn
        assert writer is not None
        assert writer.execute("PRAGMA journal_size_limit").fetchone() == (10 * 1024**2,)


def test_clear_counts_the_rows_deleted_with_an_independent_writer(tmp_path):
    path = tmp_path / "history.sqlite3"
    record: dict[str, object] = {
        "request_id": "finished",
        "started_at": time.time(),
        "model": "m",
        "account_id": "a",
        "key_id": "k",
        "result": "success",
        "status_code": 200,
    }
    running = {**record, "request_id": "active", "result": "running", "status_code": 0}
    with (
        closing(RequestHistory(path)) as history,
        closing(RequestHistory(path)) as other,
    ):
        other.insert(record)
        history.insert(running)
        connection = history._write_conn
        assert connection is not None

        def finish_before_delete(sql):
            # Commits after any pre-count but before the DELETE acquires its lock.
            if sql.startswith("DELETE FROM requests"):
                other.insert({**record, "request_id": "also-finished"})

        connection.set_trace_callback(finish_before_delete)
        removed = history.clear()
        connection.set_trace_callback(None)
        assert removed == 2
        assert history.query()["total"] == 0
        assert history.get("active") == running
        assert history.clear() == 0
