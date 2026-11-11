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


class PublicMediaTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https") or url.username or url.password:
            raise ValueError("Unsupported media URL")
        address = await resolve_public_address(
            url.host, url.port or (443 if url.scheme == "https" else 80)
        )
        # Pin the checked IP so a second DNS lookup cannot rebind to a private host.
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return await super().handle_async_request(pinned)
