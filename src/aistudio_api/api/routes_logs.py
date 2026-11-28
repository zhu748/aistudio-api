"""运行日志查看与 SSE 实时日志流路由（自 routes_system.py 拆分）。"""

from __future__ import annotations

import asyncio
import contextlib
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.logs")

public_router = APIRouter()
protected_router = APIRouter()

# ========== 运行日志查看（内存环形缓冲） ==========

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def clean_level(level: object) -> str:
    """归一化日志级别查询参数：非法/缺省回退 INFO。"""
    value = str(level or "INFO").strip().upper()
    return value if value in _LOG_LEVELS else "INFO"


def clean_limit(limit: object, max_limit: int, default: int) -> int:
    """钳位返回条数：1~max_limit，非法/缺省回退 default。"""
    try:
        return max(1, min(int(limit), max_limit))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@protected_router.get("/api/logs")
async def get_runtime_logs(
    level: str = "INFO",
    limit: int = 300,
    search: str | None = None,
    order: str = "desc",
    category: str = "model",
    request_id: str = "",
    model: str = "",
    account_id: str = "",
) -> dict[str, object]:
    """查询最近运行日志（内存环形缓冲），供面板展示运行日志与报错。

    - ``level``: 最低日志级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL）
    - ``limit``: 返回条数上限（1~1000）
    - ``search``: 关键字过滤（匹配消息、日志器名、级别）
    - ``order``: ``desc`` 最新在前（默认），``asc`` 按时间正序
    """
    from aistudio_api.infrastructure.utils.logger import (
        get_log_buffer_stats,
        get_recent_logs,
    )

    clean_level_value = clean_level(level)
    clean_limit_value = clean_limit(limit, 1000, 300)
    clean_order = "asc" if str(order or "").strip().lower() == "asc" else "desc"

    entries = get_recent_logs(
        min_level=clean_level_value,
        limit=clean_limit_value,
        search=search,
        order=clean_order,
        category=category,
        request_id=request_id,
        model=model,
        account_id=account_id,
    )
    stats = get_log_buffer_stats()
    return {
        "ok": True,
        "count": len(entries),
        "total": stats["size"],
        "capacity": stats["capacity"],
        "logs": entries,
    }


@protected_router.delete("/api/logs")
async def clear_runtime_logs() -> dict[str, object]:
    """清空内存日志缓冲。"""
    from aistudio_api.infrastructure.utils.logger import (
        clear_recent_logs,
        get_log_buffer_stats,
    )

    removed = clear_recent_logs()
    logger.info("内存日志缓冲已清空: 清除条数=%d", removed)
    stats = get_log_buffer_stats()
    return {
        "ok": True,
        "removed": removed,
        "total": stats["size"],
        "capacity": stats["capacity"],
    }


@protected_router.post("/api/logs/stream-ticket")
async def create_log_stream_ticket() -> dict[str, object]:
    """签发一次性 SSE 票据，供浏览器 EventSource 通过 URL 携带鉴权。

    管理密码不应出现在 URL 中（会残留在浏览器历史、代理访问日志与 Referer 中），
    因此前端先用请求头完成鉴权，换取短时（30 秒）且仅能使用一次的票据。
    """
    from aistudio_api.api.dependencies import issue_stream_ticket

    return {"ok": True, "ticket": issue_stream_ticket()}


