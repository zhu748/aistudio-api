"""Tests for system and configuration routes."""

from __future__ import annotations

from typing import ClassVar

import httpx
import pytest

from aistudio_api.api.app import app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_health_endpoint():
    async with _client() as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_check_endpoint():
    async with _client() as client:
        resp = await client.get("/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "auth_enabled" in data


@pytest.mark.asyncio
async def test_spa_endpoints():
    import asyncio

    async with _client() as client:
        responses = await asyncio.gather(
            *[
                client.get(path, headers={"Accept": "text/html"})
                for path in ("/", "/login", "/accounts", "/settings")
            ]
        )
        for resp in responses:
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_get_config_endpoint():
    async with _client() as client:
        resp = await client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "port" in data
        assert "proof_server_url" in data
        assert "yaml_content" in data


@pytest.mark.asyncio
async def test_update_config_yaml_validation():
    async with _client() as client:
        resp = await client.put(
            "/config/yaml", json={"yaml_content": "invalid: [yaml: broken"}
        )
        assert resp.status_code == 400

        resp_non_dict = await client.put(
            "/config/yaml", json={"yaml_content": "- item1\n- item2"}
        )
        assert resp_non_dict.status_code == 400


@pytest.mark.asyncio
async def test_rotation_status_and_clear_cooldown():
    from unittest.mock import MagicMock

    from aistudio_api.api.state import runtime_state
    from aistudio_api.application.account_rotator import AccountRotator

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_rotator.get_all_stats.return_value = {"acc_1": {"requests": 1}}

    runtime_state.rotator = mock_rotator
    async with _client() as client:
        resp = await client.get("/rotation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "sticky"
        assert "accounts" in data

        resp_clear = await client.post(
            "/rotation/clear-cooldown", json={"account_id": "acc_1"}
        )
        assert resp_clear.status_code == 200
        mock_rotator.clear_cooldown.assert_called_once_with("acc_1", model=None)


class _FakeSignerResponse:
    def __init__(self, status_code: int, text: str = "", json_data: object = None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeSignerClient:
    responses: ClassVar[list] = []

    def __init__(self, timeout: float = 8.0, proxy: str | None = None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str, headers: dict | None = None):
        return type(self).responses.pop(0)


@pytest.mark.asyncio
async def test_signer_server_test_legacy_200(monkeypatch):
    """旧版签名服务：GET /health 无参返回 200。"""
    _FakeSignerClient.responses = [
        _FakeSignerResponse(200, json_data={"ok": True, "status": "healthy"})
    ]
    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", _FakeSignerClient)
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "https://waa.spinsnow.fun"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status_code"] == 200
        assert data["data"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_signer_server_test_new_protocol_missing_cookies(monkeypatch):
    """新版 spinsnow 协议：/health 缺 Cookie 返回 400 + Missing cookies，
    应判定服务在线（ok=True）并附说明。"""
    _FakeSignerClient.responses = [
        _FakeSignerResponse(
            400,
            text='{"ok":false,"error":"Missing `cookies` query parameter or '
            'X-Google-Cookies header"}',
            json_data={
                "ok": False,
                "error": "Missing `cookies` query parameter or X-Google-Cookies header",
            },
        )
    ]
    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", _FakeSignerClient)
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "https://waa.spinsnow.fun"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status_code"] == 400
        assert "服务在线" in data.get("note", "")


@pytest.mark.asyncio
async def test_signer_server_test_new_protocol_with_cookie_recheck(monkeypatch):
    """新版协议且本地有活跃账号：携带 Cookie 复测应返回完整池化状态。"""
    from unittest.mock import MagicMock

    from aistudio_api.infrastructure.account.account_store import AccountStore

    fake_store = MagicMock(spec=AccountStore)
    fake_store.get_active_account.return_value = MagicMock(id="acc_test")
    fake_store.get_auth_path_optional.return_value = None  # 无 auth 文件则跳过复测
    monkeypatch.setattr(AccountStore, "__new__", lambda cls, *a, **kw: fake_store)

    _FakeSignerClient.responses = [
        _FakeSignerResponse(
            400,
            text='{"ok":false,"error":"Missing `cookies` query parameter"}',
            json_data={
                "ok": False,
                "error": "Missing `cookies` query parameter",
            },
        )
    ]
    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", _FakeSignerClient)
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "https://waa.spinsnow.fun"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True


@pytest.mark.asyncio
async def test_signer_server_test_other_400_stays_failed(monkeypatch):
    """非缺 Cookie 的 400 仍应视为失败。"""
    _FakeSignerClient.responses = [
        _FakeSignerResponse(
            400,
            text='{"ok":false,"error":"Bad gateway"}',
            json_data={"ok": False, "error": "Bad gateway"},
        )
    ]
    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", _FakeSignerClient)
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "https://waa.spinsnow.fun"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["status_code"] == 400


@pytest.mark.asyncio
async def test_signer_server_test_network_error(monkeypatch):
    """网络不可达时返回 ok=False 与错误信息。"""

    class _BrokenClient:
        def __init__(self, timeout: float = 8.0, proxy: str | None = None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url: str, headers: dict | None = None):
            raise httpx.ConnectError("All connection attempts failed")

    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "https://waa.spinsnow.fun"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "error" in data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://waa.example.com",
        "http://127.0.0.1.evil.com:8081",
        "http://2130706433:8081",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_signer_server_test_rejects_unregistered_url(monkeypatch, url):
    """SSRF 防护：未登记/非 http(s) 的目标必须在发出任何请求前被拒绝。"""

    class _NoCallClient:
        def __init__(self, timeout: float = 8.0, proxy: str | None = None):
            raise AssertionError("未登记的地址不应创建网络客户端")

    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", _NoCallClient)
        resp = await client.post("/config/signer-servers/test", json={"url": url})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "未在签名服务器配置中登记" in data["error"]


@pytest.mark.asyncio
async def test_signer_server_test_no_cookie_exfiltration_to_remote(monkeypatch):
    """远程节点缺 Cookie 时判定在线，但绝不发 X-Google-Cookies 复测。"""
    seen_headers: list[dict | None] = []

    class _RecordingClient:
        responses: ClassVar[list] = []

        def __init__(self, timeout: float = 8.0, proxy: str | None = None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url: str, headers: dict | None = None):
            seen_headers.append(headers)
            return type(self).responses.pop(0)

    _RecordingClient.responses = [
        _FakeSignerResponse(
            400,
            text='{"ok":false,"error":"Missing `cookies` query parameter"}',
            json_data={"ok": False, "error": "Missing `cookies` query parameter"},
        )
    ]
    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "https://waa.spinsnow.fun"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # 只发了 1 次 GET（缺 Cookie 的首次探活），且未携带任何 Cookie 头
        assert len(seen_headers) == 1
        assert seen_headers[0] is None
        assert "禁止向远程节点发送账号 Cookie" in data.get("note", "")
