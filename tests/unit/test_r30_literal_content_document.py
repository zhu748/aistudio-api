"""Claude custom-content documents can carry literal text, not just blocks."""

from typing import cast
from unittest.mock import AsyncMock, Mock

import anthropic
import pytest
from anthropic.types import (
    ContentBlockSourceParam,
    DocumentBlockParam,
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
)
from anthropic.types.beta import (
    BetaContentBlockSourceParam,
    BetaRequestDocumentBlockParam,
)
from test_compat_media_routes import _client_options, _serialized_contents
from test_r8_claude_estimator_scope import ascii_count
from test_signed_part_roundtrip import MODEL, _claude_sdk_turn, route as route

from aistudio_api.application.media_session import MediaSession
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_part
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_BODY = "  Musée 東\r\n10:30  "
_URL = "https://fixture.invalid/this-is-literal"


def _text(text):
    return TextBlockParam(type="text", text=text)


def _doc(body, beta):
    source = BetaContentBlockSourceParam if beta else ContentBlockSourceParam
    doc = BetaRequestDocumentBlockParam if beta else DocumentBlockParam
    return doc(type="document", source=source(type="content", content=body))


@pytest.fixture(autouse=True)
def _no_media(monkeypatch):
    get = AsyncMock(side_effect=AssertionError("literal text is not media"))
    charge = Mock(side_effect=AssertionError("literal text has no media charge"))
    monkeypatch.setattr(MediaSession, "get_client", get)
    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    yield
    get.assert_not_awaited()
    charge.assert_not_called()


@pytest.mark.parametrize("beta", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_messages_sdk_literal_document_order_and_result_ownership(
    route, stream, beta
):
    seen = _serialized_contents(route)
    # A document lookalike inside tool arguments is opaque JSON, not a replayable document.
    args_a = {"example": _doc("not a message", beta)}
    args_b = {"section": ["east", "door"]}
    calls = [
        AistudioPart(function_call=("lookup", args_a, "A")),
        AistudioPart(function_call=("lookup", args_b, "B")),
    ]
    route.wire.reply(*calls)
    prompt = {
        "role": "user",
        "content": [
            _text("Before."),
            _doc(_BODY, beta),
            _text("After."),
            _doc(_URL, beta),
        ],
    }
    params = {"model": MODEL, "max_tokens": 32, "messages": [prompt]}
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as client:
        sdk = client.beta if beta else client
        prior = await _claude_sdk_turn(sdk, params, stream)
        assert [(block.id, block.name, block.input) for block in prior.content] == [
            ("A", "lookup", args_a),
            ("B", "lookup", args_b),
        ]
        route.wire.reply(AistudioPart(text="OK"))
        results = [
            ToolResultBlockParam(
                type="tool_result",
                tool_use_id="B",
                content=[_text("B start"), _doc(_BODY, beta), _text("B end")],
            ),
            ToolResultBlockParam(
                type="tool_result", tool_use_id="A", content=[_doc(_URL, beta)]
            ),
            _text("Follow up."),
        ]
        next_reply = await _claude_sdk_turn(
            sdk,
            {
                **params,
                "messages": [
                    prompt,
                    prior.to_param(),
                    {"role": "user", "content": results},
                ],
            },
            stream,
        )
    assert next_reply.content[0].text == "OK" and next_reply.stop_reason == "end_turn"
    before, continued = seen
    expected_prompt = [
        [AistudioPart(text=t).to_wire() for t in ("Before.", _BODY, "After.", _URL)],
        "user",
    ]
    b = AistudioPart(
        function_response=("lookup", {"result": "B start\n" + _BODY + "\nB end"}, "B")
    )
    a = AistudioPart(function_response=("lookup", {"result": _URL}, "A"))
    expected_result = [
        [b.to_wire(), a.to_wire(), AistudioPart(text="Follow up.").to_wire()],
        "user",
    ]
    assert (before, continued[-1]) == ([expected_prompt], expected_result)
    assert continued[0] == expected_prompt
    # The inputs are arbitrary JSON, not additional document positions.
    for part, expected in zip(continued[1][0], calls, strict=True):
        call = parse_response_part(part).function_call
        assert call is not None
        assert (call["name"], call["args"], call["call_id"]) == expected.function_call


@pytest.mark.parametrize("nested", [False, True])
async def test_count_tokens_still_counts_scalar_custom_content_as_typed_text(
    route, nested
):
    doc = _doc("Seconds on the clock. " * 80, False)
    content = (
        [{"type": "tool_result", "tool_use_id": "read", "content": [doc]}]
        if nested
        else [doc]
    )
    messages = [{"role": "user", "content": content}]
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as sdk:
        result = await sdk.messages.count_tokens(
            model=MODEL, messages=cast("list[MessageParam]", messages)
        )
    assert result.input_tokens == ascii_count(
        {"system": None, "tools": None, "messages": messages}
    )


async def test_literal_custom_document_can_stand_alone(route):
    seen = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="OK"))
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as sdk:
        reply = await sdk.messages.create(
            model=MODEL,
            max_tokens=32,
            messages=[{"role": "user", "content": [_doc(_BODY, False)]}],
        )
    assert reply.stop_reason == "end_turn"
    assert seen == [[[[AistudioPart(text=_BODY).to_wire()], "user"]]]
