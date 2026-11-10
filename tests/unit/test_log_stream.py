"""面板实时日志流（SSE /api/logs/stream）与日志订阅广播机制的单元测试。

说明：httpx ASGITransport 会将应用跑到响应结束才返回，无法直接测试无限
SSE 流，因此这里直接调用端点函数并手动迭代 ``body_iterator``，通过
``aclose()`` 模拟客户端断连，覆盖完整事件协议与订阅清理逻辑。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from starlette.requests import Request

# 导入 app 以触发 setup_logging()（挂载 RingBufferHandler 与订阅广播链路）
from aistudio_api.api.app import (  # noqa: F401
    app,  # pyright: ignore[reportUnusedImport]
)
from aistudio_api.api.routes_system import protected_router, stream_runtime_logs
from aistudio_api.infrastructure.utils.logger import (
    get_log_subscriber_count,
    get_logger,
    get_recent_logs,
    passes_min_level,
    subscribe_log_entries,
    unsubscribe_log_entries,
)

test_log = get_logger("sse-test")


def _make_stream_request() -> Request:
    """构造直连端点函数所需的最小 Starlette Request（客户端不断连）。"""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/logs/stream",
        "raw_path": b"/api/logs/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 54321),
        "server": ("test", 80),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": "", "more_body": False}

    return Request(scope, receive=receive)


def _parse_sse_block(block: str) -> tuple[str, str]:
    """解析单个 SSE 事件块（event: xxx / data: yyy）。"""
    event, data = "", ""
    for line in block.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
    return event, data


async def _next_event(agen: object, timeout: float = 10.0) -> tuple[str, object]:
    """读取下一个 SSE 事件并解析 JSON 载荷。"""
    block = await asyncio.wait_for(agen.__anext__(), timeout=timeout)  # type: ignore[attr-defined]
    event, data = _parse_sse_block(str(block))
    try:
        payload: object = json.loads(data) if data else None
    except json.JSONDecodeError:
        payload = data
    return event, payload


# ---------------------------------------------------------------------------
# 日志订阅广播机制（logger.py）
# ---------------------------------------------------------------------------


def test_subscribe_unsubscribe_and_broadcast():
    """订阅后同步收到新日志条目，取消订阅后不再接收。"""
    baseline = get_log_subscriber_count()
    received: list[dict[str, object]] = []
    handle = subscribe_log_entries(received.append)
    assert get_log_subscriber_count() == baseline + 1
    try:
        test_log.info("PUBSUB_MARKER_alpha %s", "中文参数")
        assert any("PUBSUB_MARKER_alpha" in str(e.get("message", "")) for e in received)
        entry = received[-1]
        assert entry["level"] == "INFO"
        assert entry["name"] == "aistudio.sse-test"
        assert isinstance(entry["ts_ms"], int)
        assert "ts" in entry
    finally:
        unsubscribe_log_entries(handle)
    assert get_log_subscriber_count() == baseline

    count_before = len(received)
    test_log.info("PUBSUB_MARKER_beta")
    assert len(received) == count_before


def test_subscriber_exception_does_not_break_logging():
    """订阅回调抛异常绝不影响日志主链路与环形缓冲。"""

    def _bad_callback(_entry: dict[str, object]) -> None:
        raise RuntimeError("boom")

    handle = subscribe_log_entries(_bad_callback)
    try:
        test_log.warning("PUBSUB_MARKER_gamma")
        recent = get_recent_logs(limit=50, order="desc")
        assert any("PUBSUB_MARKER_gamma" in str(e.get("message", "")) for e in recent)
    finally:
        unsubscribe_log_entries(handle)


def test_passes_min_level():
    """级别判断辅助函数边界行为。"""
    assert passes_min_level("ERROR", "INFO") is True
    assert passes_min_level("INFO", "INFO") is True
    assert passes_min_level("DEBUG", "INFO") is False
    assert passes_min_level("WARNING", "warning") is True
    # 未知级别按 INFO 处理
    assert passes_min_level("unknown-level", "DEBUG") is True
    assert passes_min_level("unknown-level", "WARNING") is False


def test_get_recent_logs_is_thread_safe_under_concurrent_appends():
    """并发写入线程锤击期间迭代读取不得抛 deque mutated RuntimeError。

    回归背景：环形缓冲原先无锁，后台线程（线程池/WAA daemon）打日志时
    面板查询会以 RuntimeError 中断（修复前 20 次读取内即可复现）。
    """
    import threading

    from aistudio_api.infrastructure.utils.logger import _log_ring

    stop = threading.Event()

    def _hammer() -> None:
        i = 0
        while not stop.is_set():
            # 模拟真实 RingBufferHandler 条目结构，避免污染快照字段断言
            _log_ring.append(
                {
                    "ts": "2026-01-01 00:00:00",
                    "ts_ms": i,
                    "level": "INFO",
                    "name": "hammer",
                    "message": f"m{i}",
                }
            )
            i += 1

    worker = threading.Thread(target=_hammer)
    worker.start()
    try:
        for _ in range(300):
            entries = get_recent_logs(limit=200)
            assert isinstance(entries, list)
    finally:
        stop.set()
        worker.join()


def test_stream_route_registered_on_protected_router():
    """SSE 端点已注册在受保护路由上（自动继承面板鉴权）。"""
    paths = {getattr(r, "path", "") for r in protected_router.routes}
    assert "/api/logs/stream" in paths


# ---------------------------------------------------------------------------
# SSE 流事件协议（routes_system.py）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_protocol_ready_snapshot_live_and_cleanup():
    """ready → snapshot → 增量 log 事件按序到达，断连后订阅被清理。"""
    marker = "SSE_LIVE_MARKER_a1b2"
    baseline = get_log_subscriber_count()

    resp = await stream_runtime_logs(request=_make_stream_request(), level="INFO")
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"
    agen = cast(AsyncGenerator[str, None], resp.body_iterator)

    seen_ready = False
    seen_snapshot = False
    found_marker = False
    try:
        # 1) ready 事件：附带缓冲统计与订阅级别
        event, payload = await _next_event(agen)
        assert event == "ready"
        assert isinstance(payload, dict)
        assert payload["ok"] is True
        assert payload["level"] == "INFO"
        assert "total" in payload and "capacity" in payload
        seen_ready = True

        # 2) snapshot 事件：正序历史数组，条目结构完整
        event, payload = await _next_event(agen)
        assert event == "snapshot"
        assert isinstance(payload, dict)
        logs = payload["logs"]
        assert isinstance(logs, list) and len(logs) > 0
        assert isinstance(logs[0], dict)
        assert {"ts", "ts_ms", "level", "name", "message"} <= set(logs[0])
        seen_snapshot = True

        # 3) 触发一条新日志，等待增量推送
        test_log.info("SSE live 推送验证 %s", marker)
        for _ in range(20):
            event, payload = await _next_event(agen)
            if event == "log" and marker in str(payload):
                assert isinstance(payload, dict)
                assert payload["level"] == "INFO"
                assert payload["name"] == "aistudio.sse-test"
                found_marker = True
                break
        assert found_marker
    finally:
        # 4) 模拟客户端断连：生成器被关闭，订阅必须被清理
        await agen.aclose()

    assert seen_ready and seen_snapshot
    for _ in range(50):
        if get_log_subscriber_count() == baseline:
            break
        await asyncio.sleep(0.02)
    assert get_log_subscriber_count() == baseline


@pytest.mark.asyncio
async def test_stream_level_filter_blocks_lower_levels():
    """订阅级别为 WARNING 时，INFO 日志不应出现在流中。"""
    info_marker = "SSE_FILTER_INFO_c3d4"
    warn_marker = "SSE_FILTER_WARN_e5f6"
    baseline = get_log_subscriber_count()

    resp = await stream_runtime_logs(
        request=_make_stream_request(), level="WARNING", limit=5
    )
    agen = cast(AsyncGenerator[str, None], resp.body_iterator)
    collected: list[str] = []
    found_warn = False
    try:
        event, _payload = await _next_event(agen)
        assert event == "ready"

        test_log.info("不应推送 %s", info_marker)
        test_log.warning("应当推送 %s", warn_marker)

        # 标记日志有两条合法到达通道：ready 之后、生成器恢复执行之前写入
        # 的日志会同时进入历史快照（snapshot 事件，对象身份去重后不再作为
        # log 事件重发）与订阅队列，实际走哪条取决于事件循环调度顺序——
        # Python 3.11 的 wait_for 会把 __anext__ 包成 Task，订阅回调
        # call_soon_threadsafe 先执行，快照读取时标记已在环形缓冲中；
        # 3.12+ 则内联 await，快照先读、随后经 queue.get() 以 log 事件
        # 送达。增量通道本身由
        # test_stream_protocol_ready_snapshot_live_and_cleanup 单独覆盖。
        for _ in range(20):
            event, payload = await _next_event(agen)
            collected.append(str(payload))
            if event in ("log", "snapshot") and warn_marker in str(payload):
                found_warn = True
                break
        assert found_warn
        # INFO 级别日志无论走快照还是增量通道都必须被过滤掉
        assert all(info_marker not in c for c in collected)
    finally:
        await agen.aclose()

    assert get_log_subscriber_count() == baseline


@pytest.mark.asyncio
async def test_stream_invalid_params_fall_back_to_defaults():
    """非法 level / limit 参数回退到安全默认值而不报错。"""
    resp = await stream_runtime_logs(
        request=_make_stream_request(), level="not-a-level", limit=999999
    )
    agen = cast(AsyncGenerator[str, None], resp.body_iterator)
    try:
        event, payload = await _next_event(agen)
        assert event == "ready"
        assert isinstance(payload, dict)
        assert payload["level"] == "INFO"
    finally:
        await agen.aclose()
