"""Tests for accounts routes, cache isolation headers, and /api/accounts prefix."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountMeta


@pytest.fixture(autouse=True)
def setup_mock_account_service():
    mock_svc = MagicMock(spec=AccountService)
    sample_acc = AccountMeta(
        id="acc_1",
        name="Test Account 1",
        email="test1@gmail.com",
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user="0",
        cookie_id="cookie_1",
    )
    mock_svc.list_accounts.return_value = [sample_acc]
    mock_svc.get_active_account.return_value = sample_acc

    orig = runtime_state.account_service
    runtime_state.account_service = mock_svc
    try:
        yield mock_svc
    finally:
        runtime_state.account_service = orig


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_accounts_html_navigation_headers():
    """Verify that visiting /accounts as HTML returns the SPA HTML with cache isolation headers."""
    async with _client() as client:
        for path in ("/accounts", "/accounts/"):
            resp = await client.get(
                path, headers={"Accept": "text/html,application/xhtml+xml"}
            )
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "text/html" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_accounts_json_api_headers():
    """Verify that calling /accounts with Accept: application/json returns JSON with cache isolation headers."""
    async with _client() as client:
        resp = await client.get("/accounts", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("Content-Type", "")
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_endpoint_headers():
    """Verify that the dedicated /api/accounts endpoint returns JSON with cache isolation headers."""
    async with _client() as client:
        for path in ("/api/accounts", "/api/accounts/"):
            resp = await client.get(path, headers={"Accept": "application/json"})
            assert resp.status_code == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_and_legacy_accounts_parity():
    """Verify that /api/accounts and /accounts return identical payload for JSON clients."""
    async with _client() as client:
        resp_legacy = await client.get(
            "/accounts", headers={"Accept": "application/json"}
        )
        resp_api = await client.get(
            "/api/accounts", headers={"Accept": "application/json"}
        )
        assert resp_legacy.status_code == 200
        assert resp_api.status_code == 200
        assert resp_legacy.json() == resp_api.json()


@pytest.mark.asyncio
async def test_issue_5_cache_isolation_regression():
    """Regression test for Issue #5:
    Ensure that a client requesting HTML followed by JSON (or vice-versa) receives distinct
    content types and both responses carry Vary: Accept and Cache-Control: no-store headers.
    """
    async with _client() as client:
        html_resp = await client.get("/accounts", headers={"Accept": "text/html"})
        assert html_resp.status_code == 200
        assert "<!DOCTYPE html>" in html_resp.text
        assert "Accept" in html_resp.headers.get("Vary", "")
        assert "no-store" in html_resp.headers.get("Cache-Control", "")

        json_resp = await client.get(
            "/accounts", headers={"Accept": "application/json"}
        )
        assert json_resp.status_code == 200
        assert "application/json" in json_resp.headers.get("Content-Type", "")
        assert "Accept" in json_resp.headers.get("Vary", "")
        assert "no-store" in json_resp.headers.get("Cache-Control", "")
        assert isinstance(json_resp.json(), list)


@pytest.mark.asyncio
async def test_api_accounts_active_endpoint():
    """Verify subroutes under /api/accounts like /api/accounts/active work."""
    async with _client() as client:
        resp = await client.get(
            "/api/accounts/active", headers={"Accept": "application/json"}
        )
        assert resp.status_code == 200
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert data["id"] == "acc_1"


# ───────────── 手动纠正订阅等级 PUT /accounts/{id}/tier（上游 a78e54b 同款） ─────────────


@pytest.mark.asyncio
async def test_update_account_tier_manual_normalizes_and_invalidates_catalog(
    monkeypatch,
):
    """PUT tier：别名归一化（pro→Pro / ultra_20x→Ultra_20X）、失效目录缓存、
    活跃账号同步内存上下文 tier。"""
    from unittest.mock import MagicMock

    from aistudio_api.infrastructure.gateway import (
        model_discovery as model_discovery_module,
    )

    invalidate_mock = MagicMock()
    monkeypatch.setattr(
        model_discovery_module.model_discovery, "invalidate_cache", invalidate_mock
    )
    mock_svc = runtime_state.account_service
    mock_svc.get_account.return_value = AccountMeta(
        id="acc_1",
        name="Test Account 1",
        email="test1@gmail.com",
        created_at="2026-10-17T00:00:00Z",
        auth_user="0",
        cookie_id="cookie_1",
        tier="Free",
    )
    async with _client() as client:
        for raw, canonical in (("pro", "Pro"), ("ultra_20x", "Ultra_20X")):
            resp = await client.put("/accounts/acc_1/tier", json={"tier": raw})
            assert resp.status_code == 200
            mock_svc.update_account_tier.assert_called_with("acc_1", canonical)
    assert invalidate_mock.call_count == 2


@pytest.mark.asyncio
async def test_update_account_tier_manual_rejects_unknown_tier():
    """未知等级显式 400（normalize_benefit_tier 的静默落 Free 会把误拼的
    Pro 号降级成 Free，手动端点不允许这种静默行为）。"""
    mock_svc = runtime_state.account_service
    mock_svc.get_account.return_value = AccountMeta(
        id="acc_1",
        name="Test Account 1",
        email="test1@gmail.com",
        created_at="2026-10-17T00:00:00Z",
    )
    async with _client() as client:
        for bad in ("garbage", "", "utlra"):
            resp = await client.put("/accounts/acc_1/tier", json={"tier": bad})
            assert resp.status_code == 400
    mock_svc.update_account_tier.assert_not_called()


@pytest.mark.asyncio
async def test_update_account_tier_manual_404_for_missing_account():
    """账号不存在时 404，且不触碰层级存储。"""
    mock_svc = runtime_state.account_service
    mock_svc.get_account.return_value = None
    async with _client() as client:
        resp = await client.put("/accounts/acc_1/tier", json={"tier": "pro"})
        assert resp.status_code == 404
    mock_svc.update_account_tier.assert_not_called()
