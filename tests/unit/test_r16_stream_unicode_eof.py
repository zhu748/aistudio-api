"""An EOF inside an escaped non-BMP character must not strand a streaming client."""

import json
from typing import Any

import anthropic
import httpx
import httpx2
import openai
import pytest
import test_ordered_tool_routes as routes
from google import genai
from google.genai import types

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_chunk

wire_source = routes.wire_source
_BASE_URL = "http://fixture.invalid"
_HEADERS = {"X-Anti-Truncation": "0"}


def _open_string(value):
    wire = json.dumps([[routes.chunk([routes.text(value + " end.")])]])
    return wire[: wire.index(" end.")]


def _recovered(fragment):
    parser = IncrementalJSONStreamParser()
    for char in fragment:
        assert not list(parser.feed(char))
    (chunk,) = parser.finish()
    return parse_response_chunk(chunk).text


@pytest.mark.parametrize("char", ["\U00010000", "🙂", "\U0010ffff"])
@pytest.mark.parametrize("slashes", [0, 1, 2])
def test_every_cut_in_a_real_escape_pair(char, slashes):
    prefix = "Prior 🌱 " + "\\" * slashes
    pair = json.dumps(char)[1:-1]
    # Exercise uppercase hex as well as json.dumps' lowercase wire in the SDK tests.
    pair = r"\u" + pair[2:6].upper() + r"\u" + pair[8:12].upper()
    for width in range(len(pair) + 1):
        assert _recovered(_open_string(prefix) + pair[:width]) == (
            prefix + (char if width == len(pair) else "")
        )


@pytest.mark.parametrize(
    "literal", [r"\ud83d", r"\\uD83D", r"\ud83d\ude42", r"\uD800\uDC"]
)
def test_literal_escapes_survive_eof_before_or_inside_next_bmp_escape(literal):
    prefix = "Literal: " + literal
    # U+D55C is one valid BMP character; its escape starts like a low surrogate.
    for width in range(7):
        assert _recovered(_open_string(prefix) + r"\ud55c"[:width]) == (
            prefix + ("한" if width == 6 else "")
        )


@pytest.fixture(
    params=[None, 0, 6, 9, 12], ids=["complete", "before", "high", "low", "pair"]
)
def answer(request, wire_source, monkeypatch):
    monkeypatch.setattr(settings, "request_history", False)
    wire = json.dumps(
        [
            [
                routes.chunk([routes.text("First. ")]),
                routes.chunk([routes.text("Weather: 🙂 clear.")], 1),
            ]
        ]
    ).encode("ascii")
    cut = request.param
    if cut is None:
        wire_source.raw = wire
        return "First. Weather: 🙂 clear.", True
    wire_source.raw = wire[: wire.index(rb"\ud83d\ude42") + cut]
    return "First. Weather: " + ("🙂" if cut == 12 else ""), False


def _compat_client_options() -> dict[str, Any]:
    return {
        "api_key": "fixture",
        "max_retries": 0,
        "default_headers": _HEADERS,
        "_strict_response_validation": True,
        "http_client": httpx2.AsyncClient(transport=httpx2.ASGITransport(routes.app)),
    }


async def test_responses_sdk_terminal(answer):
    expected, complete = answer
    async with (
        openai.AsyncOpenAI(
            base_url=_BASE_URL + "/v1", **_compat_client_options()
        ) as client,
        client.responses.stream(model=routes.MODEL, input="Weather?") as stream,
    ):
        events = [event async for event in stream]
    terminal = events[-1]
    if complete:
        assert terminal.type == "response.completed"
    else:
        assert terminal.type == "response.incomplete"
    assert terminal.response.output_text == expected


async def test_chat_sdk_terminal(answer):
    expected, complete = answer
    async with openai.AsyncOpenAI(
        base_url=_BASE_URL + "/v1", **_compat_client_options()
    ) as client:
        stream = await client.chat.completions.create(
            model=routes.MODEL,
            messages=[{"role": "user", "content": "Weather?"}],
            stream=True,
        )
        choices = [choice async for event in stream for choice in event.choices]
    assert "".join(choice.delta.content or "" for choice in choices) == expected
    assert [c.finish_reason for c in choices if c.finish_reason] == [
        "stop" if complete else "length"
    ]


async def test_claude_sdk_terminal(answer):
    expected, complete = answer
    async with (
        anthropic.AsyncAnthropic(
            base_url=_BASE_URL, **_compat_client_options()
        ) as client,
        client.messages.stream(
            model=routes.MODEL,
            messages=[{"role": "user", "content": "Weather?"}],
            max_tokens=64,
        ) as stream,
    ):
        events = [e.type async for e in stream]
        message = await stream.get_final_message()
    assert events[-1] == "message_stop"
    assert message.stop_reason == ("end_turn" if complete else "max_tokens")
    assert "".join(b.text for b in message.content if b.type == "text") == expected


async def test_native_sdk_terminal(answer):
    expected, complete = answer
    async with httpx.AsyncClient(transport=httpx.ASGITransport(routes.app)) as http:
        options = types.HttpOptions(
            base_url=_BASE_URL,
            api_version="v1beta",
            headers=_HEADERS,
            httpx_async_client=http,
        )
        async with genai.Client(api_key="fixture", http_options=options).aio as client:
            frames = [
                f
                async for f in await client.models.generate_content_stream(
                    model=routes.MODEL, contents="Weather?"
                )
            ]
    assert "".join(f.text or "" for f in frames) == expected
    assert [
        c.finish_reason for f in frames for c in f.candidates or [] if c.finish_reason
    ] == [types.FinishReason.STOP if complete else types.FinishReason.OTHER]
