"""Rotating JSONL logs written by a bounded background queue."""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
from pathlib import Path

from aistudio_api.config import resolve_data_dir
from aistudio_api.infrastructure.request_context import current_request
from aistudio_api.infrastructure.utils.redaction import redact_text

_listener: logging.handlers.QueueListener | None = None
_queue_handler: logging.handlers.QueueHandler | None = None
_file_handler: logging.handlers.RotatingFileHandler | None = None


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = current_request.get()
        record.request_id = getattr(record, "request_id", "") or (
            context.request_id if context else ""
        )
        record.msg, record.args = redact_text(record.getMessage()), ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                "ts_ms": int(record.created * 1000),
                "level": record.levelname,
                "name": record.name,
                "message": redact_text(record.getMessage())[:8192],
                "request_id": getattr(record, "request_id", ""),
            },
            ensure_ascii=False,
        )


class BoundedQueueHandler(logging.handlers.QueueHandler):
    dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


class PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        Path(self.baseFilename).chmod(0o600)
        return stream


def log_directory() -> Path:
    return resolve_data_dir() / "logs"


def start_file_logging() -> None:
    global _listener, _queue_handler, _file_handler
    if _listener is not None:
        return
    folder = log_directory()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "runtime.jsonl"
    _file_handler = PrivateRotatingHandler(
        target, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    target.chmod(0o600)
    _file_handler.setFormatter(JsonFormatter())
    _queue_handler = BoundedQueueHandler(queue.Queue(maxsize=10000))
    _queue_handler.addFilter(ContextFilter())
    logging.getLogger().addHandler(_queue_handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).addHandler(_queue_handler)
    _listener = logging.handlers.QueueListener(_queue_handler.queue, _file_handler)
    _listener.start()


def stop_file_logging() -> None:
    global _listener, _queue_handler, _file_handler
    if _queue_handler:
        for name in (
            "",
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "httpx",
            "httpcore",
        ):
            logging.getLogger(name).removeHandler(_queue_handler)
    if _listener:
        # QueueListener.stop uses put_nowait; reserve room for its sentinel.
        if _queue_handler and isinstance(_queue_handler.queue, queue.Queue):
            _queue_handler.queue.join()
        _listener.stop()
    if _file_handler:
        _file_handler.close()
    _listener = _queue_handler = _file_handler = None


def read_log_history(
    *,
    level: str = "INFO",
    search: str = "",
    request_id: str = "",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, object]:
    minimum = getattr(logging, level, logging.INFO)
    matched: list[dict[str, object]] = []
    folder = log_directory()
    paths = [
        folder / "runtime.jsonl",
        *[folder / f"runtime.jsonl.{i}" for i in range(1, 6)],
    ]
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    getattr(logging, item.get("level", "INFO"), logging.INFO) < minimum
                    or (request_id and item.get("request_id") != request_id)
                    or (search and search.lower() not in str(item).lower())
                ):
                    continue
                matched.append(item)
    matched.sort(key=lambda item: int(str(item["ts_ms"])), reverse=True)
    offset = (page - 1) * page_size
    return {
        "items": matched[offset : offset + page_size],
        "total": len(matched),
        "page": page,
        "page_size": page_size,
        "dropped": getattr(_queue_handler, "dropped", 0),
    }
