"""Unified logging subsystem and request dump instrumentation.

Provides standardized log formatting, dynamic log level adjustments,
and full-payload request dumping controlled via the DEBUG environment
variable or web configuration.

Additionally mirrors every log record into an in-memory ring buffer so the
web console can expose runtime logs (including upstream errors) via
``GET /api/logs`` — the operator should never face an "empty reply with
no clue" situation again. New entries are also broadcast to registered
subscribers so the web panel can stream them live via
``GET /api/logs/stream`` (Server-Sent Events).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from aistudio_api.infrastructure.request_context import current_request
from aistudio_api.infrastructure.utils.common import atomic_write_json
from aistudio_api.infrastructure.utils.redaction import redact_text

# ---------------------------------------------------------------------------
# 内存日志环形缓冲（供 Web 面板 /api/logs 查询运行日志与报错）
# ---------------------------------------------------------------------------

_LOG_RING_CAPACITY = 2000
_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

_log_ring: deque[dict[str, object]] = deque(maxlen=_LOG_RING_CAPACITY)
# 保护环形缓冲的并发访问：日志可能在任意线程产生（FastAPI 线程池、WAA
# daemon 线程等），而面板查询/SSE 快照在事件循环线程读取。deque 在迭代
# 期间被其他线程 append 会抛 RuntimeError，必须用锁串行化读写。
_log_ring_lock = threading.Lock()


class RingBufferHandler(logging.Handler):
    """把日志记录同步镜像进内存环形缓冲，供面板实时查看。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact_text(record.getMessage())
            if record.exc_info:
                exc_text = self._format_exc_tail(record)
                if exc_text:
                    message = f"{message}\n{exc_text}"
            message = redact_text(message)
            # 进环形缓冲前截断（L7）：开启 dump 时单条转储约 8KB，日志环
            # 容量固定且逐条 SSE 广播，超长消息会成倍放大内存占用与广播
            # 流量；截断到 2KB 保留头部足以定位问题。
            if len(message) > 2048:
                message = message[:2048] + f"\n...[已截断，原长 {len(message)} 字符]"
            entry: dict[str, object] = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ts_ms": int(record.created * 1000),
                "level": record.levelname,
                "name": record.name,
                "message": message,
                "request_id": getattr(record, "request_id", ""),
            }
            with _log_ring_lock:
                _log_ring.append(entry)
            # 同步广播给实时订阅者（面板 SSE 日志流），与环形缓冲共享同一对象。
            # 放在锁外：订阅回调只做 call_soon_threadsafe 入队，但为避免任何
            # 潜在的慢回调阻塞日志主链路，不与缓冲写入互斥。
            _publish_log_entry(entry)
        except Exception:
            self.handleError(record)

    @staticmethod
    def _format_exc_tail(record: logging.LogRecord) -> str:
        import traceback

        exc = record.exc_info[1] if record.exc_info else None
        if exc is None:
            return ""
        try:
            return "".join(traceback.format_exception(exc, limit=4)).strip()
        except Exception:
            return ""


def get_recent_logs(
    *,
    min_level: str = "INFO",
    limit: int = 300,
    search: str | None = None,
    order: str = "desc",
) -> list[dict[str, object]]:
    """读取环形缓冲中的最近日志，支持级别过滤与关键字检索。

    ``order='desc'`` 时最新日志排在最前（面板默认），``asc`` 按时间正序。
    """
    min_no = _LEVEL_ORDER.get(str(min_level).strip().upper(), logging.INFO)
    needle = (search or "").strip().lower()
    # 锁内快照：避免迭代期间其他线程 append 触发
    # "deque mutated during iteration" RuntimeError
    with _log_ring_lock:
        snapshot = list(_log_ring)
    matched: list[dict[str, object]] = []
    for entry in snapshot:
        if _LEVEL_ORDER.get(str(entry.get("level")), logging.INFO) < min_no:
            continue
        if needle:
            haystack = (
                f"{entry.get('message', '')} {entry.get('name', '')} "
                f"{entry.get('level', '')}"
            ).lower()
            if needle not in haystack:
                continue
        matched.append(entry)
    if str(order).strip().lower() == "asc":
        return matched[-limit:]
    return list(reversed(matched))[:limit]


