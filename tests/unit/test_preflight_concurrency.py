"""Browser preflights must remain anonymous and share concurrent cache misses."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport

URL = "https://alkalimakersuite-pa.clients6.google.com/rpc"
HEADERS = {"Origin": "https://aistudio.google.com", "Content-Type": "application/json"}


@pytest.mark.parametrize("status", [204, 503])
async def test_concurrent_cache_misses_send_one_options(status):
    options = 0
    posts = 0

    async def handler(request):
        nonlocal options, posts
        if request.method == "OPTIONS":
            options += 1
            await asyncio.sleep(0.01)
            return httpx.Response(status, headers={"Access-Control-Max-Age": "600"})
        posts += 1
        return httpx.Response(200, content=b"ok")

    transport = HTTPStreamTransport()
    transport._clients[transport.proxy_url or ""] = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        results = await asyncio.gather(
            *[
                transport.send_request(url=URL, headers=HEADERS, body="{}")
                for _ in range(32)
            ]
        )
        assert results == [(200, b"ok")] * 32
        assert posts == 32 and options == 1
    finally:
        await transport.close()


async def test_preflight_drops_client_cookie_jar_and_default_auth():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=("account", "secret")
    ) as client:
        client.cookies.set("SAPISID", "other-account", domain=".clients6.google.com")
        transport = HTTPStreamTransport()
        await transport._ensure_cors_preflight(
            url=URL, rpc_headers=HEADERS, client=client
        )
        await transport.close()
    assert len(seen) == 1
    assert "cookie" not in seen[0].headers and "authorization" not in seen[0].headers


@pytest.mark.parametrize("different_client", [False, True])
async def test_cache_is_partitioned_by_origin_and_connection_pool(different_client):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204, headers={"Access-Control-Max-Age": "600"})

    transport = HTTPStreamTransport()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as first,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as second,
    ):
        await transport._ensure_cors_preflight(
            url=URL, rpc_headers=HEADERS, client=first
        )
        headers = (
            HEADERS
            if different_client
            else {**HEADERS, "Origin": "https://other.example"}
        )
        await transport._ensure_cors_preflight(
            url=URL, rpc_headers=headers, client=second if different_client else first
        )
    await transport.close()
    assert len(seen) == 2


async def test_cancelling_one_caller_does_not_break_another():
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.method == "OPTIONS":
            entered.set()
            await release.wait()
            return httpx.Response(204, headers={"Access-Control-Max-Age": "600"})
        return httpx.Response(200, content=b"ok")

    transport = HTTPStreamTransport()
    transport._clients[transport.proxy_url or ""] = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    first = asyncio.create_task(
        transport.send_request(url=URL, headers=HEADERS, body="{}")
    )
    await entered.wait()
    second = asyncio.create_task(
        transport.send_request(url=URL, headers=HEADERS, body="{}")
    )
    await asyncio.sleep(0)
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await asyncio.wait_for(second, 1) == (200, b"ok")
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await transport.close()
