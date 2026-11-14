"""Resolve and pin public media addresses, including every redirect."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


async def resolve_public_address(host: str, port: int) -> str:
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    if not addresses:
        raise ValueError("Media host has no addresses")
    resolved = [str(item[4][0]) for item in addresses]
    if any(not ipaddress.ip_address(address).is_global for address in resolved):
        raise ValueError("Media URL must resolve only to public addresses")
    return resolved[0]


class PublicMediaTransport(httpx.AsyncBaseTransport):
    def __init__(self, proxy: str | None = None) -> None:
        self.proxy = proxy
        self._closed = False
        self._origins: dict[tuple[str, str, int | None], httpx.AsyncHTTPTransport] = {}

    async def aclose(self) -> None:
        self._closed = True
        transports = tuple(self._origins.values())
        self._origins.clear()
        await asyncio.gather(*(transport.aclose() for transport in transports))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise RuntimeError("Media transport is closed")
        url = request.url
        if url.scheme not in ("http", "https") or url.username or url.password:
            raise ValueError("Unsupported media URL")
        timeout = request.extensions.get("timeout", {})
        connect_timeout = timeout.get("connect") if isinstance(timeout, dict) else None
        try:
            async with asyncio.timeout(connect_timeout):
                address = await resolve_public_address(
                    url.host, url.port or (443 if url.scheme == "https" else 80)
                )
        except TimeoutError as exc:
            raise httpx.ConnectTimeout(
                "Media DNS resolution timed out", request=request
            ) from exc
        except socket.gaierror as exc:
            raise httpx.ConnectError(
                "Media DNS resolution failed", request=request
            ) from exc
        if self._closed:
            raise RuntimeError("Media transport is closed")
        # Pin the checked IP so a second DNS lookup cannot rebind to a private host.
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        # Pinned IPs can be shared by unrelated HTTPS hosts. Keep their pools
        # separate so a connection authenticated for one SNI is never reused for another.
        key = (url.scheme, url.host, url.port)
        transport = self._origins.get(key)
        if transport is None:
            from aistudio_api.infrastructure.gateway.transport import (
                _shared_ssl_context,
            )

            transport = httpx.AsyncHTTPTransport(
                proxy=self.proxy, verify=_shared_ssl_context()
            )
            self._origins[key] = transport
        return await transport.handle_async_request(pinned)
