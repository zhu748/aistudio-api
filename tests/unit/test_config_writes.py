"""config.yaml 写入路径的原子性与并发读-改-写（RMW）安全测试。

背景：``PUT /config/yaml`` 与三个 API Key 端点都是「读整份 YAML → 改内存 →
整文件覆盖」。若直接 ``write_text``，进程被中断会留下半截配置（下次启动解析
失败），并发请求则会互相抹掉对方的改动（最后写入者获胜）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from aistudio_api.api import routes_system
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.infrastructure.gateway import model_defaults
from aistudio_api.infrastructure.utils.common import (
    atomic_write_json,
    atomic_write_text,
)

# ========== atomic_write_text ==========


def test_atomic_write_text_writes_utf8_content(tmp_path: Path) -> None:
    """应写入 UTF-8 文本且内容完全一致（含中文与非 ASCII 标点）。"""
    target = tmp_path / "sub" / "config.yaml"
    atomic_write_text(target, "logging:\n  level: INFO\n# 中文注释 ✓\n")

    assert target.exists()
    assert "中文注释" in target.read_text(encoding="utf-8")


def test_atomic_write_text_replaces_existing_file(tmp_path: Path) -> None:
    """覆盖已有文件时应替换而非追加。"""
    target = tmp_path / "config.yaml"
    atomic_write_text(target, "first: 1\n")
    atomic_write_text(target, "second: 2\n")

    assert target.read_text(encoding="utf-8") == "second: 2\n"


def test_atomic_write_text_leaves_no_temp_files(tmp_path: Path) -> None:
    """成功写入后不应残留 .tmp.* 临时文件。"""
    target = tmp_path / "config.yaml"
    atomic_write_text(target, "ok: true\n")

    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_atomic_write_text_failure_keeps_original_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """替换阶段失败时应保留原内容，并清理临时文件、向上抛出异常。"""
    target = tmp_path / "config.yaml"
    atomic_write_text(target, "original: 1\n")

    def _boom(self: Path, _other: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", _boom, raising=True)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "new: 2\n")

    assert target.read_text(encoding="utf-8") == "original: 1\n"
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_atomic_write_json_remains_valid_json(tmp_path: Path) -> None:
    """atomic_write_json 与 atomic_write_text 共用同一临时文件策略，内容须可解析。"""
    target = tmp_path / "state.json"
    atomic_write_json(target, {"accounts": {"a": {"tier": "Pro"}}})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "accounts": {"a": {"tier": "Pro"}}
    }
    assert [p.name for p in tmp_path.iterdir() if ".tmp." in p.name] == []


# ========== config.yaml 读-改-写 ==========


@pytest.fixture
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 _resolve_config_path 指向临时文件，避免污染仓库 config.yaml。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump({"api_keys": [], "logging": {"level": "INFO"}}, allow_unicode=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes_system, "_resolve_config_path", lambda _p: config_path)
    monkeypatch.setattr(routes_system, "invalidate_config_cache", lambda: None)
    return config_path


def _read_keys(config_path: Path) -> list[str]:
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return [str(item.get("key")) for item in parsed.get("api_keys") or []]


def _patch_slow_write(monkeypatch: pytest.MonkeyPatch, delay: float = 0.05) -> None:
    """给 atomic_write_text 加延时，放大「读-改-写」的写窗口。

    无锁时所有线程都会在写之前读到同一份旧配置，最终只剩最后一个线程的
    改动；有锁时临界区串行，所有改动都会保留。
    """
    original_write = routes_system.atomic_write_text

    def _slow(path: Path | str, text: str) -> None:
        time.sleep(delay)
        original_write(path, text)

    monkeypatch.setattr(routes_system, "atomic_write_text", _slow)


def test_threaded_create_api_keys_keeps_all_entries(
    _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8 个线程并发创建必须全部落盘：无锁 RMW 时只有最后一个线程的改动会保留。"""
    _patch_slow_write(monkeypatch)
    errors: list[Exception] = []

    def _create(index: int) -> None:
        try:
            asyncio.run(
                routes_system.create_api_key(
                    routes_system.CreateApiKeyRequest(
                        key=f"sk-thread-{index:02d}", name=f"key-{index}"
                    )
                )
            )
        except Exception as exc:  # 线程内异常带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_create, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    keys = _read_keys(_isolated_config)
    assert sorted(keys) == sorted(f"sk-thread-{index:02d}" for index in range(8))


