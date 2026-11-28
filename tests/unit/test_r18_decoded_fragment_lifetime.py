"""A stalled provider read need not keep the last decoded wire fragment."""

import asyncio
import gc
import weakref
from contextlib import aclosing
from encodings.utf_8 import IncrementalDecoder
from types import SimpleNamespace
from typing import Any, cast

import pytest
import test_ordered_tool_routes as routes

from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway import streaming
from aistudio_api.infrastructure.gateway.client import PreparedRequest

wire_source = routes.wire_source


class _Wire:
    """Retain the original bytes, not the decoder's string, while paused."""

    def __init__(self, first: bytes, second: bytes, status: int = 200):
        self.first, self.second, self.status = first, second, status
        self.reading, self.resume = asyncio.Event(), asyncio.Event()
        self.closes = 0

    async def send_streaming_request(self, **_kwargs):
        try:
            yield "status", self.status
            yield "chunk", self.first
            self.reading.set()
            await self.resume.wait()
            yield "chunk", self.second
        finally:
            self.closes += 1

    def stream(self):
        return streaming.StreamingGateway(cast(Any, self)).stream_chat(
            request=PreparedRequest(url="https://fixture.invalid", headers={}, body=""),
            model="fixture",
        )


@pytest.mark.parametrize("partial", [False, True], ids=["complete", "split-utf8"])
async def test_messages_route_releases_consumed_wire_string(
    monkeypatch, wire_source, partial
):
    first, second = "Wind in 京都 🌱. " * 256, "Next: café 🌱."
    chunk = routes.chunk([routes.text(first)])
    raw = routes.wire(chunk, routes.chunk([routes.text(second)], 1))
    # The next candidate may already be open, even inside a multibyte character.
    cut = raw.index("é".encode()) + 1 if partial else len(routes.wire(chunk)) - 2
    source = _Wire(raw[:cut], raw[cut:])
    monkeypatch.setattr(
        wire_source, "send_streaming_request", source.send_streaming_request
    )
    monkeypatch.setattr(settings, "request_history", False)

    class PhysicalFragment(str):
        pass

    watched: list[weakref.ReferenceType[PhysicalFragment]] = []

    class Decoder(IncrementalDecoder):
        def decode(self, input, final=False):
            text = super().decode(input, final)
            if not watched and text:
                fragment = PhysicalFragment(text)
                watched.append(weakref.ref(fragment))
                return fragment
            return text

    # Make only the first actual UTF-8 decoder result observable by identity.
    monkeypatch.setattr(
        streaming, "codecs", SimpleNamespace(getincrementaldecoder=lambda _: Decoder)
    )
    reply = asyncio.create_task(routes.request("claude", True))
    try:
        await asyncio.wait_for(source.reading.wait(), timeout=5)
        gc.collect()
        assert len(watched) == 1
        retained = watched[0]() is not None
    finally:
        source.resume.set()
        events = await asyncio.wait_for(reply, timeout=5)
    assert not retained, "The gateway still owns the previous decoded fragment"
    assert source.closes == 1
    assert [e["type"] for e in events] == [
        "message_start",
        "ping",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert [e["delta"]["text"] for e in events[3:5]] == [first, second]
    assert events[-2]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.parametrize("cancel_read", [False, True])
async def test_gateway_closes_without_reading_ahead(cancel_read):
    source = _Wire(routes.wire(routes.chunk([routes.text("first")])), b"")
    async with asyncio.timeout(3), aclosing(source.stream()) as stream:
        assert await anext(stream) == ("body", "first")
        assert not source.reading.is_set()
        if cancel_read:
            read = asyncio.ensure_future(anext(stream))
            try:
                await source.reading.wait()
            finally:
                read.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await read
    assert source.reading.is_set() == cancel_read
    assert source.closes == 1


async def test_gateway_error_body_still_combines_fragments():
    source = _Wire(b"bad-\xff", b" input", status=422)
    source.resume.set()
    async with aclosing(source.stream()) as stream:
        with pytest.raises(RequestError) as err:
            await anext(stream)
    assert (err.value.status, err.value.message) == (422, "bad-� input")
    assert source.closes == 1
