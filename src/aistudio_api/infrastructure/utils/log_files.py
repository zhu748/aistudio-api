"""Rotating JSONL logs written by a bounded background queue."""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
from pathlib import Path

from aistudio_api.config import resolve_data_dir
from aistudio_api.infrastructure.request_context import log_context
from aistudio_api.infrastructure.utils.redaction import redact_text

_listener: logging.handlers.QueueListener | None = None
_queue_handler: logging.handlers.QueueHandler | None = None
_file_handler: logging.handlers.RotatingFileHandler | None = None


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in log_context().items():
            setattr(record, key, getattr(record, key, "") or value)
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
                **{
                    key: getattr(record, key, "system" if key == "category" else "")
                    for key in ("category", "model", "account_id", "stage")
                },
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
    category: str = "all",
    model: str = "",
    account_id: str = "",
    order: str = "desc",
) -> dict[str, object]:
    from aistudio_api.infrastructure.utils.logger import matches_log_entry

    matched: list[dict[str, object]] = []
    folder = log_directory()
    # desc（默认面板视图）时提前终止：文件按时间从新到旧排列
    # （当前文件最新，备份序号越大越旧），凑满 offset+page_size 后
    # 更旧文件不可能影响本页内容，可停止读取（最多 6×5MB 全量读盘
    # -> 通常只读当前文件的一小段）。
    paths = [
        folder / "runtime.jsonl",
        *[folder / f"runtime.jsonl.{i}" for i in range(1, 6)],
    ]
    # desc（默认）：当前文件 -> .1 -> .2 ...（每文件内部行本身时间递增，
    # 文件间当前文件整体最新）；凑满 offset+page_size 即可停止读更旧文件
    early_stop = order != "asc"
    needed = page * page_size
    overflow_seen: int | None = None  # total 只能给出下界时用它提示截断

    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or not matches_log_entry(
                    item,
                    min_level=level,
                    search=search,
                    request_id=request_id,
                    category=category,
                    model=model,
                    account_id=account_id,
                ):
                    continue
                matched.append(item)
        if early_stop and len(matched) >= needed:
            # 已凑满到请求页：更旧文件不可能影响本页内容，可安全停止。
            # total 退化为下界（实际值 >= 返回值），用 overflow 标记提示。
            overflow_seen = len(matched)
            break
    matched.sort(key=lambda item: int(str(item["ts_ms"])), reverse=order != "asc")
    offset = (page - 1) * page_size
    return {
        "items": matched[offset : offset + page_size],
        # early-stop 时 total 是下界：面板据此显示 "N+" 提示还有更旧记录
        "total": len(matched),
        "total_truncated": overflow_seen is not None,
        "page": page,
        "page_size": page_size,
        "dropped": getattr(_queue_handler, "dropped", 0),
    }
