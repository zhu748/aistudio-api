"""SQLite request metadata, bounded retention and parameterized queries."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from aistudio_api.config import resolve_data_dir, settings


class RequestHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA journal_size_limit=10485760")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS requests ("
            "request_id TEXT PRIMARY KEY, started_at REAL NOT NULL, "
            "model TEXT NOT NULL, account_id TEXT NOT NULL, key_id TEXT NOT NULL, "
            "result TEXT NOT NULL, status_code INTEGER NOT NULL, payload TEXT NOT NULL)"
        )
        for column in ("started_at", "model", "account_id", "key_id", "result"):
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS requests_{column} ON requests({column})"
            )
        self.path.chmod(0o600)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def insert(self, record: dict[str, object]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["request_id"],
                    record["started_at"],
                    record["model"],
                    record["account_id"],
                    record["key_id"],
                    record["result"],
                    record["status_code"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            now = time.time()
            if now - self._last_cleanup > 60:
                connection.execute(
                    "DELETE FROM requests WHERE started_at < ?",
                    (now - settings.history_days * 86400,),
                )
                connection.execute(
                    "DELETE FROM requests WHERE request_id IN ("
                    "SELECT request_id FROM requests ORDER BY started_at DESC LIMIT -1 OFFSET ?)",
                    (settings.history_max_rows,),
                )
                self._last_cleanup = now

    def query(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        model: str = "",
        account_id: str = "",
        key_id: str = "",
        result: str = "",
        search: str = "",
        since: float = 0,
        until: float = 0,
    ) -> dict[str, object]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("model", model),
            ("account_id", account_id),
            ("key_id", key_id),
            ("result", result),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        for operator, value in ((">=", since), ("<=", until)):
            if value:
                clauses.append(f"started_at {operator} ?")
                params.append(value)
        if search:
            clauses.append("(request_id LIKE ? OR model LIKE ? OR account_id LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM requests" + where, params
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT payload FROM requests"
                + where
                + " ORDER BY started_at DESC, request_id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get(self, request_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def clear(self) -> int:
        with self._lock, self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            connection.execute("DELETE FROM requests")
        return count


@lru_cache(maxsize=8)
def _history_at(path: Path) -> RequestHistory:
    return RequestHistory(path)


def request_history() -> RequestHistory:
    return _history_at(resolve_data_dir() / "requests.sqlite3")
