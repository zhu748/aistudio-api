"""h2 缺失时 HTTP/2 优雅降级的单元测试。

背景：httpx 的 HTTP/2 支持是可选依赖（h2 包）。若环境未安装 h2 而代码
仍给 AsyncClient 传 http2=True，客户端在创建时即抛
"Using http2=True, but the 'h2' package is not installed"，所有上游请求
全部 500（Render 干净环境按 requirements.txt 安装时曾复现）。

行为要求：
  - h2 可用  → 客户端启用 HTTP/2（保持现状性能）；
  - h2 缺失  → 打一条 WARNING（须进入面板日志环形缓冲）并降级
    HTTP/1.1，绝不因缺包导致整站不可用；
  - 探测结果进程内缓存，告警只发一次。
"""

from __future__ import annotations

import sys
import types

from aistudio_api.infrastructure.gateway import transport as transport_mod
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport
from aistudio_api.infrastructure.utils.logger import (
    clear_recent_logs,
    get_recent_logs,
    setup_logging,
)


async def test_http2_enabled_when_h2_importable(monkeypatch):
    """sys.modules 中存在 h2 时探测结果为 True，客户端启用 HTTP/2。"""
    monkeypatch.setattr(transport_mod, "_H2_AVAILABLE", None)
    monkeypatch.setitem(sys.modules, "h2", types.ModuleType("h2"))

    assert transport_mod._http2_enabled() is True
    assert transport_mod._H2_AVAILABLE is True


async def test_http2_disabled_when_h2_missing(monkeypatch):
    """sys.modules['h2'] 为 None 时 import 抛 ImportError，探测结果为 False。"""
    monkeypatch.setattr(transport_mod, "_H2_AVAILABLE", None)
    monkeypatch.setitem(sys.modules, "h2", None)

    assert transport_mod._http2_enabled() is False
    assert transport_mod._H2_AVAILABLE is False


async def test_client_created_with_http1_when_h2_missing(monkeypatch):
    """h2 缺失时创建的 AsyncClient 必须是 http2=False（降级而非抛错）。"""
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        is_closed = False

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(transport_mod.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(transport_mod, "_H2_AVAILABLE", False)

    transport = HTTPStreamTransport(proxy_url=None, timeout=30.0)
    await transport._get_client()

    assert captured["http2"] is False


async def test_client_created_with_http2_when_h2_present(monkeypatch):
    """h2 可用时客户端保持 http2=True（正常环境不回退）。"""
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        is_closed = False

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(transport_mod.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(transport_mod, "_H2_AVAILABLE", True)

    transport = HTTPStreamTransport(proxy_url=None, timeout=30.0)
    await transport._get_client()

    assert captured["http2"] is True


async def test_missing_h2_warning_visible_in_panel_log_buffer(monkeypatch):
    """降级 WARNING 必须进入环形缓冲（面板 Logs 页可见），且只发一次。"""
    setup_logging("INFO")
    clear_recent_logs()
    monkeypatch.setattr(transport_mod, "_H2_AVAILABLE", None)
    monkeypatch.setitem(sys.modules, "h2", None)

    assert transport_mod._http2_enabled() is False
    assert transport_mod._http2_enabled() is False  # 第二次命中缓存

    warnings = get_recent_logs(min_level="WARNING", search="HTTP/1.1")
    assert len(warnings) == 1, f"应恰好一条降级告警，实际 {len(warnings)}"
    message = str(warnings[0].get("message", ""))
    assert "h2" in message
    assert "httpx[http2]" in message
    assert warnings[0].get("name") == "aistudio.transport"


async def test_missing_h2_does_not_break_mock_request(monkeypatch):
    """端到端冒烟：h2 缺失降级后，请求仍能经 MockTransport 正常完成。"""
    import httpx

    monkeypatch.setattr(transport_mod, "_H2_AVAILABLE", False)

    transport = HTTPStreamTransport(proxy_url=None, timeout=30.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'["ok"]')

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport._clients[""] = mock_client

    status, body = await transport.send_request(
        url="https://example.com/api",
        headers={"Content-Type": "application/json"},
        body="{}",
    )

    assert status == 200
    assert body == b'["ok"]'
    await transport.close()
