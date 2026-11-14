"""Persistence coalesces concurrent changes without retaining request payloads."""

import asyncio
import gc
import threading
import time
import weakref
from contextvars import ContextVar
from unittest.mock import MagicMock

import pytest

from aistudio_api.api.state import RuntimeState
from aistudio_api.application.account_rotator import AccountRotator, AccountStats
from aistudio_api.config import settings


@pytest.mark.parametrize("mode", ["runtime", "rotator_immediate", "rotator_delayed"])
async def test_save_task_does_not_keep_the_initiating_request_alive(monkeypatch, mode):
    monkeypatch.setattr(settings, "persist_stats", True)
    monkeypatch.setattr(settings, "persist_rotator", True)
    if mode == "runtime":
        owner = RuntimeState()
        monkeypatch.setattr(owner, "_persist_stats", lambda payload: None)
    else:
        store = MagicMock()
        store.list_accounts.return_value = []
        owner = AccountRotator(store)
        monkeypatch.setattr(owner, "_persist_state", lambda payload: None)
    owner._last_saved_time = time.time() if mode == "rotator_delayed" else 0
    variable: ContextVar[object | None] = ContextVar("request_payload", default=None)

    class Payload:
        pass

    payload = Payload()
    reference = weakref.ref(payload)
    token = variable.set(payload)
    try:
        owner._schedule_debounced_save(delay=0)
    finally:
        variable.reset(token)
    del payload
    try:
        gc.collect()
        assert reference() is None
    finally:
        assert owner._save_task is not None
        await owner._save_task


@pytest.mark.parametrize("delayed", [False, True])
async def test_rotator_persists_updates_arriving_during_a_write(monkeypatch, delayed):
    monkeypatch.setattr(settings, "persist_rotator", True)
    store = MagicMock()
    store.list_accounts.return_value = []
    rotator = AccountRotator(store)
    stats = AccountStats(account_id="a", total_requests=1)
    rotator._stats["a"] = stats
    rotator._last_saved_time = time.time() if delayed else 0
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    writes = []

    def persist(payload):
        writes.append(payload)
        if len(writes) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)

    monkeypatch.setattr(rotator, "_persist_state", persist)
    rotator._schedule_debounced_save(delay=0)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rotator.record_success("a", "test")
    finally:
        release.set()
    assert rotator._save_task is not None
    await asyncio.wait_for(rotator._save_task, 5)
    assert writes[-1]["a"]["total_requests"] == 2
    assert writes[-1]["a"]["total_success"] == 1


@pytest.mark.parametrize("mode", ["runtime", "rotator"])
async def test_cancelled_save_drains_its_worker_before_shutdown_can_write_again(
    monkeypatch, mode
):
    monkeypatch.setattr(settings, "persist_stats", True)
    monkeypatch.setattr(settings, "persist_rotator", True)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()

    def persist(payload):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        finished.set()

    if mode == "runtime":
        owner = RuntimeState()
        monkeypatch.setattr(owner, "_persist_stats", persist)
    else:
        store = MagicMock()
        store.list_accounts.return_value = []
        owner = AccountRotator(store)
        monkeypatch.setattr(owner, "_persist_state", persist)
    owner._schedule_debounced_save(delay=0)
    await asyncio.wait_for(entered.wait(), 2)
    task = owner._save_task
    assert task is not None
    task.cancel()
    try:
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