@protected_router.get("/api/logs/stream")
async def stream_runtime_logs(
    request: Request,
    level: str = "INFO",
    limit: int = 300,
    category: str = "model",
    request_id: str = "",
    model: str = "",
    account_id: str = "",
    search: str = "",
) -> StreamingResponse:
    """SSE 实时日志流：连接后先下发历史快照，随后持续增量推送新日志。

    - ``level``: 最低日志级别过滤（连接期间固定，切换级别由前端重连实现）
    - ``limit``: 初始历史快照条数上限（1~500）
    - 鉴权复用 ``require_web_auth``：浏览器 EventSource 无法携带自定义请求头，
      因此前端先用请求头调用 ``POST /api/logs/stream-ticket`` 换取一次性票据，
      再通过 ``?ticket=`` 携带。管理密码不会出现在 URL 中。
    - 空闲期每 20 秒发送一次 ``ping`` 心跳事件（附带缓冲统计），
      既防止反向代理层超时断连，也作为前端 total/capacity 的低频刷新源

    事件协议：
    ``ready``     连接确认（附带缓冲统计与订阅级别）
    ``snapshot``  历史快照（正序日志数组，重连时前端用它整体替换本地列表）
    ``log``       单条增量日志
    ``ping``      心跳（附带缓冲统计）
    """
    import json as _json
    from collections import deque
    from collections.abc import AsyncIterator

    from aistudio_api.infrastructure.utils.logger import (
        get_log_buffer_stats,
        get_recent_logs,
        matches_log_entry,
        subscribe_log_entries,
        unsubscribe_log_entries,
    )

    clean_level_value = clean_level(level)
    clean_limit_value = clean_limit(limit, 500, 300)

    def _sse(event: str, payload: object) -> str:
        data = _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event}\ndata: {data}\n\n"

    async def _event_stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()

        def _on_entry(entry: dict[str, object]) -> None:
            # 级别过滤在入队前完成，低于订阅级别的日志不占用连接带宽
            if not matches_log_entry(
                entry,
                min_level=clean_level_value,
                category=category,
                request_id=request_id,
                model=model,
                account_id=account_id,
                search=search,
            ):
                return

            def _put() -> None:
                try:
                    queue.put_nowait(entry)
                except asyncio.QueueFull:
                    # 慢消费者：丢弃最旧一条，优先保留最新日志
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(entry)

            with contextlib.suppress(RuntimeError):
                # 事件循环已关闭（进程关闭阶段）会抛 RuntimeError，静默丢弃
                loop.call_soon_threadsafe(_put)

        handle = subscribe_log_entries(_on_entry)
        pushed = 0
        t_start = time.perf_counter()
        client_ip = request.client.host if request.client else "unknown"
        logger.info(
            "面板实时日志流已连接: 客户端=%s, 级别>=%s", client_ip, clean_level_value
        )
        try:
            # 1) ready：连接确认 + 缓冲统计（字段命名与 /api/logs 响应保持一致）
            stats = get_log_buffer_stats()
            yield _sse(
                "ready",
                {
                    "ok": True,
                    "level": clean_level_value,
                    "total": stats["size"],
                    "capacity": stats["capacity"],
                },
            )
            # 2) 历史快照：在订阅之后读取，保证快照与增量流无缝衔接
            snapshot = get_recent_logs(
                min_level=clean_level_value,
                limit=clean_limit_value,
                order="asc",
                category=category,
                request_id=request_id,
                model=model,
                account_id=account_id,
                search=search,
            )
            yield _sse("snapshot", {"count": len(snapshot), "logs": snapshot})
            # 3) 先排空订阅瞬间积压的增量（跳过已包含在快照中的条目，按对象身份去重）
            snapshot_ids = {id(entry) for entry in snapshot}
            backlog: deque[dict[str, object]] = deque()
            while True:
                try:
                    pending = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if id(pending) not in snapshot_ids:
                    backlog.append(pending)
                del pending
            # 去重期间保留快照以免 id 被复用；完成后不再占用已投递日志。
            del snapshot, snapshot_ids
            while backlog:
                yield _sse("log", backlog.popleft())
                pushed += 1
            del backlog
            # 4) 持续增量推送 + 空闲心跳
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    stats = get_log_buffer_stats()
                    yield _sse(
                        "ping",
                        {
                            "ok": True,
                            "total": stats["size"],
                            "capacity": stats["capacity"],
                        },
                    )
                    continue
                yield _sse("log", entry)
                del entry
                pushed += 1
        finally:
            unsubscribe_log_entries(handle)
            logger.info(
                "面板实时日志流已断开: 客户端=%s, 推送条数=%d, 持续=%.1fs",
                client_ip,
                pushed,
                time.perf_counter() - t_start,
            )

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