def clear_recent_logs() -> int:
    """清空内存日志缓冲，返回清空前条数。"""
    with _log_ring_lock:
        count = len(_log_ring)
        _log_ring.clear()
    return count


def get_log_buffer_stats() -> dict[str, int]:
    """返回日志环形缓冲的容量与当前条数。"""
    with _log_ring_lock:
        return {"capacity": _LOG_RING_CAPACITY, "size": len(_log_ring)}


# ---------------------------------------------------------------------------
# 日志实时订阅（供 Web 面板 /api/logs/stream SSE 增量推送）
# ---------------------------------------------------------------------------

_LogSubscriber = Callable[[dict[str, object]], None]
_log_subscribers: dict[int, _LogSubscriber] = {}
_log_subscriber_lock = threading.Lock()
_log_subscriber_seq = 0


def subscribe_log_entries(callback: _LogSubscriber) -> int:
    """注册实时日志订阅回调，返回可用于取消的订阅句柄。

    回调会在产生日志的线程中被同步调用，订阅方需自行保证线程安全
    （例如通过 ``loop.call_soon_threadsafe`` 把条目投递到自己的事件循环）。
    """
    global _log_subscriber_seq
    with _log_subscriber_lock:
        _log_subscriber_seq += 1
        handle: int = _log_subscriber_seq
        _log_subscribers[handle] = callback
    return handle


def unsubscribe_log_entries(handle: int) -> None:
    """按句柄取消实时日志订阅（句柄不存在时静默忽略）。"""
    with _log_subscriber_lock:
        _log_subscribers.pop(handle, None)


def get_log_subscriber_count() -> int:
    """返回当前活跃的实时日志订阅连接数。"""
    with _log_subscriber_lock:
        return len(_log_subscribers)


def _publish_log_entry(entry: dict[str, object]) -> None:
    """把新日志条目广播给所有订阅者；订阅方异常绝不影响日志主链路。"""
    if not _log_subscribers:
        return
    with _log_subscriber_lock:
        callbacks = tuple(_log_subscribers.values())
    for callback in callbacks:
        with contextlib.suppress(Exception):
            # 订阅方异常绝不影响日志主链路，也不影响其他订阅者
            callback(entry)


def passes_min_level(level_name: str, min_level: str = "INFO") -> bool:
    """判断日志级别是否不低于给定最低级别（供查询与流式推送共用）。"""
    level_no = _LEVEL_ORDER.get(str(level_name).strip().upper())
    min_no = _LEVEL_ORDER.get(str(min_level).strip().upper(), logging.INFO)
    if level_no is None:
        level_no = logging.INFO
    return level_no >= min_no


