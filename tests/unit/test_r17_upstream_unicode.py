"""Invalid provider text is an upstream failure, not a codec failure or success."""

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import httpx2
import openai
import pytest
import test_ordered_tool_routes as routes

from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError, TransportError
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output

wire_source = routes.wire_source
_URL = "http://fixture.invalid"
_HEADERS = {"X-Anti-Truncation": "0"}


def _wire(token: bytes, *, accepted=False, cut=False) -> bytes:
    first = [routes.chunk([routes.text("First. ")])] if accepted else []
    raw = json.dumps(
        [[*first, routes.chunk([routes.text("Weather: MARKER end.")], 1)]]
    ).encode("ascii")
    if cut:
        raw = raw[: raw.index(b" end.")]
    return raw.replace(b"MARKER", token)


@pytest.mark.parametrize(
    "token",
    [rb"\ud800X", rb"\uDFFF", rb"\uD800\uD800", rb"\\uD800\uDC00", rb"\uD800\\uDC00"],
)
@pytest.mark.parametrize("cut", [False, True])
def test_unpaired_escapes_rejected_without_returning_the_bad_chunk(token, cut):
    raw = _wire(token, accepted=True, cut=cut).decode()
    with pytest.raises(TransportError, match="unpaired Unicode surrogate"):
        parse_text_output(raw)
    parser = IncrementalJSONStreamParser()
    received = []
    with pytest.raises(TransportError, match="unpaired Unicode surrogate"):
        for c in raw:
            received.extend(parser.feed(c))
        received.extend(parser.finish())
    assert received == [routes.chunk([routes.text("First. ")])]


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (rb"\uD800\uDc00\udbFf\udFFF", "\U00010000\U0010ffff"),
        (rb"\\ud800\\uDC00", r"\ud800\uDC00"),
        (rb"\\\uD83d\uDE42", "\\🙂"),
        ("文🙂�".encode(), "文🙂�"),  # U+FFFD is valid if actually provided.
    ],
)
def test_valid_escapes_literal_text_and_unicode_round_trip(token, expected):
    raw = _wire(token).decode()
    assert parse_text_output(raw).text == f"Weather: {expected} end."
    parser = IncrementalJSONStreamParser()
    assert list(parser.feed(raw)) == [
        routes.chunk([routes.text(f"Weather: {expected} end.")], 1)
    ]


async def test_bad_metadata_keys_rejected_before_usage_or_capture():
    first = routes.chunk([routes.text("First. ")])
    # Valid display text and usage in this chunk do not make it acceptable.
    bad = [*routes.chunk([routes.text("Not accepted.")], 1), None, [11, 2, 13]]
    bad.append([{"bad" + chr(0xD800): "entry"}])
    source = routes.WireSource()
    source.raw = json.dumps([[first, bad]]).encode()
    capture, on_usage = Mock(), Mock()
    request = PreparedRequest(
        url=_URL, headers={}, body="", model_turn=Mock(capture=lambda: capture)
    )
    received = []
    with pytest.raises(TransportError):
        async for event in StreamingGateway(cast(Any, source)).stream_chat(
            request=request, model=routes.MODEL, on_usage=on_usage
        ):
            received.append(event)
    assert received == [("body", "First. ")]
    capture.add.assert_called_once_with([routes.text("First. ")], None)
    capture.finish.assert_not_called()
    on_usage.assert_not_called()


@pytest.fixture
def attempts(wire_source, monkeypatch):
    # Keep real retry logic and deterministic zero delay; never switch accounts.
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(api_service_gemini, "get_max_retries", lambda: 5)
    monkeypatch.setattr(
        api_service_gemini, "random", SimpleNamespace(uniform=lambda *_: 0)
    )
    monkeypatch.setattr(
        api_service_gemini, "try_switch_account", AsyncMock(return_value=False)
    )
    requests = []
    unary = wire_source.send_request
    streaming = wire_source.send_streaming_request

    async def send(**kwargs):
        requests.append("unary")
        return await unary(**kwargs)

    async def stream(**kwargs):
        requests.append("stream")
        async for event in streaming(**kwargs):
            yield event

    monkeypatch.setattr(wire_source, "send_request", send)
    monkeypatch.setattr(wire_source, "send_streaming_request", stream)
    return requests


@pytest.mark.parametrize("protocol", routes.PROTOCOLS)
@pytest.mark.parametrize(
    ("token", "cut"), [(b"\xff", False), (rb"\uDE42", False), (rb"\ud83dX", True)]
)
async def test_unary_invalid_provider_text_is_502_and_retries_are_limited(
    attempts, wire_source, protocol, token, cut
):
    wire_source.raw = _wire(token, accepted=True, cut=cut)
    path, params = routes.input_request(protocol, False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(routes.app), base_url=_URL
    ) as client:
        response = await client.post(path, json=params, headers=_HEADERS)
    assert response.status_code == 502
    assert "Invalid" in response.json()["error"]["message"]
    assert attempts == ["unary"] * 3


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize(
    ("token", "cut"), [(rb"\uDE42", False), (rb"\uD800X", True), (b"\xff", False)]
)
async def test_responses_sdk_stream_has_one_failed_terminal_and_no_replayed_text(
    attempts, wire_source, token, cut, accepted
):
    wire_source.raw = _wire(token, accepted=accepted, cut=cut)
    async with (
        openai.AsyncOpenAI(
            base_url=_URL + "/v1",
            api_key="fixture",
            max_retries=0,
            default_headers=_HEADERS,
            _strict_response_validation=True,
            http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(routes.app)),
        ) as client,
        client.responses.stream(model=routes.MODEL, input="Weather?") as stream,
    ):
        events = [event async for event in stream]
    assert [e.delta for e in events if e.type == "response.output_text.delta"] == (
        ["First. "] if accepted else []
    )
    terminal = [
        e
        for e in events
        if e.type in ("response.failed", "response.completed", "response.incomplete")
    ]
    assert len(terminal) == 1 and terminal[0] is events[-1]
    assert terminal[0].type == "response.failed"
    assert (
        terminal[0].response.error and terminal[0].response.error.code == "server_error"
    )
    assert attempts == ["stream"] * (1 if accepted else 3)


async def test_non_success_bytes_keep_the_existing_http_diagnostic():
    client = AIStudioClient.__new__(AIStudioClient)
    client._session = Mock(send_request=AsyncMock(return_value=(400, b"bad-\xff")))
    with pytest.raises(RequestError, match="bad-�") as err:
        await client.generate_content(
            prepared=PreparedRequest(url=_URL, headers={}, body="")
        )
    assert err.value.status == 400