@pytest.mark.asyncio
async def test_threaded_delete_and_create_do_not_clobber(
    _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除与创建并发时不应互抹：最终应包含所有创建结果且不含被删项。"""
    # 注意：不要在主线程用 asyncio.run() 跑 setup——它会清掉当前线程的
    # event loop，导致 pytest-asyncio 保存的 loop 成为孤儿（GC 时报
    # "unclosed event loop"）。auto 模式下直接 await 即可。
    for index in range(4):
        await routes_system.create_api_key(
            routes_system.CreateApiKeyRequest(key=f"sk-old-{index}", name="old")
        )

    _patch_slow_write(monkeypatch)
    errors: list[Exception] = []

    def _delete_old() -> None:
        try:
            asyncio.run(routes_system.delete_api_key("sk-old-0"))
        except Exception as exc:
            errors.append(exc)

    def _create_new(key: str) -> None:
        try:
            asyncio.run(
                routes_system.create_api_key(
                    routes_system.CreateApiKeyRequest(key=key, name="new")
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_delete_old),
        threading.Thread(target=_create_new, args=("sk-new-1",)),
        threading.Thread(target=_create_new, args=("sk-new-2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    keys = _read_keys(_isolated_config)
    assert "sk-old-0" not in keys
    assert "sk-new-1" in keys and "sk-new-2" in keys
    assert len(keys) == 5


@pytest.mark.asyncio
async def test_update_api_key_name_is_atomic_and_preserves_other_fields(
    _isolated_config: Path,
) -> None:
    """改名只应影响目标 Key 的 name，其余字段与同级配置块保持不变。"""
    await routes_system.create_api_key(
        routes_system.CreateApiKeyRequest(key="sk-target-abc", name="旧名")
    )

    updated = await routes_system.update_api_key_name(
        "sk-target-abc", routes_system.UpdateApiKeyRequest(name="新名")
    )

    assert updated.name == "新名"
    parsed = yaml.safe_load(_isolated_config.read_text(encoding="utf-8"))
    assert parsed["logging"]["level"] == "INFO"
    entries = {item["key"]: item for item in parsed["api_keys"]}
    assert entries["sk-target-abc"]["name"] == "新名"
    assert [
        p.name for p in _isolated_config.parent.iterdir() if ".tmp." in p.name
    ] == []


# ========== 调度器状态落盘频率 ==========


def test_record_auth_error_defers_write_to_debounce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """403 计数走防抖而非同步落盘：单请求 5 次重试不应同步写盘 5 次。"""
    rotator = AccountRotator.__new__(AccountRotator)
    rotator._stats = {}
    calls: list[str] = []
    monkeypatch.setattr(
        AccountRotator,
        "_schedule_debounced_save",
        lambda self: calls.append("debounced"),
    )
    monkeypatch.setattr(
        AccountRotator, "save_state", lambda self: calls.append("immediate")
    )

    rotator.record_auth_error("acc_1", model="gemini-3.7-flash")

    assert calls == ["debounced"]


def test_record_rate_limited_saves_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 冷却直接决定下一次选路，必须立即落盘。"""
    rotator = AccountRotator.__new__(AccountRotator)
    rotator._stats = {}
    calls: list[str] = []
    monkeypatch.setattr(
        AccountRotator,
        "_schedule_debounced_save",
        lambda self: calls.append("debounced"),
    )
    monkeypatch.setattr(
        AccountRotator, "save_state", lambda self: calls.append("immediate")
    )

    rotator.record_rate_limited("acc_1", model="gemini-3.7-flash")

    assert calls == ["immediate"]


# ========== 跨模块共享写锁 ==========


def test_config_write_lock_is_shared_between_routes_and_model_defaults() -> None:
    """路由端点与 model_defaults 的配置写函数必须共用同一把锁。

    两边各持一把锁时，面板保存 YAML 与切换 WAA 节点并发仍会互相覆盖。
    """
    assert routes_system.config_file_write_lock is model_defaults.config_file_write_lock


@pytest.mark.asyncio
async def test_cross_module_config_writes_do_not_clobber(
    _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路由写 API Key 与 model_defaults 写 anti_truncation 并发时不得互相覆盖。"""
    monkeypatch.setattr(
        model_defaults, "_resolve_config_path", lambda _p: _isolated_config
    )
    monkeypatch.setattr(model_defaults, "invalidate_config_cache", lambda: None)

    await asyncio.gather(
        *(
            routes_system.create_api_key(
                routes_system.CreateApiKeyRequest(key=f"sk-cross-{index:02d}", name="x")
            )
            for index in range(4)
        ),
        *(
            asyncio.to_thread(
                model_defaults.update_anti_truncation_settings, enabled=True
            )
            for _ in range(3)
        ),
    )

    parsed = yaml.safe_load(_isolated_config.read_text(encoding="utf-8"))
    keys = {str(item.get("key")) for item in parsed.get("api_keys") or []}
    assert {f"sk-cross-{index:02d}" for index in range(4)} <= keys
    assert parsed["anti_truncation"]["enabled"] is True
