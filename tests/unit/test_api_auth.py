from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from aistudio_api.api.dependencies import require_api_key, require_web_auth
from aistudio_api.config import settings


def _build_client() -> httpx.AsyncClient:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected():
        return {"ok": True}

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_auth_is_disabled_when_no_api_key_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/protected")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_bearer_token_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get(
            "/protected", headers={"Authorization": "Bearer secret-token"}
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_x_api_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get("/protected", headers={"X-API-Key": "secret-token"})
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_x_goog_api_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get(
            "/protected", headers={"x-goog-api-key": "secret-token"}
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_query_param_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get("/protected?key=secret-token")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_missing_or_invalid_api_key_returns_401(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get("/protected")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def _stub_request(
    path: str, *, headers: dict[str, str], query: dict[str, str] | None = None
) -> MagicMock:
    """构造一个显式带 url.path 的请求桩。

    必须显式设置 ``url.path``：否则 MagicMock 会让
    ``request.url.path.endswith("/api/logs/stream")`` 返回真值，
    从而误入一次性票据快速路径，使下面的鉴权断言其实没走到密码校验。
    """
    req = MagicMock(spec=Request)
    req.method = "POST"
    req.headers = headers
    req.query_params = query or {}
    req.url = MagicMock()
    req.url.path = path
    return req


def test_dependencies_timing_safe_auth(monkeypatch):
    """Verify constant-time comparison and empty password guard."""
    monkeypatch.setattr(settings, "web_password", "super_secret_admin_pass")
    monkeypatch.setattr(settings, "api_keys", frozenset({"key123"}))

    # require_web_auth with correct password
    require_web_auth(
        _stub_request(
            "/stats", headers={"authorization": "Bearer super_secret_admin_pass"}
        )
    )

    # require_web_auth with wrong password
    with pytest.raises(HTTPException) as exc_info:
        require_web_auth(
            _stub_request("/stats", headers={"authorization": "Bearer wrong"})
        )
    assert exc_info.value.status_code == 401

    # require_api_key matching admin web_password
    require_api_key(
        _stub_request("/v1/models", headers={"x-api-key": "super_secret_admin_pass"})
    )

    # Empty web_password guard
    monkeypatch.setattr(settings, "web_password", "")
    with pytest.raises(HTTPException):
        require_api_key(_stub_request("/v1/models", headers={"x-api-key": ""}))


def test_api_key_comparison_uses_compare_digest_with_bytes(monkeypatch):
    """常量时间比较的契约：以 bytes 形式调用 secrets.compare_digest。

    不做时序测量（CI 上必然 flaky），而是钉住实现方式。
    """
    import secrets as secrets_module

    monkeypatch.setattr(settings, "web_password", "")
    monkeypatch.setattr(settings, "api_keys", frozenset({"key123", "key456"}))

    calls: list[tuple[object, object]] = []
    real_compare = secrets_module.compare_digest

    def _spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return bool(real_compare(a, b))

    monkeypatch.setattr(secrets_module, "compare_digest", _spy)

    require_api_key(_stub_request("/v1/models", headers={"x-api-key": "key456"}))
    assert calls, "未调用 secrets.compare_digest"
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)

    calls.clear()
    with pytest.raises(HTTPException):
        require_api_key(_stub_request("/v1/models", headers={"x-api-key": "nope"}))
    assert calls


def test_require_web_auth_rejects_html_accept_spoofing(monkeypatch):
    """验证客户端伪造 Accept: text/html 无法绕过管理控制台密码校验。"""
    monkeypatch.setattr(settings, "web_password", "super_secret_admin_pass")

    req_spoof = MagicMock(spec=Request)
    req_spoof.method = "GET"
    req_spoof.headers = {"accept": "text/html,application/xhtml+xml"}
    req_spoof.query_params = {}
    with pytest.raises(HTTPException) as exc_info:
        require_web_auth(req_spoof)
    assert exc_info.value.status_code == 401
