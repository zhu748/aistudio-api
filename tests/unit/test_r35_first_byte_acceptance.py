"""First-byte history measures accepted HTTP body bytes, not attempted writes."""

import asyncio
import time

import httpx
import pytest
import test_ordered_tool_routes as wire
from test_client_disconnect import _Client, isolated_history as isolated_history

from aistudio_api.api.app import app
from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings

wire_source = wire.wire_source
_SENT = "首个响应数据已发送"


def _client(stream):
    client = _Client("chat", stream=stream, spec_version="2.4")
    client.scope["headers"].append((b"x-anti-truncation", b"0"))
    return client


async def _history(client):
    start = client.messages[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    request_id = dict(start["headers"])[b"x-request-id"].decode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as admin:
        detail = await admin.get(f"/api/requests/{request_id}")
        stats = await admin.get("/api/requests/stats")
        assert detail.status_code == stats.status_code == 200
        totals = stats.json()["totals"]
        assert totals["requests"] == 1
        return detail.json(), totals


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("blocked", [False, True], ids=["rejected", "timed-out"])
async def test_first_body_not_accepted(
    monkeypatch, isolated_history, wire_source, stream, blocked
):
    wire_source.raw = wire.wire(wire.chunk([wire.text("A finite answer.")], 1))
    monkeypatch.setattr(settings, "nonstream_keepalive", False)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 60)
    monkeypatch.setattr(settings, "stream_send_timeout", 0.05)
    client = _client(stream)
    attempted = []

    async def send(message):
        if body := message.get("body", b""):
            attempted.append(body)
            if blocked:
                await asyncio.Event().wait()
            raise OSError("fixture peer closed")
        await client.send(message)

    async with asyncio.timeout(4):
        with pytest.raises(OSError):
            await app(client.scope, client.receive, send)
    assert len(attempted) == 1 and not any(m.get("body") for m in client.messages)
    row, totals = await _history(client)
    assert row["result"] == "cancelled" and not row["finish_reason"]
    assert row["first_byte_ms"] is None
    assert all(event["message"] != _SENT for event in row["events"])
    assert totals["first_byte_p50_ms"] is totals["first_byte_p95_ms"] is None
    if not stream:
        # The model generated content even though no JSON reached the peer.
        assert row["first_content_ms"] is not None


@pytest.mark.parametrize("stream", [False, True])
async def test_only_heartbeat_accepted(
    monkeypatch, isolated_history, wire_source, stream
):
    async def quiet(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(api_service_gemini, "ensure_active_account", quiet)
    monkeypatch.setattr(settings, "nonstream_keepalive", True)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    client = _client(stream)
    dwell_ms = None
    heartbeat = b": keepalive\n\n" if stream else b" \n"

    async def send(message):
        nonlocal dwell_ms
        if message.get("body"):
            if dwell_ms is not None:
                raise OSError("fixture peer closed after heartbeat")
            assert message["body"] == heartbeat
            entered = time.perf_counter()
            await asyncio.sleep(0.05)  # Backpressure before the first acceptance.
            dwell_ms = (time.perf_counter() - entered) * 1000
        await client.send(message)

    async with asyncio.timeout(4):
        with pytest.raises(OSError):
            await app(client.scope, client.receive, send)
    assert [m["body"] for m in client.messages if m.get("body")] == [heartbeat]
    row, totals = await _history(client)
    assert row["result"] == "cancelled" and row["first_content_ms"] is None
    assert dwell_ms is not None and row["first_byte_ms"] >= dwell_ms
    assert [e["stage"] for e in row["events"] if e["message"] == _SENT] == [
        "streaming" if stream else "response"
    ]
    assert (
        totals["first_byte_p50_ms"]
        == totals["first_byte_p95_ms"]
        == round(row["first_byte_ms"], 2)
    )
