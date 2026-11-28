"""The dashboard reports the status accepted by the ASGI server, or zero."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from test_client_disconnect import _Client, isolated_history as isolated_history

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput


@pytest.mark.parametrize(
    ("code", "cut", "result"),
    [
        (200, "disconnect-before-start", "cancelled"),
        (200, "cancel-start", "cancelled"),
        (200, "refuse-start", "cancelled"),
        (413, "refuse-start", "cancelled"),
        (200, "refuse-body", "cancelled"),
        (503, "refuse-body", "cancelled"),
        (200, "complete", "success"),
        (429, "complete", "rate_limited"),
        (500, "complete", "error"),
        (504, "complete", "timeout"),
        (500, "refuse-start", "error"),
        (504, "refuse-start", "timeout"),
    ],
)
async def test_response_status_at_outer_send(
    monkeypatch, isolated_history, code, cut, result
):
    client = _Client("chat", stream=False, spec_version="2.4")
    paused, release = asyncio.Event(), asyncio.Event()
    starts = []

    async def generate(**_kwargs):
        return ModelOutput(
            candidates=[Candidate(text="Local answer.", finish_reason=1)]
        )

    async def local_client():
        if cut == "disconnect-before-start":
            paused.set()
            await release.wait()
        if code == 500:
            raise RuntimeError("fixture: application failed before headers")
        if code == 504:
            await asyncio.Event().wait()  # The request deadline generates the reply.
        if code in (429, 503):
            raise HTTPException(code, "local fixture")
        return SimpleNamespace(generate_content=generate)

    monkeypatch.setitem(app.dependency_overrides, get_client, local_client)
    monkeypatch.setattr(settings, "nonstream_keepalive", False)
    if code == 504:
        monkeypatch.setattr(settings, "request_timeout", 0)
    if code == 413:
        client.scope["headers"].append(
            (b"content-length", str(settings.max_request_bytes + 1).encode())
        )

    async def send(message):
        start = message["type"] == "http.response.start"
        if start:
            starts.append(message)
        if (start and cut in ("cancel-start", "refuse-start")) or (
            message.get("body") and cut == "refuse-body"
        ):
            paused.set()
            await release.wait()
            raise OSError("fixture: downstream refused send")
        await client.send(message)

    async with (
        asyncio.timeout(5),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as admin,
    ):
        owner = asyncio.create_task(app(client.scope, client.receive, send))
        try:
            if cut != "complete":
                await paused.wait()
                (live,) = (await admin.get("/api/requests/active")).json()["items"]
                detail = (await admin.get(f"/api/requests/{live['request_id']}")).json()
                for row in (live, detail):
                    assert row["result"] == "running" and row["first_byte_ms"] is None
                    assert row["status_code"] == (code if cut == "refuse-body" else 0)
            if cut == "disconnect-before-start":
                client.disconnect.set()
            elif cut == "cancel-start":
                owner.cancel()
            else:
                release.set()

            if cut == "cancel-start":
                with pytest.raises(asyncio.CancelledError):
                    await owner
            elif cut.startswith("refuse-"):
                with pytest.raises(OSError, match="fixture: downstream refused send"):
                    await owner
            else:
                await owner
        finally:
            if not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)

        # Once unregistered, both public history endpoints read the real SQLite row.
        assert not (await admin.get("/api/requests/active")).json()["items"]
        (stored,) = (await admin.get("/api/requests")).json()["items"]
        detail = (await admin.get(f"/api/requests/{stored['request_id']}")).json()
        assert [m["status"] for m in starts] == (
            [] if cut == "disconnect-before-start" else [code]
        )
        if starts:
            assert (
                dict(starts[0]["headers"])[b"x-request-id"].decode()
                == stored["request_id"]
            )
        accepted = [
            m["status"] for m in client.messages if m["type"] == "http.response.start"
        ]
        assert accepted == ([code] if cut in ("refuse-body", "complete") else [])
        for row in (stored, detail):
            assert (row["result"], row["status_code"]) == (
                result,
                next(iter(accepted), 0),
            )
