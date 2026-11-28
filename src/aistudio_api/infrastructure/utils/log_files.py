"""Rotating JSONL logs written by a bounded background queue."""

from __future__ import annotations

import heapq
import json
import logging
import logging.handlers
import math
import queue
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

from aistudio_api.config import resolve_data_dir
from aistudio_api.infrastructure.utils.redaction import redact_record

_listener: logging.handlers.QueueListener | None = None
_queue_handler: logging.handlers.QueueHandler | None = None
_file_handler: logging.handlers.RotatingFileHandler | None = None


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        redact_record(record)
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "ts_ms": int(record.created * 1000),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()[:8192],
            "request_id": getattr(record, "request_id", ""),
            **{
                key: getattr(record, key, "system" if key == "category" else "")
                for key in ("category", "model", "account_id", "stage")
            },
        }
        sig_meta = getattr(record, "sig_meta", None)
        if isinstance(sig_meta, dict):
            payload["sig_meta"] = sig_meta
        return json.dumps(payload, ensure_ascii=False)


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


def _backup_count() -> int:
    """当前轮转文件保留的备份份数（未启动文件日志时退回 5）。"""
    handler = _file_handler
    count = getattr(handler, "backupCount", 5)
    return count if isinstance(count, int) and count > 0 else 5


def _timestamp_ms(raw: object) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, (float, str)):
        try:
            return int(raw)
        except (ValueError, OverflowError):
            # Older/external writers may have used decimal strings.
            try:
                return int(float(raw))
            except (ValueError, OverflowError):
                pass
    return 0


def _finite_float(value: str) -> float | None:
    number = float(value)
    return number if math.isfinite(number) else None


def _decode_log_line(
    decoder: json.JSONDecoder, line: bytes
) -> dict[str, object] | None:
    try:
        item: object = decoder.decode(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return item if isinstance(item, dict) else None


def read_log_history(
    *,
    level: str = "INFO",
    search: str = "",
    request_id: str = "",
    page: int = 1,
    page_size: int = 100,
    category: str = "all",
    model: str = "",
    account_id: str = "",
    order: str = "desc",
) -> dict[str, object]:
    from aistudio_api.infrastructure.utils.logger import matches_log_entry

    folder = log_directory()
    offset = max(0, (page - 1) * page_size)
    end = page * page_size
    direction = -1 if order == "asc" else 1
    # Keep only rank and byte locators, not decoded dicts, even for deep pages.
    prefix: list[tuple[int, int, int, int]] = []
    total = 0
    items: list[dict[str, object]] = []
    # Legacy non-finite numbers cannot be sent in an application/json response.
    decoder = json.JSONDecoder(parse_float=_finite_float, parse_constant=lambda _: None)

    # Keep the original descriptors for page reads in case rotation renames files.
    with ExitStack() as archive:
        streams: list[BinaryIO] = []
        for i in range(_backup_count(), -1, -1):
            path = folder / (f"runtime.jsonl.{i}" if i else "runtime.jsonl")
            if not path.is_file():
                continue
            try:
                streams.append(archive.enter_context(path.open("rb")))
            except (FileNotFoundError, IsADirectoryError):
                continue

        # Queued writers can deliver timestamps out of order even across rotation.
        # The physical file and byte position break timestamp ties.
        for i, stream in enumerate(streams):
            file_rank = direction * i
            byte_offset = 0
            for line in stream:
                start = byte_offset
                length = len(line)
                byte_offset += length
                item = _decode_log_line(decoder, line)
                if item is None or not matches_log_entry(
                    item,
                    min_level=level,
                    search=search,
                    request_id=request_id,
                    category=category,
                    model=model,
                    account_id=account_id,
                ):
                    continue
                total += 1
                rank = (
                    direction * _timestamp_ms(item.get("ts_ms")),
                    file_rank,
                    direction * start,
                    length,
                )
                # Keep the best prefix; never allocate by the supplied page number.
                if len(prefix) < end:
                    heapq.heappush(prefix, rank)
                else:
                    heapq.heappushpop(prefix, rank)

        # The requested page is the tail of this prefix, cheapest to pop in reverse.
        while len(prefix) > offset:
            _, file_rank, byte_rank, length = heapq.heappop(prefix)
            stream = streams[direction * file_rank]
            stream.seek(direction * byte_rank)
            line = stream.read(length)
            item = _decode_log_line(decoder, line) if len(line) == length else None
            if item is not None:
                items.append(item)

    items.reverse()
    return {
        "items": items,
        "total": total,
        "total_truncated": False,
        "page": page,
        "page_size": page_size,
        "dropped": getattr(_queue_handler, "dropped", 0),
    }