_UNIFIED_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
_UNIFIED_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ANSI Color codes for TTY output
_COLORS = {
    "DEBUG": "\033[36m",  # Cyan
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[1;31m",  # Bold Red
}
_RESET = "\033[0m"


class UnifiedFormatter(logging.Formatter):
    """Unified formatter supporting optional ANSI colors on TTY streams."""

    def __init__(self, use_color: bool | None = None) -> None:
        super().__init__(fmt=_UNIFIED_LOG_FORMAT, datefmt=_UNIFIED_DATE_FORMAT)
        if use_color is None:
            use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        context = current_request.get()
        record.request_id = getattr(record, "request_id", "") or (
            context.request_id if context else ""
        )
        orig_levelname = record.levelname
        if self.use_color and record.levelname in _COLORS:
            color = _COLORS[record.levelname]
            record.levelname = f"{color}{record.levelname}{_RESET}"
        else:
            record.levelname = record.levelname

        try:
            return redact_text(super().format(record))
        finally:
            record.levelname = orig_levelname


_root_handler: logging.Handler | None = None
_ring_handler: RingBufferHandler | None = None
_current_level: str = "INFO"
_dump_requests_override: bool | None = None
_dump_to_file_override: bool | None = None
_dump_dir_override: str | None = None


def is_dump_to_file_enabled() -> bool:
    """检查是否启用将请求与响应转储至本地文件。

    优先级：
    1. 运行时动态配置 (_dump_to_file_override)
    2. 环境变量 AISTUDIO_DUMP_FILE / AISTUDIO_DUMP_TO_FILE
    3. config.yaml 中 logging.dump_to_file
    控制台与文件转储独立配置。
    """
    global _dump_to_file_override
    if _dump_to_file_override is not None:
        return _dump_to_file_override

    for name in ("AISTUDIO_DUMP_FILE", "AISTUDIO_DUMP_TO_FILE"):
        val = os.getenv(name)
        if val is not None:
            return val.strip().lower() in ("1", "true", "yes", "on", "dump", "y", "t")

    from aistudio_api.config import settings

    if getattr(settings, "dump_to_file", False):
        return True

    try:
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_configured_logging_settings,
        )

        cfg = get_configured_logging_settings()
        return bool(cfg.get("dump_to_file", False))
    except Exception:
        return False


def set_dump_to_file(enabled: bool | None) -> None:
    """动态设置是否将转储数据持久化为文件；传 None 清除运行时覆盖。"""
    global _dump_to_file_override
    _dump_to_file_override = enabled
    if enabled is not None:
        from aistudio_api.config import settings

        settings.dump_to_file = enabled


def get_dump_dir() -> Path:
    """获取或创建请求转储文件存放目录。"""
    global _dump_dir_override

    def resolve_directory(value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            from aistudio_api.config import resolve_data_dir

            path = resolve_data_dir() / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    if _dump_dir_override:
        return resolve_directory(_dump_dir_override)

    val = os.getenv("AISTUDIO_DUMP_DIR")
    if val and val.strip():
        return resolve_directory(val.strip())

    try:
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_configured_logging_settings,
        )

        cfg = get_configured_logging_settings()
        custom_dir = str(cfg.get("dump_dir") or "").strip()
        if custom_dir:
            from aistudio_api.config import resolve_data_dir

            p = Path(custom_dir)
            if not p.is_absolute():
                p = resolve_data_dir() / p
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass

    from aistudio_api.config import settings

    cfg_dir = getattr(settings, "dump_dir", None)
    if cfg_dir and str(cfg_dir).strip():
        return resolve_directory(str(cfg_dir).strip())

    from aistudio_api.config import resolve_data_dir

    default_dir = resolve_data_dir() / "dumps"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def set_dump_dir(dump_dir: str | Path | None) -> None:
    """动态设置转储文件存放目录。"""
    global _dump_dir_override
    _dump_dir_override = str(dump_dir) if dump_dir is not None else None
    if dump_dir is not None:
        from aistudio_api.config import settings

        settings.dump_dir = str(dump_dir)


def is_debug_env_active() -> bool:
    """检查是否通过 DEBUG / AISTUDIO_DEBUG 环境变量显式启用了请求转储。

    注意：此环境变量专用于触发请求信息 Dump，绝不作为显示 DEBUG 级别日志的依据。
    """
    for name in ("DEBUG", "AISTUDIO_DEBUG", "AISTUDIO_DUMP_REQUESTS"):
        val = os.getenv(name)
        if val is not None and val.strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
            "dump",
            "y",
            "t",
        ):
            return True
    return False


