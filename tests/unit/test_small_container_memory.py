"""小内存容器（512MB 级 Render 部署）的内存与持久化优化回归测试。

背景（2026-11-26 用户实测）：512MB 实例跑长代理会话（Claude Code 等
harness），~40 条请求后进程被 OOMKiller 杀掉，重启后数据全部清零。

根因分解与对应修复：

- OOM：基线 RSS ~68MB + model_turns 回放缓存默认上限 64MB + 长会话每请求
  多份正文拷贝 + ``_digest`` 对历史媒体整份 ``.encode()`` 的瞬时拷贝 +
  glibc 堆高水位不归还 → 撞 512MB 限额 → SIGKILL。
  修复：cgroup 内存预算探测（detect_memory_budget_mb）→ 缓存上限分档
  （model_turns._turn_limits）→ 媒体哈希分块（_hash_media_b64）→ 大请求
  收尾 malloc_trim 归还堆碎片（_release_allocator_memory）。
- 清零：Render 免费档根文件系统为临时盘（overlay），OOM 崩溃重启与
  重新部署一样清空 /app/data。落盘写入本身崩溃安全（SQLite WAL 即时
  提交），清零根因是"盘没了"。修复：启动时检测并告警
  （is_ephemeral_data_dir / path_mount_info），部署文档给出持久盘方案。
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aistudio_api.config import (
    detect_memory_budget_mb,
    is_ephemeral_data_dir,
    path_mount_info,
)
from aistudio_api.infrastructure.gateway import model_turns
from aistudio_api.infrastructure.gateway.model_turns import (
    ModelTurnHistory,
    _hash_media_b64,
    _turn_limits,
)

# cgroup 限额与 /proc/mounts 只存在于 Linux（部署目标是 Docker/Termux）；
# Windows 上没有这些伪文件系统，相关探测函数按设计恒返回 None/("","")。
_linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="cgroup / /proc/mounts 仅存在于 Linux"
)

# ---------------------------------------------------------------------------
# Fix 1a：cgroup 内存预算探测
# ---------------------------------------------------------------------------


def _fake_cgroup_files(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> None:
    """把 detect_memory_budget_mb 的 cgroup 路径替换为内存中的假内容。

    config 侧用 ``Path(...).open(...)`` 读 cgroup/挂载表（ruff PTH123 约定），
    故补丁挂在 ``Path.open`` 上。
    """
    real_open = Path.open

    def fake_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        key = str(self)
        if key in files:
            return io.StringIO(files[key])
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)


def test_memory_budget_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AISTUDIO_MEMORY_BUDGET_MB", "512")
    assert detect_memory_budget_mb() == 512


@_linux_only
def test_memory_budget_cgroup_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AISTUDIO_MEMORY_BUDGET_MB", raising=False)
    _fake_cgroup_files(monkeypatch, {"/sys/fs/cgroup/memory.max": "536870912\n"})
    assert detect_memory_budget_mb() == 512


@_linux_only
def test_memory_budget_cgroup_v2_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AISTUDIO_MEMORY_BUDGET_MB", raising=False)
    # v2 "max" → 回退 v1；v1 未设限（页计数器巨值）→ 无预算
    _fake_cgroup_files(
        monkeypatch,
        {
            "/sys/fs/cgroup/memory.max": "max\n",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(1 << 50),
        },
    )
    assert detect_memory_budget_mb() is None


@_linux_only
def test_memory_budget_cgroup_v1_huge_limit_means_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AISTUDIO_MEMORY_BUDGET_MB", raising=False)
    _fake_cgroup_files(
        monkeypatch,
        {"/sys/fs/cgroup/memory/memory.limit_in_bytes": str(1 << 50)},
    )
    # v1 未设限时 limit 为页计数器巨值：视为无预算
    assert detect_memory_budget_mb() is None


@_linux_only
def test_memory_budget_invalid_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISTUDIO_MEMORY_BUDGET_MB", "not-a-number")
    _fake_cgroup_files(monkeypatch, {"/sys/fs/cgroup/memory.max": "1073741824\n"})
    assert detect_memory_budget_mb() == 1024


# ---------------------------------------------------------------------------
# Fix 1b：model_turns 缓存上限分档
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_turn_limits_cache():
    """隔离 lru_cache：每个用例后清空，避免污染其它用例的档位解析。"""
    yield
    _turn_limits.cache_clear()


def _set_budget(monkeypatch: pytest.MonkeyPatch, budget: int | None) -> None:
    monkeypatch.delenv("AISTUDIO_MEMORY_BUDGET_MB", raising=False)
    monkeypatch.delenv("AISTUDIO_MODEL_TURN_CACHE_MB", raising=False)
    monkeypatch.setattr(model_turns, "detect_memory_budget_mb", lambda: budget)
    _turn_limits.cache_clear()


def test_turn_limits_default_without_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_budget(monkeypatch, None)
    assert _turn_limits() == (64 * 1024 * 1024, 1000, 4 * 1024 * 1024)


def test_turn_limits_small_container_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """512MB 档：64MB 回放缓存对请求峰值的挤占是 OOM 构成之一，缩至 12MB。"""
    _set_budget(monkeypatch, 512)
    cache, entries, turn = _turn_limits()
    assert cache == 12 * 1024 * 1024
    assert entries == 300
    assert turn == 1024 * 1024


def test_turn_limits_1gb_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_budget(monkeypatch, 1000)
    cache, entries, turn = _turn_limits()
    assert cache == 32 * 1024 * 1024
    assert entries == 500
    assert turn == 2 * 1024 * 1024


def test_turn_limits_above_1gb_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_budget(monkeypatch, 2048)
    assert _turn_limits() == (64 * 1024 * 1024, 1000, 4 * 1024 * 1024)


def test_turn_limits_env_override_scales_proportionally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISTUDIO_MODEL_TURN_CACHE_MB", "8")
    _turn_limits.cache_clear()
    cache, entries, turn = _turn_limits()
    assert cache == 8 * 1024 * 1024
    # 比例折算（8/64）：条数 1000→125、单条 4MB→512KB
    assert entries == 125
    assert turn == 512 * 1024


def test_turn_limits_env_override_keeps_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AISTUDIO_MODEL_TURN_CACHE_MB", "1")
    _turn_limits.cache_clear()
    _, entries, turn = _turn_limits()
    assert entries == 64
    assert turn == 256 * 1024


def test_model_turn_history_uses_tier_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_budget(monkeypatch, 512)
    history = ModelTurnHistory()
    assert history._max_bytes == 12 * 1024 * 1024
    assert history._max_entries == 300
    assert history._max_turn_bytes == 1024 * 1024


# ---------------------------------------------------------------------------
# Fix 2：_digest 媒体哈希分块（与整串 encode 结果恒等）
# ---------------------------------------------------------------------------


def test_hash_media_b64_matches_whole_encode() -> None:
    data = "c2lnLXZhbGlkLWJhc2U2NC1zaWduYXR1cmU=" * 377  # 非 64KB 整数倍
    assert _hash_media_b64(data) == hashlib.sha256(data.encode()).hexdigest()


def test_hash_media_b64_empty_and_ascii_only() -> None:
    assert _hash_media_b64("") == hashlib.sha256(b"").hexdigest()
    # base64 字母表全 ASCII：分块 encode 与整串 encode 恒等的前提
    data = "Aa1+/" * 12345
    assert _hash_media_b64(data) == hashlib.sha256(data.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fix 3：大请求收尾后的 malloc_trim 归还
# ---------------------------------------------------------------------------


@pytest.fixture
def trim_state(monkeypatch: pytest.MonkeyPatch):
    """重置 trim 模块级状态并替换 malloc_trim 为调用记录器。"""
    from aistudio_api.api import request_middleware as mw

    calls: list[int] = []

    def _recorder(pad: int) -> int:
        calls.append(pad)
        return 1

    monkeypatch.setattr(mw, "_malloc_trim_enabled", None)
    monkeypatch.setattr(mw, "_last_trim_monotonic", 0.0)
    monkeypatch.setattr(mw, "_resolve_malloc_trim", lambda: _recorder)
    monkeypatch.setattr(mw, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.delenv("AISTUDIO_MALLOC_TRIM", raising=False)
    return mw, calls


def test_trim_triggers_on_large_request(trim_state) -> None:
    mw, calls = trim_state
    mw._release_allocator_memory(2 * 1024 * 1024, 0)
    assert calls == [0]


def test_trim_skipped_below_threshold(trim_state) -> None:
    mw, calls = trim_state
    mw._release_allocator_memory(1024, 4096)
    assert calls == []


def test_trim_throttled_within_interval(trim_state) -> None:
    mw, calls = trim_state
    mw._release_allocator_memory(2 * 1024 * 1024, 0)
    mw._release_allocator_memory(3 * 1024 * 1024, 0)
    assert calls == [0]


def test_trim_disabled_by_env(trim_state, monkeypatch: pytest.MonkeyPatch) -> None:
    mw, calls = trim_state
    monkeypatch.setenv("AISTUDIO_MALLOC_TRIM", "0")
    mw._release_allocator_memory(2 * 1024 * 1024, 0)
    assert calls == []


def test_real_malloc_trim_safe_when_available() -> None:
    """本机存在 malloc_trim 符号（glibc）时直接调用必须安全无异常。"""
    import ctypes

    try:
        libc = ctypes.CDLL(None)
        libc.malloc_trim(0)
    except (OSError, AttributeError, TypeError):
        # TypeError: 非 POSIX 平台（如 Windows）的 ctypes.CDLL(None) 直接抛
        # TypeError，而不是 OSError/AttributeError。
        pytest.skip("本机 libc 无 malloc_trim（musl 等）")


# ---------------------------------------------------------------------------
# Fix 4：数据目录临时文件系统检测（重启清零根因的启动告警）
# ---------------------------------------------------------------------------


def _fake_mounts(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    real_open = Path.open

    def fake_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if str(self) == "/proc/mounts":
            return io.StringIO(content)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)


@_linux_only
def test_ephemeral_data_dir_on_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    import aistudio_api.config as config

    _fake_mounts(
        monkeypatch,
        "overlay / overlay rw,relatime 0 0\ntmpfs /tmp tmpfs rw 0 0\n",
    )
    monkeypatch.setattr(config, "resolve_data_dir", lambda: Path("/app/data"))
    assert is_ephemeral_data_dir() is True


@_linux_only
def test_persistent_data_dir_on_ext4(monkeypatch: pytest.MonkeyPatch) -> None:
    import aistudio_api.config as config

    _fake_mounts(
        monkeypatch,
        "overlay / overlay rw,relatime 0 0\n/dev/sda1 /data ext4 rw 0 0\n",
    )
    monkeypatch.setattr(config, "resolve_data_dir", lambda: Path("/data/requests"))
    assert is_ephemeral_data_dir() is False


@_linux_only
def test_mount_info_longest_prefix_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_mounts(
        monkeypatch,
        "overlay / overlay rw 0 0\n/dev/vdb /var/lib/render ext4 rw 0 0\n",
    )
    mount, fs = path_mount_info(Path("/var/lib/render/data"))
    assert mount == "/var/lib/render"
    assert fs == "ext4"


@_linux_only
def test_mount_info_escapes_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_mounts(
        monkeypatch,
        "/dev/vdb /mnt/with\\040space ext4 rw 0 0\n",
    )
    mount, _ = path_mount_info(Path("/mnt/with space/data"))
    assert mount == "/mnt/with space"


def test_mount_info_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = Path.open

    def fake_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if str(self) == "/proc/mounts":
            raise OSError("no mounts")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    assert path_mount_info(Path("/anywhere")) == ("", "")
