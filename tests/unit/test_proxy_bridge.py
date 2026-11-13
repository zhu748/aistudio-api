"""TURN/SSTP 桥接器配置、进程所有权与凭据处理测试（离线，无需 Go 工具链）。

覆盖：
- 统一解析入口（native 透传 / 空与 direct 哨兵 / 桥接启动 fail-closed）；
- 桥接器私有配置的权限与凭据传递契约（不进命令行、0600 临时文件用后即删）；
- 崩溃重启的端口复用与换端口重试；
- 启动失败的受控错误码 → 中文文案映射（不回显子进程任意输出）；
- 空闲回收与关停清扫。
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import cast
from unittest import mock
from unittest.mock import MagicMock

import pytest

import aistudio_api.infrastructure.proxy_bridge as pb
from aistudio_api.infrastructure.proxy_bridge import (
    ProxyBridgeError,
    ProxyBridgeManager,
    resolve_proxy_url,
    resolve_proxy_url_async,
    resolve_proxy_url_cached,
)


@pytest.fixture
def isolated_bridge_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把桥接状态目录与二进制发现指向临时目录，隔离真实文件系统。"""
    state_dir = tmp_path / "proxy-bridge"
    monkeypatch.setattr(pb, "_bridge_state_dir", lambda: state_dir, raising=True)
    monkeypatch.setattr(pb, "find_bridge_binary", lambda: None, raising=True)
    return state_dir


# ---------------------------------------------------------------------------
# 统一解析入口
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("socks5://u:p@1.2.3.4:1080", "socks5://u:p@1.2.3.4:1080"),
        ("socks5h://1.2.3.4:1080", "socks5h://1.2.3.4:1080"),
        ("http://1.2.3.4:8080", "http://1.2.3.4:8080"),
        ("https://1.2.3.4", "https://1.2.3.4"),
    ],
)
def test_native_proxies_pass_through_unchanged(
    raw: str, expected: str, isolated_bridge_env: Path
) -> None:
    assert resolve_proxy_url(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "direct", "DIRECT", "直连"])
def test_empty_and_direct_tokens_mean_no_proxy(
    raw: str | None, isolated_bridge_env: Path
) -> None:
    assert resolve_proxy_url(raw) is None


def test_unrecognised_uri_fails_closed_with_masked_value(
    isolated_bridge_env: Path,
) -> None:
    with pytest.raises(ProxyBridgeError) as exc:
        resolve_proxy_url("bogus://user:secret@1.2.3.4:1")
    assert "secret" not in str(exc.value)
    assert "bogus" in str(exc.value)


async def test_async_resolve_matches_sync_for_native(
    isolated_bridge_env: Path,
) -> None:
    assert (
        await resolve_proxy_url_async("socks5://1.2.3.4:1080")
        == "socks5://1.2.3.4:1080"
    )


def test_cached_resolve_has_no_cold_start_side_effect(
    isolated_bridge_env: Path,
) -> None:
    # native 原样返回；桥接实例未启动时也不触发拉起（返回原始规范 URI）
    assert resolve_proxy_url_cached("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert (
        resolve_proxy_url_cached("turn:1.2.3.4:3478?transport=tcp")
        == "turn://1.2.3.4:3478"
    )
    assert resolve_proxy_url_cached(None) is None