def is_dump_requests_enabled() -> bool:
    """判断当前是否开启了请求信息转储 (Dump) 功能。

    优先级：
    1. 运行时动态配置 (_dump_requests_override)
    2. DEBUG / AISTUDIO_DEBUG 环境变量
    3. config.yaml 中 logging.dump_requests 配置
    """
    global _dump_requests_override
    if _dump_requests_override is not None:
        return _dump_requests_override

    if is_debug_env_active():
        return True

    from aistudio_api.config import settings

    if getattr(settings, "dump_requests", False):
        return True

    try:
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_configured_logging_settings,
        )

        cfg = get_configured_logging_settings()
        return bool(cfg.get("dump_requests", False))
    except Exception:
        return False


def set_dump_requests(enabled: bool | None) -> None:
    """动态更新请求转储开关；传 None 清除运行时覆盖，恢复 env/config 优先级。"""
    global _dump_requests_override
    _dump_requests_override = enabled
    if enabled is not None:
        from aistudio_api.config import settings

        settings.dump_requests = enabled


def get_log_level() -> str:
    """获取当前全局配置的日志级别名称 (如 'INFO', 'DEBUG')。"""
    return _current_level


def set_log_level(level: str | int) -> None:
    """动态设置全局与 aistudio 系列日志器的日志级别。"""
    global _current_level
    if isinstance(level, int):
        level_name = logging.getLevelName(level)
    else:
        level_name = str(level).strip().upper()

    numeric_level = getattr(logging, level_name, logging.INFO)
    _current_level = level_name

    root = logging.getLogger()
    root.setLevel(numeric_level)

    aistudio_logger = logging.getLogger("aistudio")
    aistudio_logger.setLevel(numeric_level)

    # 同步更新已挂载的 handler 级别
    if _root_handler:
        _root_handler.setLevel(numeric_level)
    if _ring_handler:
        _ring_handler.setLevel(numeric_level)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(logger_name).setLevel(numeric_level)

    # 非 DEBUG 模式下静默外部 HTTP 客户端的冗余网络日志
    httpx_level = logging.DEBUG if level_name == "DEBUG" else logging.WARNING
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(httpx_level)


def setup_logging(level: str | int = "INFO") -> None:
    """初始化全局日志系统并统一所有子系统的格式与输出。"""
    global _root_handler, _ring_handler
    formatter = UnifiedFormatter()

    if _root_handler is None:
        # 强制 UTF-8 输出，避免 cp936 等 locale 下中文/emoji 触发
        # UnicodeEncodeError 刷屏。
        stderr = sys.stderr
        if hasattr(stderr, "reconfigure"):
            with contextlib.suppress(Exception):
                stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        _root_handler = logging.StreamHandler(stderr)
        _root_handler.setFormatter(formatter)
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(_root_handler)
    else:
        _root_handler.setFormatter(formatter)

    # 内存环形缓冲镜像：供 Web 面板 /api/logs 查询运行日志
    if _ring_handler is None:
        _ring_handler = RingBufferHandler()
        root = logging.getLogger()
        if _ring_handler not in root.handlers:
            root.addHandler(_ring_handler)
    else:
        _ring_handler.setFormatter(formatter)

    # 将 uvicorn 与 httpx 的日志器也接入统一格式
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "httpx",
        "httpcore",
    ):
        lgr = logging.getLogger(logger_name)
        lgr.handlers.clear()
        if _root_handler not in lgr.handlers:
            lgr.addHandler(_root_handler)
        # 同时也挂环形缓冲 handler，让上游/uvicorn 错误能进 /api/logs。
        # 这些 logger 的 propagate 关闭，仅靠 root 无法镜像到 ring。
        if _ring_handler is not None and _ring_handler not in lgr.handlers:
            lgr.addHandler(_ring_handler)
        lgr.propagate = False

    set_log_level(level)


def get_logger(name: str = "aistudio") -> logging.Logger:
    """获取遵循统一命名空间的日志器实例。"""
    if not name.startswith("aistudio.") and name != "aistudio":
        full_name = f"aistudio.{name}"
    else:
        full_name = name
    return logging.getLogger(full_name)


dump_logger = get_logger("dump")

