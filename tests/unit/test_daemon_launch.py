"""本地 WAA 签名服务自动拉起/回退路径的单元测试。

覆盖此前完全没有测试的两条高风险路径：
1. ``is_local_signer_url`` 必须按主机名精确判定，不能用子串匹配
   （``http://127.0.0.1:8081.evil.com`` 曾被误判为本机）。
2. 拉起后 2 秒探活失败必须**先回收子进程**再回退远程节点，
   否则会留下僵尸进程并长期占用 8081 端口，导致后续启动无法绑定。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aistudio_api.infrastructure.waa import daemon


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8081", True),
        ("http://localhost:8081", True),
        ("http://[::1]:8081", True),
        ("https://127.0.0.1:8081/base", True),
    ],
)
def test_is_local_signer_url_accepts_loopback(url: str, expected: bool) -> None:
    assert daemon.is_local_signer_url(url, port=8081) is expected


@pytest.mark.parametrize(
    "url",
    [
        # 子串匹配时代的典型绕过：主机名以 127.0.0.1:8081 开头但不是本机
        "http://127.0.0.1:8081.evil.com",
        "https://waa.spinsnow.fun",
        "http://127.0.0.1:9090",
        "http://localhost",
        "not a url",
        "",
    ],
)
def test_is_local_signer_url_rejects_non_loopback(url: str) -> None:
    assert daemon.is_local_signer_url(url, port=8081) is False


def test_is_local_signer_url_without_port_accepts_any_loopback_port() -> None:
    # port=None 表示不限定端口（供 routes_system 的本地判定使用）
    assert daemon.is_local_signer_url("http://127.0.0.1:9999") is True
    assert daemon.is_local_signer_url("http://127.0.0.1:8081.evil.com") is False


def test_ensure_signer_ready_reaps_child_before_remote_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探活失败的本地实例必须被 terminate + wait，再回退远程节点。"""
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    # wait() 正常返回，避免走 TimeoutExpired 分支
    fake_proc.wait.return_value = 0

    # 不往仓库里写假二进制：直接让产物探测命中
    monkeypatch.setattr(
        Path, "is_file", lambda self: self.name in ("waa-signer", "waa-signer.exe")
    )
    monkeypatch.setattr(daemon.os, "access", lambda *_a, **_kw: True)

    monkeypatch.setattr(daemon, "_daemon_proc", None, raising=False)
    monkeypatch.setattr(
        daemon,
        "get_active_waa_server",
        lambda: {"id": "local_spidermonkey", "url": "http://127.0.0.1:8081"},
    )
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)
    monkeypatch.setattr(daemon, "_set_parent_death_signal", lambda: None)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **kw: fake_proc)
    monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

    switched: list[str] = []
    monkeypatch.setattr(
        daemon, "switch_active_waa_server", lambda server_id: switched.append(server_id)
    )

    daemon.ensure_signer_ready()

    # 子进程被回收，且全局句柄被清空
    fake_proc.terminate.assert_called_once()
    fake_proc.wait.assert_called()
    assert daemon._daemon_proc is None
    # 回退到远程节点
    assert switched == ["remote_spinsnow"]


def test_ensure_signer_ready_skips_launch_when_port_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口已占用但 /health 不通时不得再拉起实例（会立刻端口冲突退出）。"""
    popen_calls: list[object] = []
    monkeypatch.setattr(
        daemon,
        "get_active_waa_server",
        lambda: {"id": "local_spidermonkey", "url": "http://127.0.0.1:8081"},
    )
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: True)
    monkeypatch.setattr(
        daemon.subprocess, "Popen", lambda *a, **kw: popen_calls.append(a)
    )
    switched: list[str] = []
    monkeypatch.setattr(
        daemon, "switch_active_waa_server", lambda server_id: switched.append(server_id)
    )

    daemon.ensure_signer_ready()

    assert popen_calls == []
    # 端口被无关进程占用时必须回退远程节点，否则配置仍指向不可用的本地节点
    assert switched == ["remote_spinsnow"]


def test_ensure_signer_ready_noop_for_remote_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置为远程节点时直接返回，不应探活或拉起本地实例。"""
    calls: list[str] = []
    monkeypatch.setattr(
        daemon,
        "get_active_waa_server",
        lambda: {"id": "remote_spinsnow", "url": "https://waa.spinsnow.fun"},
    )
    monkeypatch.setattr(
        daemon, "is_service_healthy", lambda url: calls.append("healthy") or False
    )
    monkeypatch.setattr(
        daemon, "is_port_open", lambda host, port: calls.append("port") or False
    )

    daemon.ensure_signer_ready()

    assert calls == []
