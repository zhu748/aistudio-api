"""Media order, bounded parallelism, budget and deterministic cleanup contracts."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Coroutine

import httpx
import pytest

from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application.compat_service import (
    claude_to_gemini_request,
    openai_to_gemini_request,
)
from aistudio_api.application.media_session import MediaSession
from aistudio_api.application.responses_service import responses_to_gemini_request
from aistudio_api.config import settings


async def convert(protocol: str):
    urls = [f"https://media.example/image{i}" for i in range(8)]
    if protocol == "chat":
        return await openai_to_gemini_request(
            OpenAIChatCompletionRequest.model_validate(
                {
                    "model": "test",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": url}}
                                for url in urls
                            ],
                        }
                    ],
                }
            )
        )
    if protocol == "claude":
        return await claude_to_gemini_request(
            ClaudeMessagesRequest.model_validate(
                {
                    "model": "test",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image", "source": {"type": "url", "url": url}}
                                for url in urls
                            ],
                        }
                    ],
                }
            )
        )
    return await responses_to_gemini_request(
        OpenAIResponsesRequest.model_validate(
            {
                "model": "test",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": url} for url in urls
                        ],
                    }
                ],
            }
        )
    )


def install_client(
    monkeypatch,
    handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> list[httpx.AsyncClient]:
    clients: list[httpx.AsyncClient] = []

    async def get_client(self: MediaSession) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            clients.append(self.client)
        return self.client

    monkeypatch.setattr(MediaSession, "get_client", get_client)
    return clients


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_media_downloads_reuse_one_client_bound_parallelism_and_keep_order(
    monkeypatch, protocol
):
    active, peak = 0, 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                content=request.url.path.encode(),
                headers={"content-type": "image/png"},
            )
        finally:
            active -= 1

    clients = install_client(monkeypatch, handler)
    request, _ = await convert(protocol)
    assert [
        base64.b64decode(p.inlineData.data).decode()
        for p in request.contents[0].parts
        if p.inlineData
    ] == [f"/image{i}" for i in range(8)]
    assert peak == 4 and active == 0
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_total_media_budget_fails_as_client_error_and_closes_client(
    monkeypatch, protocol
):
    monkeypatch.setattr(settings, "max_request_bytes", 5)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"image", headers={"content-type": "image/png"}
        )

    clients = install_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="Total media"):
        await convert(protocol)
    assert clients and all(c.is_closed for c in clients)


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_media_cancellation_drains_downloads_and_closes_connections(
    monkeypatch, protocol
):
    entered = asyncio.Event()
    active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
            return httpx.Response(200)
        finally:
            active -= 1

    clients = install_client(monkeypatch, handler)
    task = asyncio.create_task(convert(protocol))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0 and clients and all(c.is_closed for c in clients)
