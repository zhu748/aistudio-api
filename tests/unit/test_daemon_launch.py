"""本地 WAA 签名服务自动拉起路径的单元测试。

覆盖此前完全没有测试的两条高风险路径：
1. ``is_local_signer_url`` 必须按主机名精确判定，不能用子串匹配
   （``http://127.0.0.1:8081.evil.com`` 曾被误判为本机）。
2. 拉起后探活失败必须**先回收子进程**再返回失败原因，
   否则会留下僵尸进程并长期占用 8081 端口，导致后续启动无法绑定。

本地引擎是唯一签名方式（远程回退与多节点切换已移除）：失败只报因，
不切换任何节点。
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


def test_ensure_signer_ready_reaps_child_on_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探活失败的本地实例必须被 terminate + wait 后返回失败原因。"""
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    # wait() 正常返回，避免走 TimeoutExpired 分支
    fake_proc.wait.return_value = 0
    fake_proc.poll.return_value = 1

    # 不往仓库里写假二进制：直接让产物探测命中
    monkeypatch.setattr(
        Path, "is_file", lambda self: self.name in ("waa-signer", "waa-signer.exe")
    )
    monkeypatch.setattr(daemon.os, "access", lambda *_a, **_kw: True)

    monkeypatch.setattr(daemon, "_daemon_proc", None, raising=False)
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)
    monkeypatch.setattr(daemon, "_set_parent_death_signal", lambda: None)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **kw: fake_proc)
    monkeypatch.setattr(daemon.time, "sleep", lambda _s: None)

    reason = daemon.ensure_signer_ready(server_url="http://127.0.0.1:8081")

    # 子进程被回收，且全局句柄被清空
    fake_proc.terminate.assert_called_once()
    fake_proc.wait.assert_called()
    assert daemon._daemon_proc is None
    # 失败必须显式报因（供 /ready 与日志呈现），而不是静默成功
    assert reason is not None


def test_ensure_signer_ready_skips_launch_when_port_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口已占用但 /health 不通时不得再拉起实例（会立刻端口冲突退出）。"""
    popen_calls: list[object] = []
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: True)
    monkeypatch.setattr(
        daemon.subprocess, "Popen", lambda *a, **kw: popen_calls.append(a)
    )

    reason = daemon.ensure_signer_ready()

    assert popen_calls == []
    # 端口被无关进程占用时必须返回失败原因，不能静默返回成功
    assert reason is not None and "占用" in reason


def test_ensure_signer_ready_reports_missing_binary(monkeypatch):
    """本地二进制缺失时返回带构建指引的失败原因。"""
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)

    error = daemon.ensure_signer_ready(server_url="http://127.0.0.1:8081")

    assert error is not None and "未检测到" in error


def test_ensure_signer_ready_uses_local_node_url_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未指定 server_url 时固定解析本地节点（唯一签名方式）。"""
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: True)
    probed: list[str] = []
    monkeypatch.setattr(
        daemon,
        "is_service_healthy",
        lambda url: probed.append(url) or True,
    )

    assert daemon.ensure_signer_ready() is None
    assert probed == ["http://127.0.0.1:8081"]


def test_local_launch_overrides_platform_listen_env(monkeypatch):
    """PORT/LISTEN_ADDR 属于主服务，不能使本机子进程退出或占用公网端口。"""
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("LISTEN_ADDR", "0.0.0.0:10000")
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(daemon.os, "access", lambda *_a, **_kw: True)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)
    health = iter([False, True])
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: next(health))
    proc = MagicMock()
    proc.poll.return_value = None
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon, "_daemon_proc", None)

    assert daemon.ensure_signer_ready(server_url="http://127.0.0.1:8081") is None
    child_env = popen.call_args.kwargs["env"]
    assert child_env["LISTEN_ADDR"] == "127.0.0.1:8081"
    assert popen.call_args.args[0][1:3] == ["-listen", "127.0.0.1:8081"]


def test_local_launch_allows_slow_cold_start(monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(daemon.os, "access", lambda *_a, **_kw: True)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)
    clock = [0.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        daemon.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: clock[0] >= 3.0)
    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(daemon.subprocess, "Popen", MagicMock(return_value=proc))
    monkeypatch.setattr(daemon, "_daemon_proc", None)

    assert daemon.ensure_signer_ready(server_url="http://127.0.0.1:8081") is None
    assert clock[0] >= 3.0
    proc.terminate.assert_not_called()
