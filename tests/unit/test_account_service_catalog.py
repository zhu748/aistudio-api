"""目录缓存失效收敛到 AccountService 的单元测试。

切号/层级变更的全部入口（路由激活、导入链路、rotator 自动换号——403
处理/会话过期/限流）都经过 AccountService 的 activate_account /
set_active_account / update_account_tier，目录失效收敛到这三个方法后，
任何路径切换账号都不会留下最长 600s 的旧目录窗口。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore
from aistudio_api.infrastructure.gateway.model_discovery import model_discovery
from aistudio_api.infrastructure.gateway.session import GatewaySession


@pytest.fixture(autouse=True)
def _isolate_catalog_singleton():
    """保护模块级单例的目录状态（测试直接读写，结束后恢复）。"""
    saved = (
        model_discovery._cached_models,
        model_discovery._last_fetched_at,
    )
    yield
    (
        model_discovery._cached_models,
        model_discovery._last_fetched_at,
    ) = saved


def _service_with_store() -> tuple[AccountService, MagicMock]:
    store = MagicMock(spec=AccountStore)
    return AccountService(store), store


def _account(tier: str = "Pro") -> AccountMeta:
    return AccountMeta(
        id="acc_1",
        name="Account 1",
        email="acc1@gmail.com",
        created_at="2026-01-01",
        tier=tier,
    )


@pytest.mark.asyncio
async def test_activate_account_invalidates_catalog():
    """切换活跃账号（含 rotator 自动换号路径）后目录缓存立即失效。"""
    svc, store = _service_with_store()
    store.get_account.return_value = _account()
    store.get_auth_path_optional.return_value = "/tmp/fake_auth"
    session = MagicMock(spec=GatewaySession)
    session.switch_auth = AsyncMock()
    session.ensure_ready = AsyncMock()

    model_discovery._last_fetched_at = 12345.0
    result = await svc.activate_account("acc_1", session)
    assert result is not None
    assert model_discovery._last_fetched_at == 0.0


def test_set_active_account_invalidates_catalog():
    """轻量设置活跃账号（导入链路无会话路径）同样失效目录缓存。"""
    svc, _store = _service_with_store()
    model_discovery._last_fetched_at = 12345.0
    svc.set_active_account("acc_1")
    assert model_discovery._last_fetched_at == 0.0


def test_update_account_tier_invalidates_catalog_on_change():
    """层级更新成功后失效目录缓存（tier 头与付费门控视角随之变化）。"""
    svc, store = _service_with_store()
    store.update_account_tier.return_value = True
    model_discovery._last_fetched_at = 12345.0
    assert svc.update_account_tier("acc_1", "Ultra") is True
    assert model_discovery._last_fetched_at == 0.0


def test_update_account_tier_keeps_catalog_when_no_change():
    """层级未实际变更（store 返回 False）不失效目录缓存。"""
    svc, store = _service_with_store()
    store.update_account_tier.return_value = False
    model_discovery._last_fetched_at = 12345.0
    assert svc.update_account_tier("acc_1", "Ultra") is False
    assert model_discovery._last_fetched_at == 12345.0
