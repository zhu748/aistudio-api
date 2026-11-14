"""第三轮深审修复回归测试（P2-1/2、P3-3~10）。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import cast

import pytest

# ─── P2-1: AccountStore 读-改-写方法全程持锁 ───


def test_account_store_update_methods_hold_lock(tmp_path: Path) -> None:
    """无锁方法与持锁方法并发不再抛 dict changed size。"""
    from aistudio_api.infrastructure.account.account_store import AccountStore

    store = AccountStore(accounts_dir=tmp_path / "accounts")
    ids = [
        store.save_account(
            name=f"a{i:03d}", email=None, storage_state={"SAPISID": f"x{i}"}
        ).id
        for i in range(120)
    ]

    stop = threading.Event()
    errors: list[tuple[str, str]] = []

    def writer() -> None:
        for aid in ids[:60]:
            if stop.is_set():
                return
            try:
                store.delete_account(aid)
            except Exception as e:  # pragma: no cover
                errors.append(("writer", f"{type(e).__name__}: {e}"))
                stop.set()
                return

    def reader() -> None:
        for _ in range(4):
            for aid in ids[60:]:
                if stop.is_set():
                    return
                try:
                    store.update_account_tier(aid, "Pro")
                    store.get_active_account()
                    store.set_active_account(aid)
                except Exception as e:  # pragma: no cover
                    errors.append(("reader", f"{type(e).__name__}: {e}"))
                    stop.set()
                    return

    t1, t2 = threading.Thread(target=writer), threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    stop.set()
    assert not errors, errors[:3]


def test_get_active_account_lock_contended_with_save(tmp_path: Path) -> None:
    """每请求热路径 get_active_account 与 save_account 并发不崩。"""
    from aistudio_api.infrastructure.account.account_store import AccountStore

    store = AccountStore(accounts_dir=tmp_path / "accounts")
    store.save_account(name="seed", email=None, storage_state={"SAPISID": "x"})

    stop = threading.Event()
    errors: list[str] = []

    def hot_path() -> None:
        while not stop.is_set():
            try:
                store.get_active_account()
            except Exception as e:  # pragma: no cover
                errors.append(f"{type(e).__name__}: {e}")
                stop.set()

    def saver() -> None:
        i = 0
        while not stop.is_set():
            try:
                store.save_account(
                    name=f"s{i}", email=None, storage_state={"SAPISID": f"y{i}"}
                )
                i += 1
            except Exception as e:  # pragma: no cover
                errors.append(f"saver {type(e).__name__}: {e}")
                stop.set()

    threads = [threading.Thread(target=hot_path), threading.Thread(target=saver)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors[:3]


# ─── P2-2: presets.json 损坏防护 ───


def test_corrupt_presets_quarantined_not_silently_reset(tmp_path: Path) -> None:
    from aistudio_api.infrastructure.account.account_store import AccountStore

    store = AccountStore(accounts_dir=tmp_path / "accounts")
    presets_path = store._presets_path
    presets_path.write_text("{broken json!!!", encoding="utf-8")

    # 清单例缓存模拟真实重启
    AccountStore._instances.clear()
    store2 = AccountStore(accounts_dir=tmp_path / "accounts")
    with pytest.raises(RuntimeError, match="损坏"):
        store2.list_presets()

    # 坏文件被隔离（持久标记），不再原地
    assert not presets_path.exists()
    assert list(presets_path.parent.glob(".corrupt.*.presets.json"))

    # 再重启仍拒绝（持久标记生效）
    AccountStore._instances.clear()
    store3 = AccountStore(accounts_dir=tmp_path / "accounts")
    with pytest.raises(RuntimeError):
        store3.list_presets()


def test_clean_presets_still_work(tmp_path: Path) -> None:
    from aistudio_api.infrastructure.account.account_store import (
        AccountPreset,
        AccountStore,
    )

    store = AccountStore(accounts_dir=tmp_path / "accounts")
    store.save_preset(AccountPreset(id="custom_1", name="我的预设", description="t"))
    AccountStore._instances.clear()
    store2 = AccountStore(accounts_dir=tmp_path / "accounts")
    ids = [p.id for p in store2.list_presets()]
    assert "custom_1" in ids


# ─── P3-3/4: signer 惰性初始化竞态 ───


@pytest.mark.asyncio
async def test_remote_signer_concurrent_client_init() -> None:
    """并发首调只建一个 AsyncClient。"""
    from aistudio_api.infrastructure.waa.signer import RemoteProofSigner

    rs = RemoteProofSigner()
    clients = await asyncio.gather(*[rs._get_client() for _ in range(20)])
    assert len({id(c) for c in clients}) == 1
    await rs.close()


@pytest.mark.asyncio
async def test_waa_signer_reconfigure_lock_singleton() -> None:
    """并发首调只创建一把 reconfigure 锁。"""
    from aistudio_api.infrastructure.waa.signer import WaaSigner

    w = WaaSigner()
    locks = [w._get_reconfigure_lock() for _ in range(10)]
    locks += await asyncio.gather(
        *[asyncio.to_thread(w._get_reconfigure_lock) for _ in range(10)]
    )
    assert len({id(lk) for lk in locks}) == 1


# ─── P3-5: 流式 Set-Cookie 后台持久化 ───


@pytest.mark.asyncio
async def test_streaming_set_cookie_persist_off_critical_path() -> None:
    """status 事件不等落盘；close 前冲刷保证持久化执行。"""
    from unittest.mock import patch

    import httpx

    from aistudio_api.infrastructure.gateway import transport as tr

    class FakeHeaders:
        def get_list(self, name: str) -> list[str]:
            return ["SIDCC=x"] if name == "set-cookie" else []

    class FakeResp:
        status_code = 200
        headers = FakeHeaders()

        async def aiter_bytes(self):
            yield b"chunk"

        async def aclose(self) -> None:
            pass

    class FakeClient:
        def build_request(self, *a, **k):
            return httpx.Request(*a, **k)

        async def send(self, *a, **k):
            return FakeResp()

    t = tr.HTTPStreamTransport()
    called = threading.Event()

    def slow_persist(set_cookies, auth_file=None, cookies=None):
        time.sleep(0.2)
        called.set()

    with (
        patch.object(tr, "_persist_set_cookies", slow_persist),
        patch.object(tr.HTTPStreamTransport, "_get_client") as gc,
    ):
        gc.return_value = FakeClient()
        t0 = time.perf_counter()
        async for ev in t.send_streaming_request(url="http://u", headers={}, body="b"):
            if ev[0] == "status":
                assert time.perf_counter() - t0 < 0.1  # 不等 200ms 落盘
                break
        await t._flush_cookie_persist()
    assert called.is_set()


# ─── P3-7: request_history schema 一次性 ───


def test_request_history_schema_once(tmp_path: Path) -> None:
    import sqlite3

    from aistudio_api.infrastructure.request_history import RequestHistory

    h = RequestHistory(tmp_path / "h.sqlite3")
    for i in range(3):
        h.insert(
            {
                "request_id": f"r{i}",
                "started_at": time.time(),
                "model": "m",
                "account_id": "a",
                "key_id": "k",
                "result": "ok",
                "status_code": 200,
            }
        )
    assert h._schema_ready

    stmts: list[str] = []
    conn = sqlite3.connect(tmp_path / "h.sqlite3")
    conn.set_trace_callback(stmts.append)
    h2 = RequestHistory(tmp_path / "h.sqlite3")
    h2._schema_ready = True
    h2.insert(
        {
            "request_id": "r9",
            "started_at": time.time(),
            "model": "m",
            "account_id": "a",
            "key_id": "k",
            "result": "ok",
            "status_code": 200,
        }
    )
    assert not [s for s in stmts if "CREATE" in s.upper()]
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 4


# ─── P3-8: 日志查询 desc 提前终止 ───


def test_read_log_history_early_stop(tmp_path: Path) -> None:
    from unittest.mock import patch

    from aistudio_api.infrastructure.utils import log_files as lf

    now = int(time.time() * 1000)

    def line(ts: int, msg: str) -> str:
        return json.dumps(
            {
                "ts_ms": ts,
                "level": "INFO",
                "msg": msg,
                "category": "",
                "model": "",
                "account_id": "",
                "request_id": "",
            }
        )

    (tmp_path / "runtime.jsonl").write_text(
        "\n".join(line(now + i, f"cur{i}") for i in range(500)) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime.jsonl.1").write_text(
        "\n".join(line(now - 5000 + i, f"old{i}") for i in range(500)) + "\n",
        encoding="utf-8",
    )

    read_files: list[str] = []
    real_open = Path.open

    def spy_open(self, *a, **k):
        if "runtime.jsonl" in self.name:
            read_files.append(self.name)
        return real_open(self, *a, **k)

    with (
        patch.object(Path, "open", spy_open),
        patch.object(lf, "log_directory", return_value=tmp_path),
        patch.object(lf, "_queue_handler", None),
    ):
        r = lf.read_log_history(page=1, page_size=100)
        assert len(cast(list[object], r["items"])) == 100
        assert r["total_truncated"] is True
        assert read_files == ["runtime.jsonl"]  # 未读旧备份

        read_files.clear()
        lf.read_log_history(page=1, page_size=100, order="asc")
        assert len(read_files) == 2  # asc 全量读


# ─── P3-9: 转储 stat TOCTOU ───


@pytest.mark.asyncio
async def test_request_payload_file_vanishes_gracefully(tmp_path: Path) -> None:
    from unittest.mock import patch

    from aistudio_api.api import routes_operations as ro

    target = tmp_path / "req_test123.json"
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    with (
        patch.object(ro, "get_dump_dir", return_value=tmp_path),
        patch.object(ro, "request_history") as rh,
        patch.object(ro.settings, "dump_max_bytes", 1024),
    ):
        rh.return_value.get.return_value = {"request_id": "req_test123"}
        r = await ro.request_payload("req_test123")
        assert r["available"] is True

        target.unlink()  # 并发清理窗口
        r2 = await ro.request_payload("req_test123")
        assert r2["available"] is False  # 优雅降级而非 500


# ─── P3-10: 旧代理池退役 ───


@pytest.mark.asyncio
async def test_retire_stale_proxy_clients() -> None:
    from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport

    t = HTTPStreamTransport()
    c_old = await t._get_client("http://old:1")
    c_new = await t._get_client("http://new:2")
    await t.retire_stale_clients("http://new:2")
    assert "http://old:1" not in t._clients
    assert "http://new:2" in t._clients
    assert not c_old.is_closed  # 在途引用安全

    await asyncio.sleep(0.05)  # 延迟任务挂起
    await t.close()
    assert c_old.is_closed and c_new.is_closed
    assert not t._retired_close_tasks
    assert not t._retired_clients


@pytest.mark.asyncio
async def test_retire_then_immediate_close_edge() -> None:
    """retire 后立即 close（延迟任务未启动帧）也必须回收。"""
    from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport

    t = HTTPStreamTransport()
    c = await t._get_client("http://x:1")
    await t.retire_stale_clients("http://y:2")
    await t.close()
    assert c.is_closed
    assert not t._retired_clients


# ─── graceful shutdown 参数 ───


def test_uvicorn_graceful_shutdown_configured() -> None:
    import inspect

    from aistudio_api.api.app import main

    src = inspect.getsource(main)
    assert "timeout_graceful_shutdown" in src
