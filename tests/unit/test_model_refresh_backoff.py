"""Unusable model catalogs use a short retry interval instead of serial refetches."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from aistudio_api.infrastructure.gateway import model_discovery as module


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
async def test_failed_refresh_is_coalesced_and_retried_after_short_ttl(
    monkeypatch, cached, failure
):
    now = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    service = module.ModelDiscoveryService(cache_ttl_seconds=600)
    if cached:
        service._cached_models = [{"id": "existing"}]
    fetch = AsyncMock(return_value=[])
    if failure == "exception":
        fetch.side_effect = RuntimeError("upstream down")
    monkeypatch.setattr(service, "_fetch_via_http", fetch)
    results = await asyncio.gather(
        *(service.get_models(cookies={"SAPISID": "test"}) for _ in range(20))
    )
    assert all(result for result in results)
    assert fetch.await_count == 1
    await service.get_models(force_refresh=True, cookies={"SAPISID": "test"})
    assert fetch.await_count == 2
    now[0] += 61
    await service.get_models(cookies={"SAPISID": "test"})
    assert fetch.await_count == 3


# ───────────── 后台目录周期刷新（2026-11-15 补：被动周期 → 主动周期） ─────────────


async def test_background_refresh_lifecycle(monkeypatch):
    """后台目录刷新：启动即预热、tick 周期唤醒、异常不致死、停止后不再调用。"""
    calls: list[int] = []

    async def fake_get_models(**kwargs):
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            raise RuntimeError("transient upstream error")
        return []

    service = module.ModelDiscoveryService()
    monkeypatch.setattr(service, "get_models", fake_get_models)
    service.start_background_refresh(tick_seconds=0.01)
    task = service._refresh_task
    assert task is not None and not task.done()
    # 幂等：重复启动复用现有任务
    service.start_background_refresh(tick_seconds=0.01)
    assert service._refresh_task is task
    await asyncio.sleep(0.1)
    # 预热 + 周期唤醒（≥4 次），且第 2 次抛异常后循环仍存活
    assert len(calls) >= 4
    assert not task.done()
    before = len(calls)
    await service.stop_background_refresh()
    assert service._refresh_task is None
    await asyncio.sleep(0.05)
    assert len(calls) == before


async def test_stop_background_refresh_without_start_is_noop():
    """从未启动时停止为无操作，不抛异常（服务关闭路径的健壮性）。"""
    service = module.ModelDiscoveryService()
    await service.stop_background_refresh()
    assert service._refresh_task is None
