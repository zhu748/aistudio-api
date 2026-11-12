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
        protocol: str = "",
        retried: bool = False,
        min_elapsed_ms: float = 0,
    ) -> dict[str, object]:
        clauses: list[str] = ["result != 'running'"]
        params: list[object] = []
        for column, value in (
            ("model", model),
            ("account_id", account_id),
            ("key_id", key_id),
            ("result", result),
        ):
            if value:
                empty_filters = {
                    "model": "__unknown__",
                    "account_id": "__unassigned__",
                    "key_id": "__anonymous__",
                }
                if value == empty_filters.get(column):
                    clauses.append(f"{column} = ''")
                elif column == "result" and value == "failed":
                    clauses.append("result != 'success'")
                elif column == "model":
                    clauses.append("(model = ? OR model = ?)")
                    clean = value.removeprefix("models/")
                    params.extend([clean, "models/" + clean])
                else:
                    clauses.append(f"{column} = ?")
                    params.append(value)
        for operator, value in ((">=", since), ("<=", until)):
            if value:
                clauses.append(f"started_at {operator} ?")
                params.append(value)
        if search:
            clauses.append(
                "(request_id LIKE ? OR model LIKE ? OR account_id LIKE ? OR json_extract(payload, '$.account_name') LIKE ? OR key_id LIKE ?)"
            )
            params.extend([f"%{search}%"] * 5)
        if protocol:
            clauses.append("json_extract(payload, '$.protocol') = ?")
            params.append(protocol)
        if retried:
            clauses.append("json_array_length(payload, '$.attempts') > 1")
        if min_elapsed_ms:
            clauses.append("json_extract(payload, '$.elapsed_ms') >= ?")
            params.append(min_elapsed_ms)
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
            count = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE result != 'running'"
            ).fetchone()[0]
            connection.execute("DELETE FROM requests WHERE result != 'running'")
        return count

    def recover_interrupted(self) -> int:
        """Called once at server startup, before accepting new requests."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id, payload FROM requests WHERE result = 'running'"
            ).fetchall()
            for request_id, payload in rows:
                record = json.loads(payload)
                record.update(
                    result="interrupted",
                    stage="finished",
                    error="服务重启前调用未完成",
                    status_code=0,
                    elapsed_ms=None,
                    first_byte_ms=None,
                )
                connection.execute(
                    "UPDATE requests SET result = 'interrupted', payload = ? WHERE request_id = ?",
                    (json.dumps(record, ensure_ascii=False), request_id),
                )
        return len(rows)

    def metrics(
        self, *, since: float, until: float, model: str = "", account_id: str = ""
    ) -> dict[str, object]:
        from aistudio_api.infrastructure.request_metrics import summarize

        fields = {
            "elapsed_ms": "$.elapsed_ms",
            "first_byte_ms": "$.first_byte_ms",
            "account_name": "$.account_name",
            "prompt_tokens": "$.usage.prompt_tokens",
            "completion_tokens": "$.usage.completion_tokens",
            "total_tokens": "$.usage.total_tokens",
            "reasoning_tokens": "$.usage.completion_tokens_details.reasoning_tokens",
            "cached_tokens": "$.usage.cached_tokens",
            "finish_reason": "$.finish_reason",
        }
        columns = ", ".join(
            f"json_extract(payload, '{value}') AS {key}"
            for key, value in fields.items()
        )
        clauses = ["started_at >= ?", "started_at <= ?", "result != 'running'"]
        params: list[object] = [since, until]
        if model:
            clauses.append("(model = ? OR model = ?)")
            clean = model.removeprefix("models/")
            params.extend([clean, "models/" + clean])
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT model, account_id, key_id, result, started_at, "
                + columns
                + ", json_array_length(payload, '$.attempts') AS attempts FROM requests WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchall()
        return summarize([dict(row) for row in rows], since=since, until=until)

    def options(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            return {
                "models": [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT model FROM requests WHERE model != '' ORDER BY model"
                    )
                ],
                "keys": [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT key_id FROM requests WHERE key_id != '' ORDER BY key_id"
                    )
                ],
                "history_days": settings.history_days,
                "enabled": settings.request_history,
            }


@lru_cache(maxsize=8)
def _history_at(path: Path) -> RequestHistory:
    return RequestHistory(path)


def request_history() -> RequestHistory:
    return _history_at(resolve_data_dir() / "requests.sqlite3")
