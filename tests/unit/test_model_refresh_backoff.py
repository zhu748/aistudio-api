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
