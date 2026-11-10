"""``compat_cors_middleware`` 的 CORS 白名单测试。

历史缺陷：中间件曾无条件反射任意 ``Origin`` 并附带
``Access-Control-Allow-Credentials: true``，等价于允许任意站点带用户凭据
跨域读取 ``/v1/*``。现在必须只放行 ``AISTUDIO_CORS_ALLOWED_ORIGINS``
中显式登记的精确 Origin，其余一律不下发任何 ``Access-Control-*`` 头。
"""

from __future__ import annotations

import httpx
import pytest

from aistudio_api.api.app import app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _access_control_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower().startswith("access-control-")}


@pytest.mark.asyncio
async def test_preflight_allows_configured_origin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AISTUDIO_CORS_ALLOWED_ORIGINS", "https://app.example.com")
    async with _client() as client:
        resp = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
    assert resp.headers["access-control-allow-credentials"] == "true"
    assert resp.headers["access-control-max-age"] == "86400"


@pytest.mark.asyncio
async def test_preflight_rejects_unlisted_origin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AISTUDIO_CORS_ALLOWED_ORIGINS", "https://app.example.com")
    async with _client() as client:
        resp = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert resp.status_code == 204
    # 未允许的预检不得下发任何 Access-Control-* 头
    assert _access_control_headers(resp.headers) == {}


@pytest.mark.asyncio
async def test_preflight_rejects_everything_when_allowlist_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("AISTUDIO_CORS_ALLOWED_ORIGINS", raising=False)
    async with _client() as client:
        resp = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert resp.status_code == 204
    assert _access_control_headers(resp.headers) == {}


@pytest.mark.asyncio
async def test_simple_request_does_not_reflect_arbitrary_origin(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("AISTUDIO_CORS_ALLOWED_ORIGINS", raising=False)
    async with _client() as client:
        resp = await client.get(
            "/v1/models", headers={"Origin": "https://evil.example.com"}
        )
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_non_v1_path_is_untouched(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AISTUDIO_CORS_ALLOWED_ORIGINS", "https://app.example.com")
    async with _client() as client:
        resp = await client.get(
            "/health", headers={"Origin": "https://app.example.com"}
        )
    assert resp.status_code == 200
    assert _access_control_headers(resp.headers) == {}