def test_cached_resolve_returns_local_socks5_when_running(
    isolated_bridge_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ProxyBridgeManager()
    monkeypatch.setattr(
        manager,
        "cached_local_socks5",
        lambda uri: "socks5://127.0.0.1:12080",
        raising=True,
    )
    monkeypatch.setattr(pb, "bridge_manager", manager)
    assert resolve_proxy_url_cached("turn://1.2.3.4:3478") == "socks5://127.0.0.1:12080"


# ---------------------------------------------------------------------------
# 桥接器进程管理（子进程全 mock）
# ---------------------------------------------------------------------------

_URI = "sstp://private-user:private-password@vpn.example.com:443?sni=cert.example.com"


def _fake_launch(state_dir: Path):
    """构造 Popen 侧效果：读取私有配置、断言权限、写 ready 标记、返回假进程。"""

    def launch(args, **kwargs):
        assert args[1:2] == ["-config"], args  # 命令行只有 -config 路径
        config_path = Path(args[-1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        # 凭据绝不出现在命令行或进程参数里（ps 泄漏面）
        assert "private-password" not in " ".join(args)
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
        Path(config["ready_file"]).write_text("ready\n", encoding="ascii")
        process = MagicMock()
        process.poll.return_value = None
        return process

    return launch


def test_missing_binary_fails_closed_with_actionable_hint(
    isolated_bridge_env: Path,
) -> None:
    manager = ProxyBridgeManager()
    with pytest.raises(ProxyBridgeError) as exc:
        manager.get_local_socks5(_URI)
    assert "AISTUDIO_PROXY_BRIDGE_BIN" in str(exc.value)
    assert "不会自动降级直连" in str(exc.value)
    assert "private-password" not in str(exc.value)


def test_private_config_stable_port_and_idle_cleanup(
    isolated_bridge_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ProxyBridgeManager()
    with (
        mock.patch.object(pb, "find_bridge_binary", return_value="/fake/bridge"),
        mock.patch.object(
            pb.subprocess, "Popen", side_effect=_fake_launch(isolated_bridge_env)
        ) as popen,
        mock.patch.object(pb, "_wait_socks5_ready", return_value=True),
        mock.patch.object(pb, "_find_free_port", return_value=12080),
    ):
        assert manager.get_local_socks5(_URI) == "socks5://127.0.0.1:12080"
        assert manager.get_local_socks5(_URI) == "socks5://127.0.0.1:12080"
        assert popen.call_count == 1  # 同 URI 复用同一进程 / 同一端口
    instance = manager._instances[
        "sstp://private-user:private-password@vpn.example.com:443?sni=cert.example.com"
    ]
    # 私有配置与 ready 标记在启动后立即删除（凭据不落常驻文件）
    assert not Path(instance.log_path).with_suffix(".json").exists()
    # 空闲回收：阈值内不回收，超时后回收
    with mock.patch.object(pb, "_idle_minutes", return_value=1):
        instance.last_used = time.time() - 30
        assert manager.reap_idle() == 0
        instance.last_used = time.time() - 120
        assert manager.reap_idle() == 1
    cast("MagicMock", instance.process).terminate.assert_called_once()
    manager.shutdown_all()


def test_launch_failure_maps_controlled_error_code_to_hint(
    isolated_bridge_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ProxyBridgeManager()

    def failing_launch(args, **kwargs):
        # 子进程立即退出，日志里留下受控错误码
        config_path = Path(args[-1])
        log_path = config_path.with_suffix(".log")
        log_path.write_text(
            "some noise\nsstp_auth_failed_requires_mschapv2\n", encoding="utf-8"
        )
        process = MagicMock()
        process.poll.return_value = 1
        return process

    with (
        mock.patch.object(pb, "find_bridge_binary", return_value="/fake/bridge"),
        mock.patch.object(pb.subprocess, "Popen", side_effect=failing_launch),
        mock.patch.object(pb, "_wait_socks5_ready", return_value=False),
        mock.patch.object(pb, "_find_free_port", return_value=12081),
        pytest.raises(ProxyBridgeError) as exc,
    ):
        manager.get_local_socks5(_URI)
    assert "SSTP 认证失败" in str(exc.value)
    assert "MS-CHAPv2" in str(exc.value)
    assert "private-password" not in str(exc.value)
    manager.shutdown_all()


def test_unknown_error_code_degrades_to_generic_timeout_hint(
    isolated_bridge_env: Path,
) -> None:
    manager = ProxyBridgeManager()

    def failing_launch(args, **kwargs):
        log_path = Path(args[-1]).with_suffix(".log")
        log_path.write_text("something-unmapped\n", encoding="utf-8")
        process = MagicMock()
        process.poll.return_value = 1
        return process

    with (
        mock.patch.object(pb, "find_bridge_binary", return_value="/fake/bridge"),
        mock.patch.object(pb.subprocess, "Popen", side_effect=failing_launch),
        mock.patch.object(pb, "_wait_socks5_ready", return_value=False),
        mock.patch.object(pb, "_find_free_port", return_value=12082),
        pytest.raises(ProxyBridgeError) as exc,
    ):
        manager.get_local_socks5("turn://1.2.3.4:3478")
    assert "启动超时或子进程退出" in str(exc.value)


def test_crashed_instance_restarts_preferring_same_port(
    isolated_bridge_env: Path,
) -> None:
    manager = ProxyBridgeManager()
    launches: list[dict[str, object]] = []

    def launch(args, **kwargs):
        config_path = Path(args[-1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        launches.append({"port": config["listen_port"]})
        Path(config["ready_file"]).write_text("ready\n", encoding="ascii")
        process = MagicMock()
        process.poll.return_value = None
        return process

    with (
        mock.patch.object(pb, "find_bridge_binary", return_value="/fake/bridge"),
        mock.patch.object(pb.subprocess, "Popen", side_effect=launch),
        mock.patch.object(pb, "_wait_socks5_ready", return_value=True),
        mock.patch.object(pb, "_find_free_port", return_value=19300),
    ):
        uri = "turn://1.2.3.4:3478"
        assert manager.get_local_socks5(uri) == "socks5://127.0.0.1:19300"
        # 模拟崩溃：进程死亡后再次取用应自动重启并复用原端口
        cast("MagicMock", manager._instances[uri].process).poll.return_value = 1
        assert manager.get_local_socks5(uri) == "socks5://127.0.0.1:19300"
        assert [item["port"] for item in launches] == [19300, 19300]
    manager.shutdown_all()


def test_shutdown_all_terminates_and_rejects_new_lookups(
    isolated_bridge_env: Path,
) -> None:
    manager = ProxyBridgeManager()
    with (
        mock.patch.object(pb, "find_bridge_binary", return_value="/fake/bridge"),
        mock.patch.object(
            pb.subprocess, "Popen", side_effect=_fake_launch(isolated_bridge_env)
        ),
        mock.patch.object(pb, "_wait_socks5_ready", return_value=True),
        mock.patch.object(pb, "_find_free_port", return_value=12083),
    ):
        assert manager.get_local_socks5("turn://1.2.3.4:3478")
        instance = next(iter(manager._instances.values()))
        manager.shutdown_all()
        cast("MagicMock", instance.process).terminate.assert_called_once()
        with pytest.raises(ProxyBridgeError):
            manager.get_local_socks5("turn://1.2.3.4:3478")
    # reopen 供下一轮测试隔离（不复活旧实例）
    manager.reopen()


def test_sweep_orphan_files_only_removes_dead_owners(
    isolated_bridge_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = isolated_bridge_env
    state_dir.mkdir(parents=True, exist_ok=True)
    dead_pid = 999_999_999  # 几乎必然不存在
    live_file = state_dir / f"bridge-{__import__('os').getpid()}-ns-aaaa-12000.log"
    live_file.write_text("x", encoding="utf-8")
    # 1 小时宽限期内不删；过期后删
    fresh_orphan = state_dir / f"bridge-{dead_pid}-ns-bbbb-12001.log"
    fresh_orphan.write_text("x", encoding="utf-8")
    old_orphan = state_dir / f"bridge-{dead_pid}-ns-cccc-12002.log"
    old_orphan.write_text("x", encoding="utf-8")
    old_ts = time.time() - 7200
    import os

    os.utime(old_orphan, (old_ts, old_ts))
    manager = ProxyBridgeManager()
    manager._sweep_orphan_files(state_dir)
    assert live_file.exists()
    assert fresh_orphan.exists()
    assert not old_orphan.exists()


def test_bridge_status_masks_credentials(
    isolated_bridge_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ProxyBridgeManager()
    with (
        mock.patch.object(pb, "find_bridge_binary", return_value="/fake/bridge"),
        mock.patch.object(
            pb.subprocess, "Popen", side_effect=_fake_launch(isolated_bridge_env)
        ),
        mock.patch.object(pb, "_wait_socks5_ready", return_value=True),
        mock.patch.object(pb, "_find_free_port", return_value=12084),
    ):
        manager.get_local_socks5(_URI)
        stats = manager.stats()
    assert stats["available"] is True
    assert stats["instances"], "应包含运行实例"
    for item in stats["instances"]:
        assert "private-password" not in str(item["uri"])
        assert "***@" in str(item["uri"])
    manager.shutdown_all()