# 敏感键名匹配：按非字母数字边界切词后整体比对，避免子串误伤。
# 旧实现用无边界正则，把 max_tokens / total_tokens / cached_tokens /
# promptTokenCount / token_count / author / session_id / ticket 之外的
# 大量无关字段一并打成 "***"，导致 dump 里 token 计量与参数上限失真。
_STRONG_SENSITIVE_SEGMENTS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "passwd",
        "secret",
        "credential",
        "credentials",
        "proof",
        "ticket",
        "sapisid",
        "apisid",
        "sapisidhash",
        # Google 会话 Cookie 名（__Secure-1PSIDTS 等），dump 里若以键名出现同样属于凭据
        "psid",
        "psidts",
        "osid",
        "hsid",
        "ssid",
        # Google 会话 Cookie 的变体与复数形式（__Secure-3PAPISID、X-Google-Cookies）
        "papisid",
        "sid",
        "sidcc",
        "cookies",
    }
)
# 这些词过于常见（key=分页键、token=计量、auth=作者等），只有出现在
# 键名末尾（api_key / x-goog-api-key / token / auth）才视为凭据。
_WEAK_SENSITIVE_SEGMENTS = frozenset({"key", "keys", "token", "auth", "session"})

_SEGMENT_SPLIT_RE = re.compile(r"[^a-z0-9]+")
# camelCase / PascalCase 边界：refreshToken -> refresh Token，APIKey -> API Key
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _key_segments(key: str) -> list[str]:
    """把键名切成小写词段（api_key / api-key / refreshToken / APIKey 均可切）。"""
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", key.strip())
    return [seg for seg in _SEGMENT_SPLIT_RE.split(spaced.lower()) if seg]


def _is_sensitive_key(key: str) -> bool:
    """判断键名是否代表凭据类字段。"""
    segments = _key_segments(key)
    if not segments:
        return False
    # 强段（cookie/password/secret/... 与 Google 会话 Cookie 名）命中即凭据，
    # 不受分页豁免影响：否则 pagePassword / page_cookie 这类键会把真实凭据
    # 原样写进 dump。
    if any(seg in _STRONG_SENSITIVE_SEGMENTS for seg in segments):
        return True
    # nextPageToken / pageToken 是分页游标，不是凭据；脱敏只会干扰排查。
    # 该豁免只对弱规则（末段 key/token/auth/session）生效。
    if "page" in segments:
        return False
    return segments[-1] in _WEAK_SENSITIVE_SEGMENTS


def _mask_sensitive_value(key: str, val: str) -> str:
    """对敏感字段值做固定掩码，绝不保留任何原始前缀。"""
    if not val:
        return "***"
    from urllib.parse import unquote

    if _is_sensitive_key(unquote(key)):
        return "***"
    return val


def _mask_url(url: str) -> str:
    """脱敏 URL 查询串中的敏感参数（如 ?key=<管理密码>）。"""
    url = redact_text(url)
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    parts: list[str] = []
    for pair in query.split("&"):
        name, sep, value = pair.partition("=")
        parts.append(f"{name}{sep}{_mask_sensitive_value(name, value)}")
    return f"{base}?{'&'.join(parts)}"


def _mask_headers(h: dict[str, str] | None) -> dict[str, str]:
    if not h:
        return {}
    return {k: _mask_sensitive_value(k, str(v)) for k, v in h.items()}


def _mask_query_params(qp: dict[str, str] | None) -> dict[str, str]:
    if not qp:
        return {}
    return {k: _mask_sensitive_value(k, str(v)) for k, v in qp.items()}


