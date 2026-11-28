"""第三轮深审修复回归测试（P2-1/2、P3-3~10）。"""

from __future__ import annotations

import asyncio
import json
import os
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
async def test_local_signer_concurrent_client_init() -> None:
    """并发首调只建一个 AsyncClient。"""
    from aistudio_api.infrastructure.waa.signer import LocalProofSigner

    rs = LocalProofSigner()
    clients = await asyncio.gather(*[rs._get_client() for _ in range(20)])
    assert len({id(c) for c in clients}) == 1
    await rs.close()


# ─── P3-5: 流式 Set-Cookie 后台持久化 ───


@pytest.mark.asyncio
async def test_streaming_set_cookie_persist_off_critical_path() -> None:
    """status 事件不等落盘；close 前冲刷保证持久化执行。"""
    from unittest.mock import patch

    import httpx

    from aistudio_api.infrastructure.gateway import transport as tr

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"chunk"

    class FakeClient:
        def build_request(self, *a, **k):
            return httpx.Request(*a, **k)

        async def send(self, *a, **k):
            return httpx.Response(200, headers={"set-cookie": "SIDCC=x"}, stream=Body())

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
    try:
        assert not [s for s in stmts if "CREATE" in s.upper()]
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 4
    finally:
        # 显式关闭：不关闭时 sqlite3.Connection.__del__ 在 GC 期抛
        # ResourceWarning: unclosed database，在 filterwarnings=error 下变成失败。
        conn.close()
        h.close()
        h2.close()


# ─── 日志归档分页：按全体匹配记录的时间排序，返回精确总数 ───


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_read_log_history_filtered_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    from aistudio_api.infrastructure.utils import log_files as lf

    monkeypatch.setattr(lf, "log_directory", lambda: tmp_path)

    def line(ts: int, label: str, **override: str) -> str:
        return json.dumps(
            {
                "ts_ms": ts,
                "label": label,
                "message": "Needle",
                "level": "WARNING",
                "category": "model",
                "model": "models/m",
                "request_id": "r",
                "account_id": "a",
            }
            | override
        )

    (tmp_path / "runtime.jsonl.1").write_text(
        "\n".join([line(30, "old tie"), line(90, "highest in backup")]),
        encoding="utf-8",
    )
    current = [
        line(10, "low"),
        line(30, "tie A"),
        line(30, "tie B"),
        line(20, "middle"),
        line(99, "wrong level", level="INFO"),
        line(99, "wrong model", model="m2"),
        line(99, "wrong id", request_id="r2"),
        line(99, "wrong account", account_id="a2"),
        line(99, "wrong category", category="system"),
        line(99, "no hit", message="other"),
    ]
    (tmp_path / "runtime.jsonl").write_bytes(
        b"[]\n\xff\n{broken\n" + "\n".join(current).encode()
    )

    def labels(page: int, size: int) -> list[object]:
        result = lf.read_log_history(
            page=page,
            page_size=size,
            order=order,
            level="WARNING",
            category="model",
            model="m",
            account_id="a",
            request_id="r",
            search="needle",
        )
        assert result["total"] == 6
        assert result["total_truncated"] is False
        return [row["label"] for row in cast(list[dict[str, object]], result["items"])]

    expected = ["low", "middle", "old tie", "tie A", "tie B", "highest in backup"]
    if order == "desc":
        expected.reverse()
    assert [label for i in range(1, 7) for label in labels(i, 1)] == expected
    assert labels(1, 4) + labels(2, 4) == expected
    assert labels(10**40, 100) == []


@pytest.mark.asyncio
async def test_archive_history_next_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from fastapi import FastAPI

    from aistudio_api.api.routes_operations import router
    from aistudio_api.infrastructure.utils import log_files as lf

    monkeypatch.setattr(lf, "log_directory", lambda: tmp_path)
    (tmp_path / "runtime.jsonl").write_text(
        "\n".join(json.dumps({"ts_ms": i, "category": "model"}) for i in range(101)),
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        first = (await client.get("/api/logs/history")).raise_for_status().json()
        second = (
            (await client.get("/api/logs/history?page=2")).raise_for_status().json()
        )
    assert first["page"] * first["page_size"] < first["total"]  # 面板的“下一页”可用
    assert first["total"] == second["total"] == 101
    assert first["total_truncated"] is second["total_truncated"] is False
    assert [row["ts_ms"] for row in first["items"] + second["items"]] == list(
        range(100, -1, -1)
    )


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX open-file rename")
def test_archive_history_reads_page_from_original_file_after_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aistudio_api.infrastructure.utils import log_files as lf, logger

    monkeypatch.setattr(lf, "log_directory", lambda: tmp_path)
    current = tmp_path / "runtime.jsonl"
    rows = [
        {"ts_ms": 20, "message": "日志一"},
        {"ts_ms": 30, "message": "日志二"},
        {"ts_ms": 10, "message": "日志三"},
    ]
    current.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    matches_log_entry = logger.matches_log_entry

    def match_during_rotation(entry: dict[str, object], **filters: str) -> bool:
        if entry == rows[-1]:
            current.rename(current.with_suffix(".jsonl.1"))
            current.write_text('{"ts_ms":40,"message":"new file"}\n', encoding="utf-8")
        return matches_log_entry(entry, **filters)

    monkeypatch.setattr(logger, "matches_log_entry", match_during_rotation)
    result = lf.read_log_history(page_size=1)
    assert result["total"] == 3
    assert result["items"] == [rows[1]]


def test_archive_history_nonfinite_legacy_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aistudio_api.infrastructure.utils import log_files as lf

    monkeypatch.setattr(lf, "log_directory", lambda: tmp_path)
    (tmp_path / "runtime.jsonl").write_text(
        "\n".join(
            [
                '{"ts_ms":1e309}',
                '{"ts_ms":Infinity}',
                '{"ts_ms":"-Infinity"}',
                '{"ts_ms":"2.5"}',
            ]
        ),
        encoding="utf-8",
    )
    result = lf.read_log_history()
    assert result["total"] == 4
    assert [row["ts_ms"] for row in cast(list[dict[str, object]], result["items"])] == [
        "2.5",
        "-Infinity",
        None,
        None,
    ]
    json.dumps(result, allow_nan=False)


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
