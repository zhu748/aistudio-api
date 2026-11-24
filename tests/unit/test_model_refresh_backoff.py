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


# ───────────── 账号/层级变更后目录缓存失效（上游 a78e54b 同款机制） ─────────────


async def test_invalidate_cache_forces_immediate_refetch(monkeypatch):
    """invalidate_cache：账号导入/激活/层级变更后，下一次 get_models 立即重拉，
    不再吃满 600s TTL（否则付费门控最长 10 分钟用切换前的目录）。"""
    now = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    service = module.ModelDiscoveryService(cache_ttl_seconds=600)
    fetch = AsyncMock(return_value=[{"id": "m1"}])
    monkeypatch.setattr(service, "_fetch_via_http", fetch)

    await service.get_models(cookies={"SAPISID": "test"})
    assert fetch.await_count == 1
    # TTL 内的正常调用不再拉网
    await service.get_models(cookies={"SAPISID": "test"})
    assert fetch.await_count == 1

    # 模拟账号/层级变更：invalidate 后 TTL 内也立即重拉
    service.invalidate_cache()
    assert service._last_fetched_at == 0.0
    await service.get_models(cookies={"SAPISID": "test"})
    assert fetch.await_count == 2

    # 缓存仍作为兜底：拉网失败路径不受 invalidate 影响（退避语义不变）
    fetch.reset_mock(return_value=True, side_effect=True)
    fetch.return_value = [{"id": "m2"}]
    service.invalidate_cache()
    results = await asyncio.gather(
        *(service.get_models(cookies={"SAPISID": "test"}) for _ in range(10))
    )
    assert all(result for result in results)
    assert fetch.await_count == 1  # 并发请求合并为一次拉网


# ───────────── 目录抓取视角不一致 → 付费/等级结论降级到绝对源 ─────────────


def _service_with_view(catalog_view_tier: str | None, cached: list[dict]):
    service = module.ModelDiscoveryService(cache_ttl_seconds=600)
    service._cached_models = cached
    service._catalog_fetch_tier = catalog_view_tier
    return service


def test_view_mismatch_pro_catalog_blocks_free_viewer():
    """Pro 视角目录（neon 标记 paid=False 可用）+ Free 请求者 → 视角不一致，
    结论降级到静态快照（paid=True / accessModes=[1,3]）→ 判定为付费、要求 Pro+。

    ListModels 的 paid/accessModes 语义未做跨账号交叉验证（快照系 Free 号
    实测）；若槽位实为相对抓取账号的可用性视图，该降级保证 Pro→Free 后
    旧目录不会把付费模型漏放行到上游。"""
    service = _service_with_view("Pro", [{"id": "gemini-4-neon", "paid": False}])
    assert service.is_paid_model("gemini-4-neon", viewer_tier="Free") is True
    assert service.get_required_tiers("gemini-4-neon", viewer_tier="Free") == [
        "Pro",
        "Ultra",
        "Ultra_20X",
    ]
    assert service.is_model_allowed_for_tier("gemini-4-neon", "Free") is False


def test_view_mismatch_free_catalog_allows_pro_viewer():
    """Free 视角目录 + Pro 请求者 → 降级静态快照 → neon 对 Pro 放行（不误拦）。"""
    service = _service_with_view(
        "Free",
        [{"id": "gemini-4-neon", "paid": True, "accessModes": [1, 3]}],
    )
    assert service.get_required_tiers("gemini-4-neon", viewer_tier="Pro") == [
        "Pro",
        "Ultra",
        "Ultra_20X",
    ]
    assert service.is_model_allowed_for_tier("gemini-4-neon", "Pro") is True
    assert service.is_model_allowed_for_tier("gemini-4-neon", "Ultra_20X") is True


def test_view_match_keeps_catalog_authority():
    """视角一致时目录结论直接生效（目录优先的现状不回归）。"""
    service = _service_with_view("Free", [{"id": "gemini-4-neon", "paid": False}])
    assert service.is_paid_model("gemini-4-neon", viewer_tier="Free") is False
    assert service.get_required_tiers("gemini-4-neon", viewer_tier="Free") == []


def test_view_mismatch_unknown_model_fails_open():
    """降级后静态快照与 config 规则均不认识的模型 → fail-open 放行（上游裁决）。"""
    service = _service_with_view("Pro", [{"id": "brand-new-model", "paid": False}])
    assert service.is_paid_model("brand-new-model", viewer_tier="Free") is False


def test_view_mismatch_tier_normalization():
    """层级比较大小写不敏感（Ultra_20X / ultra_20x 同义）。"""
    service = _service_with_view("Ultra_20X", [{"id": "gemini-4-neon", "paid": False}])
    # 大小写差异仍视为同一层级 → 目录结论直接生效
    assert service.is_paid_model("gemini-4-neon", viewer_tier="ultra_20x") is False


def test_invalidate_keeps_fetch_tier_for_stale_window_guard():
    """invalidate 刻意保留旧抓取视角：失效后旧缓存仍兜底，重拉窗口内
    （≤60s 后台 tick）降级逻辑保持生效，避免切换间隙漏放行。"""
    service = _service_with_view("Pro", [{"id": "gemini-4-neon", "paid": False}])
    service.invalidate_cache()
    assert service._last_fetched_at == 0.0
    assert service._catalog_fetch_tier == "Pro"
    assert service.is_paid_model("gemini-4-neon", viewer_tier="Free") is True


async def test_get_models_records_fetch_tier(monkeypatch):
    """拉网成功记录抓取视角（显式传 Cookie 时 tier 头为 Free）。"""
    now = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    service = module.ModelDiscoveryService(cache_ttl_seconds=600)
    fetch = AsyncMock(return_value=[{"id": "m1"}])
    monkeypatch.setattr(service, "_fetch_via_http", fetch)
    await service.get_models(cookies={"SAPISID": "test"})
    assert service._catalog_fetch_tier == "Free"
    assert service._last_fetched_at == now[0]