def _mask_json_value(value: object) -> object:
    """递归脱敏 JSON 结构：键名命中敏感词的值整体替换为 '***'。

    用于请求/响应体，避免 Authorization/Cookie/API Key 等凭据随 Dump
    落到控制台、内存环形缓冲（/api/logs、SSE）或磁盘文件。
    """
    if isinstance(value, dict):
        return {
            str(k): (
                "***"
                if _is_sensitive_key(str(k)) or str(k).lower() == "yaml_content"
                else _mask_json_value(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_json_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def dump_request_exchange(
    *,
    req_id: str,
    method: str,
    url: str,
    client: str,
    headers: dict[str, str],
    query_params: dict[str, str],
    body_text: str | None,
    status_code: int,
    elapsed_ms: float,
    response_headers: dict[str, str] | None = None,
    response_text: str | None = None,
    is_stream: bool = False,
    model: str | None = None,
    account_id: str | None = None,
    auth_user: str | None = None,
    token_usage: dict[str, object] | None = None,
    finish_reason: str | None = None,
    error_detail: str | None = None,
    stream_complete: bool = False,
    console: bool = True,
    request_body_truncated: bool = False,
    response_body_truncated: bool = False,
) -> None:
    """转储单次完整请求与响应详细信息。"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    safe_headers = _mask_headers(headers)
    safe_query_params = _mask_query_params(query_params)
    safe_url = _mask_url(url)
    header_lines = "\n".join(f"    {k}: {v}" for k, v in safe_headers.items())

    # 解析 + 脱敏：请求/响应体都可能携带凭据，不能直接打印/落盘原文。
    def _parse_and_mask(text: str | None) -> object | None:
        if not text:
            return None
        try:
            return _mask_json_value(json.loads(text))
        except Exception:
            return "(non-JSON payload omitted)"

    parsed_body = None if request_body_truncated else _parse_and_mask(body_text)
    parsed_resp = (
        None if is_stream or response_body_truncated else _parse_and_mask(response_text)
    )

    # 格式化请求体（基于脱敏后的结构）
    if request_body_truncated:
        pretty_body = "(请求体超过转储上限，已省略)"
    elif parsed_body is not None:
        pretty_body = json.dumps(parsed_body, ensure_ascii=False, indent=2)
        if len(pretty_body) > 4000:
            pretty_body = (
                pretty_body[:4000]
                + f"\n... [truncated, total {len(body_text or '')} chars]"
            )
    else:
        pretty_body = "(空请求体)"
    # 格式化响应体（基于脱敏后的结构）
    if is_stream:
        pretty_resp = f"(流式响应{'已结束' if stream_complete else '已开始'}，耗时 {elapsed_ms:.1f}ms；正文不转储)"
    elif response_body_truncated:
        pretty_resp = "(响应体超过转储上限，已省略)"
    elif parsed_resp is not None:
        pretty_resp = json.dumps(parsed_resp, ensure_ascii=False, indent=2)
        if len(pretty_resp) > 4000:
            pretty_resp = (
                pretty_resp[:4000]
                + f"\n... [truncated, total {len(response_text or '')} chars]"
            )
    else:
        pretty_resp = "(空响应体)"
    safe_resp_headers = _mask_headers(response_headers)
    resp_header_lines = ""
    if response_headers:
        resp_header_lines = (
            "响应头:\n"
            + "\n".join(f"    {k}: {v}" for k, v in safe_resp_headers.items())
            + "\n"
        )
    dump_file_path: Path | None = None
    if is_dump_to_file_enabled():
        try:
            dump_folder = get_dump_dir()
            clean_id = re.sub(r"[^\w\-.]", "_", req_id)
            dump_file_path = dump_folder / f"{clean_id}.json"

            # 落盘一律写脱敏值：绝不把 Authorization/Cookie/API Key/正文原文
            # 写入磁盘；控制台同样已脱敏，避免脱敏形同虚设。
            def _masked_text(text: str | None, parsed: object | None) -> str | None:
                if not text:
                    return None
                if isinstance(parsed, (dict, list)):
                    return json.dumps(parsed, ensure_ascii=False)
                return "(non-JSON payload omitted)"

            safe_body_text = _masked_text(body_text, parsed_body)
            safe_resp_text = _masked_text(response_text, parsed_resp)

            record_data = {
                "req_id": req_id,
                "timestamp": timestamp,
                "iso_timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "method": method,
                "url": safe_url,
                "client": client,
                "model": model,
                "account_id": account_id,
                "auth_user": auth_user,
                "headers": safe_headers,
                "safe_headers": safe_headers,
                "query_params": safe_query_params,
                "safe_query_params": safe_query_params,
                "body": parsed_body,
                "raw_body": safe_body_text,
                "request_body_truncated": request_body_truncated,
                "status_code": status_code,
                "elapsed_ms": elapsed_ms,
                "is_stream": is_stream,
                "stream_complete": stream_complete,
                "response_headers": safe_resp_headers,
                "safe_response_headers": safe_resp_headers,
                "response_body": parsed_resp,
                "raw_response_body": safe_resp_text,
                "response_body_truncated": response_body_truncated,
                "token_usage": token_usage,
                "finish_reason": finish_reason,
                "error_detail": redact_text(error_detail or ""),
            }
            atomic_write_json(dump_file_path, record_data, private=True)

            latest_file = dump_folder / "latest_request.json"
            atomic_write_json(latest_file, record_data, private=True)
            _prune_dumps(dump_folder)
        except Exception as e:
            dump_logger.warning("请求文件转储失败: %s", e)

    file_line = f"转储文件:     {dump_file_path}\n" if dump_file_path else ""
    model_line = f"目标模型:     {model}\n" if model else ""
    acc_line = (
        f"调度账号:     {account_id} (u/{auth_user or '0'})\n" if account_id else ""
    )
    usage_line = (
        f"Token统计:    {json.dumps(token_usage, ensure_ascii=False)}\n"
        if token_usage
        else ""
    )
    finish_line = f"结束原因:     {finish_reason}\n" if finish_reason else ""
    error_line = f"错误详情:     {error_detail}\n" if error_detail else ""
    dump_msg = (
        f"\n==================== [请求报文转储: {req_id}] ====================\n"
        f"记录时间:     {timestamp}\n"
        f"客户端:       {client}\n"
        f"请求接口:     {method} {safe_url}\n"
        f"{model_line}"
        f"{acc_line}"
        f"{file_line}"
        f"查询参数:     {safe_query_params if safe_query_params else '(无)'}\n"
        f"请求头:\n{header_lines}\n"
        f"请求体:\n{pretty_body}\n"
        f"-------------------- [响应报文转储: {req_id}] --------------------\n"
        f"响应状态:     HTTP {status_code}\n"
        f"处理耗时:     {elapsed_ms:.2f} ms\n"
        f"{usage_line}"
        f"{finish_line}"
        f"{error_line}"
        f"{resp_header_lines}"
        f"响应内容:\n{pretty_resp}\n"
        f"==================== [转储结束: {req_id}] ===================="
    )

    if console:
        dump_logger.info(dump_msg)


_last_dump_cleanup = 0.0
_dump_cleanup_lock = threading.Lock()


def _prune_dumps(folder: Path) -> None:
    """Bound debug payload retention by age, file count, and total disk use."""
    global _last_dump_cleanup
    with _dump_cleanup_lock:
        now = time.time()
        if now - _last_dump_cleanup < 60:
            return
        _last_dump_cleanup = now
        from aistudio_api.config import settings

        def modified(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0

        files = sorted(folder.glob("req_*.json"), key=modified, reverse=True)
        latest = folder / "latest_request.json"
        all_files = [*files, *([latest] if latest.is_file() else [])]
        for path in all_files:
            try:
                if now - path.stat().st_mtime > settings.dump_max_age_days * 86400:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

        kept: list[Path] = []
        for path in files:
            if not path.is_file():
                continue
            kept.append(path)
            if len(kept) > settings.dump_max_files:
                path.unlink(missing_ok=True)

        remaining = [*kept, *([latest] if latest.is_file() else [])]
        total = 0
        for path in sorted(remaining, key=modified, reverse=True):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total + size > settings.dump_max_total_bytes:
                path.unlink(missing_ok=True)
            else:
                total += size
