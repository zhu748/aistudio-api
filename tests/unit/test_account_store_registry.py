"""账号注册表（registry.json）损坏时的隔离行为测试。

历史缺陷：``_load_registry`` 在解析失败时静默把注册表置空，随后任意一次
``_save_registry`` 都会把 ``{"accounts": {}}`` 写回磁盘，导致用户全部账号永久丢失。
现在改为：隔离坏文件（改名备份）后抛错，拒绝静默清空。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aistudio_api.infrastructure.account.account_store import AccountStore


def _write_store_with_registry(tmp_path: Path, content: str) -> AccountStore:
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / "registry.json").write_text(content, encoding="utf-8")
    return AccountStore(accounts_dir=accounts_dir)


def test_missing_registry_starts_empty(tmp_path: Path) -> None:
    """首次启动（无 registry.json）必须正常工作，不能被隔离逻辑误伤。"""
    store = AccountStore(accounts_dir=tmp_path / "fresh_accounts")
    assert store.list_accounts() == []


def test_corrupt_registry_is_quarantined_and_raises(tmp_path: Path) -> None:
    store = _write_store_with_registry(tmp_path, "{ this is not valid json")
    with pytest.raises(RuntimeError, match=r"registry\.json 损坏"):
        store.list_accounts()

    accounts_dir = tmp_path / "accounts"
    corrupt = list(accounts_dir.glob("registry.corrupt.*.json"))
    assert len(corrupt) == 1, (
        f"坏文件应被改名隔离，实际目录内容: {list(accounts_dir.iterdir())}"
    )
    # 原始文件已被移走，避免后续写入覆盖证据
    assert not (accounts_dir / "registry.json").exists()
    # 备份内容保留，便于人工恢复
    assert "not valid json" in corrupt[0].read_text(encoding="utf-8")


def test_corrupt_registry_is_not_silently_reset(tmp_path: Path) -> None:
    """核心不变量：损坏后绝不能写出一个空的 registry.json。"""
    store = _write_store_with_registry(tmp_path, "[]")  # 合法 JSON 但不是对象
    with pytest.raises(RuntimeError):
        store.list_accounts()
    accounts_dir = tmp_path / "accounts"
    assert not (accounts_dir / "registry.json").exists()


def test_valid_registry_loads_normally(tmp_path: Path) -> None:
    payload = {"accounts": {}}
    store = _write_store_with_registry(tmp_path, json.dumps(payload))
    assert store.list_accounts() == []
    # 合法文件不应被隔离
    accounts_dir = tmp_path / "accounts"
    assert (accounts_dir / "registry.json").exists()
    assert not list(accounts_dir.glob("registry.corrupt.*.json"))


def test_corrupt_registry_keeps_failing_on_subsequent_calls(tmp_path: Path) -> None:
    """损坏判定必须「粘住」：第二次调用不能退化成首次启动而放行空注册表。

    历史缺陷：首次调用隔离并抛错后文件已不存在，第二次调用走 `exists()` 的
    首次启动分支直接返回空注册表，随后一次 `_save_registry` 就会把
    `{"accounts": {}}` 写回磁盘 —— 隔离的意义被完全抵消。
    """
    store = _write_store_with_registry(tmp_path, "{ broken")

    with pytest.raises(RuntimeError, match=r"registry\.json 损坏"):
        store.list_accounts()
    with pytest.raises(RuntimeError, match="此前已判定损坏"):
        store.list_accounts()

    accounts_dir = tmp_path / "accounts"
    assert not (accounts_dir / "registry.json").exists()


def test_quarantine_marker_survives_restart(tmp_path: Path) -> None:
    """隔离备份是持久标记：重启（新实例）后仍须拒绝，不能当首次启动写回空注册表。

    历史缺陷：`_corrupt_registry` 是纯内存标志，进程重启后 `registry.json`
    不存在会走 exists() 的首次启动分支，随后一次 `_save_registry` 就把
    `{"accounts": {}}` 写回，隔离形同虚设。
    """
    store = _write_store_with_registry(tmp_path, "{ broken")
    with pytest.raises(RuntimeError):
        store.list_accounts()

    accounts_dir = tmp_path / "accounts"
    # 模拟进程重启：绕过单例缓存，构造一个全新的 store 实例
    revived = object.__new__(AccountStore)
    AccountStore.__init__(revived, accounts_dir)
    with pytest.raises(RuntimeError, match="已被隔离但尚未人工恢复"):
        revived.list_accounts()
    assert not (accounts_dir / "registry.json").exists()


def test_save_account_preserves_existing_email_when_none(tmp_path: Path) -> None:
    """环境变量注入账号每次启动用 email=None 重建，不能抹掉已探活的邮箱。"""
    store = AccountStore(accounts_dir=tmp_path / "accounts")
    first = store.save_account(
        name="env",
        email="detected@example.com",
        storage_state={"cookies": []},
        account_id="acc_env_1",
    )
    assert first.email == "detected@example.com"

    again = store.save_account(
        name="env",
        email=None,
        storage_state={"cookies": []},
        account_id="acc_env_1",
    )
    assert again.email == "detected@example.com"

    fresh = store.save_account(name="fresh", email=None, storage_state={"cookies": []})
    assert fresh.email is None
