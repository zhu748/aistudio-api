"""SQLite request metadata, bounded retention and parameterized queries."""

from __future__ import annotations

import contextlib
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
        # 进程级一次性 schema 初始化：建表/建索引/PRAGMA/chmod 原先每次
        # 连接都重复执行（每请求 ≥2 次 insert，DDL 即使 IF NOT EXISTS 也
        # 需 schema 解析与锁，串行在 self._lock 内放大排队）。
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        # 写路径长连接复用：所有写方法（insert/clear/recover）都持
        # self._lock 串行执行，同一时刻只有一个线程在用这条连接；WAL
        # 模式下读写分离，长连接与原先的每调用 connect/close 语义一致
        # （每连接 PRAGMA 在获取时已设置）。线程池并发读（query/metrics/
        # options/get）继续走各自的临时连接，不受写锁串行影响。
        self._write_conn: sqlite3.Connection | None = None

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
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
            for column in (
                "started_at",
                "model",
                "account_id",
                "key_id",
                "result",
            ):
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS requests_{column} ON requests({column})"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS requests_finished_order ON requests("
                "started_at DESC, request_id DESC) WHERE result != 'running'"
            )
            self.path.chmod(0o600)
            self._schema_ready = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            # journal_mode is persistent; these settings are per connection.
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA secure_delete=ON")
            self._ensure_schema(connection)
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_connect(self) -> Iterator[sqlite3.Connection]:
        """写路径复用长连接（调用方必须已持有 self._lock）。

        每请求 2 次 insert 原先各自 connect+PRAGMA+close（毫秒级系统调用
        开销 × 高并发）；WAL 单写者模型下复用一条连接是等价且标准的做法。
        check_same_thread=False：insert 经 asyncio.to_thread 进入不同工作
        线程，self._lock 已保证同一时刻只有一个线程在用这条连接。
        连接损坏（磁盘错误等）时丢弃重建，不影响正确性。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        conn = self._write_conn
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self._write_conn = conn
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA secure_delete=ON")
            self._ensure_schema(conn)
            with conn:
                yield conn
        except Exception:
            # 连接级故障（文件被外部替换/磁盘错误）：丢弃坏连接，下次重建。
            self._write_conn = None
            with contextlib.suppress(Exception):
                conn.close()
            raise

    def insert(self, record: dict[str, object]) -> None:
        with self._lock, self._write_connect() as connection:
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
                    "DELETE FROM requests WHERE result != 'running' AND started_at < ?",
                    (now - settings.history_days * 86400,),
                )
                connection.execute(
                    "DELETE FROM requests WHERE request_id IN ("
                    "SELECT request_id FROM requests WHERE result != 'running' "
                    "ORDER BY started_at DESC, request_id DESC LIMIT -1 OFFSET ?)",
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
        # 只读查询不持进程级写锁（与 metrics 的 WAL 并发读语义一致）：
        # 大范围搜索（COUNT + json_extract LIKE 全表扫描）期间持锁会阻塞
        # 所有请求的 history insert；_ensure_schema 自带 _schema_lock。
        # COUNT 与 SELECT 包进同一显式事务：两条独立 SELECT 各自快照，
        # 并发 insert/清理期间 total 与本页 items 会漂移（分页观感错乱）。
        with self._connect() as connection:
            connection.execute("BEGIN DEFERRED")
            try:
                total = connection.execute(
                    "SELECT COUNT(*) FROM requests" + where, params
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT payload FROM requests"
                    + where
                    + " ORDER BY started_at DESC, request_id DESC LIMIT ? OFFSET ?",
                    [*params, page_size, (page - 1) * page_size],
                ).fetchall()
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    connection.rollback()
                raise
            connection.execute("COMMIT")
        return {
            "items": [json.loads(row[0]) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get(self, request_id: str) -> dict[str, object] | None:
        # 单行主键查询持锁开销可忽略，且锁原先提供了「请求起始记录
        # insert → 面板查询」的时序保证（running 请求立即可查），
        # 去锁会让并发读者看到 insert 提交前的空窗。
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def clear(self) -> int:
        with self._lock, self._write_connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE result != 'running'"
            ).fetchone()[0]
            connection.execute("DELETE FROM requests WHERE result != 'running'")
        return count

    def recover_interrupted(self) -> int:
        """Called once at server startup, before accepting new requests."""
        with self._lock, self._write_connect() as connection:
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
        # A separate WAL reader can aggregate without blocking request inserts
        # behind the process-wide write lock. Consume rows lazily, preserving
        # exact percentile samples without materializing two full row lists.
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT model, account_id, key_id, result, started_at, "
                + columns
                + ", json_array_length(payload, '$.attempts') AS attempts FROM requests WHERE "
                + " AND ".join(clauses),
                params,
            )
            return summarize((dict(row) for row in rows), since=since, until=until)

    def options(self) -> dict[str, object]:
        # 只读，不持写锁（同 query）
        with self._connect() as connection:
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
