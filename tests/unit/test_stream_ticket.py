"""SSE 一次性票据（/api/logs/stream-ticket）的单元测试。

背景：浏览器 EventSource 无法携带自定义请求头，历史实现把管理密码直接放进
``?key=`` 查询参数，导致密码残留在浏览器历史、反向代理访问日志与 Referer 里。
现改为先用请求头鉴权换取短时一次性票据，再用 ``?ticket=`` 建立日志流。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

from aistudio_api.api import dependencies
from aistudio_api.api.dependencies import (
    consume_stream_ticket,
    issue_stream_ticket,
    require_web_auth,
)
from aistudio_api.config import settings


def _make_request(
    path: str, query: str = "", headers: dict[str, str] | None = None
) -> MagicMock:
    req = MagicMock(spec=Request)
    req.method = "GET"
    req.url = MagicMock()
    req.url.path = path
    req.query_params = _parse_query(query)
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _parse_query(query: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        result[key] = value
    return result


def test_ticket_is_single_use() -> None:
    ticket = issue_stream_ticket()
    assert ticket
    assert consume_stream_ticket(ticket) is True
    # 第二次核销必须失败：票据是一次性的
    assert consume_stream_ticket(ticket) is False


def test_empty_ticket_is_rejected() -> None:
    assert consume_stream_ticket("") is False
    assert consume_stream_ticket("   ") is False
    assert consume_stream_ticket("not-a-real-ticket") is False


def test_tickets_are_unique() -> None:
    tickets = {issue_stream_ticket() for _ in range(32)}
    assert len(tickets) == 32


def test_expired_ticket_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = issue_stream_ticket()
    # 把 TTL 改小后等待过期
    monkeypatch.setattr(dependencies, "_STREAM_TICKET_TTL_SECONDS", 0.01)
    short_ticket = issue_stream_ticket()
    time.sleep(0.05)
    assert consume_stream_ticket(short_ticket) is False
    # 之前未过期的那张仍可用（TTL 在签发时已确定）
    assert consume_stream_ticket(ticket) is True


def test_ticket_store_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "_STREAM_TICKET_MAX", 4)
    issued = [issue_stream_ticket() for _ in range(10)]
    with dependencies._stream_tickets_lock:  # pyright: ignore[reportPrivateUsage]
        assert len(dependencies._stream_tickets) <= 4  # pyright: ignore[reportPrivateUsage]
    # 只有最近签发的票据仍然有效
    valid = [t for t in issued if consume_stream_ticket(t)]
    assert len(valid) <= 4


def test_sse_path_accepts_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "web_password", "super_secret_admin_pass")
    ticket = issue_stream_ticket()
    req = _make_request("/api/logs/stream", query=f"ticket={ticket}")
    # 不抛异常即视为通过
    require_web_auth(req)


def test_sse_path_rejects_reused_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "web_password", "super_secret_admin_pass")
    ticket = issue_stream_ticket()
    require_web_auth(_make_request("/api/logs/stream", query=f"ticket={ticket}"))
    with pytest.raises(HTTPException) as exc_info:
        require_web_auth(_make_request("/api/logs/stream", query=f"ticket={ticket}"))
    assert exc_info.value.status_code == 401


def test_ticket_does_not_bypass_other_protected_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "web_password", "super_secret_admin_pass")
    ticket = issue_stream_ticket()
    # 票据只在 SSE 端点生效，不能拿来访问其它受保护接口
    with pytest.raises(HTTPException) as exc_info:
        require_web_auth(_make_request("/api/accounts", query=f"ticket={ticket}"))
    assert exc_info.value.status_code == 401
    # 且该票据未被消耗，仍可用于 SSE
    assert consume_stream_ticket(ticket) is True


def test_auth_disabled_allows_stream_without_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "web_password", "")
    require_web_auth(_make_request("/api/logs/stream"))
