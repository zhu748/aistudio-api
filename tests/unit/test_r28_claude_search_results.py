"""Do not generate from a Claude message after silently removing search evidence."""

import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock, Mock

import anthropic
import pytest
from anthropic.types import (
    CitationsConfigParam,
    ImageBlockParam,
    MessageParam,
    SearchResultBlockParam,
    TextBlockParam,
    ToolResultBlockParam,
    UserMessageParam,
)
from anthropic.types.beta import (
    BetaSearchResultBlockParam,
    BetaToolResultBlockParam,
    BetaUserMessageParam,
)
from test_compat_media_routes import _PNG, _client_options, _serialized_contents
from test_ordered_tool_routes import sse_events, wire_source as wire_source
from test_r8_claude_estimator_scope import ascii_count
from test_r21_chat_file_ref import (
    no_external_client as no_external_client,
    reject_before_io as reject_before_io,
)
from test_r23_claude_opaque_history import _client
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import (
    MODEL,
    _claude_sdk_turn,
    route as route,
    signature,
    thought,
)

from aistudio_api.api import routes_compat
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application import compat_claude
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_SOURCE = "https://fixture.invalid/private-catalog"
_TITLE = "Private exhibit index"
_QUOTES = ("First private excerpt.", "Second private excerpt.")
_URL = "https://fixture.invalid/earlier.png"
_TEXT = TextBlockParam(type="text", text="Compare the notes.")
_IMAGE = ImageBlockParam(
    type="image",
    source={"type": "base64", "media_type": "image/png", "data": _PNG["B"]},
)


def _search(beta=False, citations=False):
    kind = BetaSearchResultBlockParam if beta else SearchResultBlockParam
    hit = kind(
        type="search_result",
        source=_SOURCE,
        title=_TITLE,
        content=[{"type": "text", "text": text} for text in _QUOTES],
    )
    if citations:
        hit["citations"] = CitationsConfigParam(enabled=True)
    return hit


def _case(beta, mixed, nested):
    # Earlier media would be fetched/charged if validation ran during conversion.
    first = {
        "role": "user",
        "content": [{"type": "image", "source": {"type": "url", "url": _URL}}, _IMAGE],
    }
    user = BetaUserMessageParam if beta else UserMessageParam
    hit = _search(beta, citations=mixed)
    items = [_TEXT, _IMAGE, hit, _TEXT] if mixed else [hit]
    if not nested:
        message = user(role="user", content=items)
        if mixed:
            return [
                first,
                {"role": "assistant", "content": "Noted."},
                message,
            ], "messages[2].content[2]"
        return [message], "messages[0].content[0]"
    calls = [{"type": "tool_use", "id": "A", "name": "lookup", "input": {}}]
    result = BetaToolResultBlockParam if beta else ToolResultBlockParam
    results = [result(type="tool_result", tool_use_id="A", content=items)]
    if mixed:
        calls.append({**calls[0], "id": "B"})
        results.insert(0, result(type="tool_result", tool_use_id="B", content="Other."))
    return [
        first,
        {"role": "assistant", "content": calls},
        user(role="user", content=results),
    ], (
        "messages[2].content[1].content[2]"
        if mixed
        else "messages[2].content[0].content[0]"
    )


@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
@pytest.mark.parametrize("beta", [False, True], ids=["standard", "beta"])
@pytest.mark.parametrize("mixed", [False, True], ids=["only", "mixed"])
@pytest.mark.parametrize("nested", [False, True], ids=["direct", "tool-result"])
async def test_typed_search_result_fails_before_replay_media_or_dispatch(
    reject_before_io, monkeypatch, caplog, delivery, beta, mixed, nested
):
    chunks = []
    observer = _ASGIObserver(lambda event: chunks.append(event.get("body", b"")))
    replay = Mock(side_effect=AssertionError("unexpected history replay"))
    monkeypatch.setattr(compat_claude, "_history_call_part", replay)
    monkeypatch.setattr(compat_claude.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(settings, "nonstream_keepalive", False)
    if delivery == "committed-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.claude_to_gemini_request

        async def after_commit(req):
            await observer.heartbeat.wait()
            return await convert(req)

        monkeypatch.setattr(routes_compat, "claude_to_gemini_request", after_commit)

    messages, path = _case(beta, mixed, nested)
    params = {"model": MODEL, "max_tokens": 40, "messages": messages}
    async with asyncio.timeout(5), _client(observer) as sdk:
        with pytest.raises(anthropic.APIStatusError) as exc:
            await _claude_sdk_turn(
                sdk.beta if beta else sdk, params, delivery != "unary"
            )
    body = b"".join(chunks).decode()
    if delivery == "committed-stream":
        assert observer.statuses == [200] and observer.heartbeat.is_set()
        (error,) = sse_events(body)
    else:
        assert isinstance(exc.value, anthropic.BadRequestError)
        assert observer.statuses == [400] and not observer.heartbeat.is_set()
        error = json.loads(body)
    assert (
        error["type"] == "error" and error["error"]["type"] == "invalid_request_error"
    )
    message = error["error"]["message"]
    assert path in message and "search_result" in message and message in str(exc.value)
    assert all(word in message for word in ("plain text", "source", "citation"))
    assert all(
        value not in body and value not in caplog.text
        for value in (_SOURCE, _TITLE, *_QUOTES, _URL, _PNG["B"])
    )
    replay.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
async def test_plain_results_preserve_signatures_images_and_reversed_owners(
    route, stream
):
    capture = _serialized_contents(route)
    # An argument can contain arbitrarily shaped JSON, even a tool_result lookalike.
    args = {"example": {"type": "tool_result", "content": [_search()]}}
    owned = [
        thought("Read both.", "thought-owner"),
        AistudioPart(
            function_call=("lookup", args, "A"),
            thought_signature=signature("call-owner"),
        ),
        AistudioPart(function_call=("lookup", {"sample": "B"}, "B")),
    ]
    route.wire.reply(*owned)
    prompt = UserMessageParam(role="user", content=[_TEXT, _IMAGE])
    params = {
        "model": MODEL,
        "max_tokens": 64,
        "messages": [prompt],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as sdk:
        previous = await _claude_sdk_turn(sdk, params, stream)
        assert [b.id for b in previous.content if b.type == "tool_use"] == ["A", "B"]
        results = [
            ToolResultBlockParam(
                type="tool_result",
                tool_use_id="B",
                content=[
                    _TEXT,
                    _IMAGE,
                    TextBlockParam(type="text", text="  B: café.\n"),
                ],
            ),
            ToolResultBlockParam(
                type="tool_result", tool_use_id="A", content="A note."
            ),
            _TEXT,
        ]
        route.wire.reply(AistudioPart(text="Done."))
        reply = await _claude_sdk_turn(
            sdk,
            {
                **params,
                "messages": [
                    prompt,
                    previous.to_param(),
                    {"role": "user", "content": results},
                ],
            },
            stream,
        )
    assert reply.stop_reason == "end_turn" and reply.content[0].text == "Done."
    initial, continued = capture
    image = AistudioPart(inline_data=("image/png", _PNG["B"]))
    text = AistudioPart(text=_TEXT["text"])
    assert initial == [[[text.to_wire(), image.to_wire()], "user"]]
    assert continued[1] == [[part.to_wire() for part in owned], "model"]
    expected = [
        AistudioPart(
            function_response=(
                "lookup",
                {"result": _TEXT["text"] + "\n  B: café.\n"},
                "B",
            )
        ),
        image,
        AistudioPart(function_response=("lookup", {"result": "A note."}, "A")),
        text,
    ]
    assert continued[-1] == [[part.to_wire() for part in expected], "user"]


@pytest.mark.parametrize("role", ["user", "assistant", "other"])
async def test_only_user_attachment_positions_are_checked(role):
    hit = _search()
    result = {"type": "tool_result", "tool_use_id": "A", "content": [hit]}
    blocks = [{**result, "content": [result, {**_TEXT, "metadata": hit}]}, _TEXT]
    if role != "user":
        blocks = [hit, result, *blocks]
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": MODEL,
            "system": [_TEXT, result],
            "messages": [{"role": role, "content": blocks}],
        }
    )
    converted, _ = await compat_claude.claude_to_gemini_request(req)
    assert converted.contents[0].parts[-1].text == _TEXT["text"]


async def test_earlier_file_reference_keeps_its_error(reject_before_io):
    ref = {"type": "image", "source": {"type": "file", "file_id": "file_fixture"}}
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "A",
                            "content": [ref, _search()],
                        }
                    ],
                }
            ],
        }
    )
    with pytest.raises(
        ValueError, match=r"messages\[0\]\.content\[0\]\.content\[0\]\.source\.file_id"
    ):
        await compat_claude.claude_to_gemini_request(req)


@pytest.mark.parametrize("nested", [False, True], ids=["direct", "tool-result"])
async def test_count_tokens_still_estimates_supplied_search_evidence(
    reject_before_io, monkeypatch, nested
):
    messages, _ = _case(False, True, nested)
    convert = AsyncMock(
        side_effect=AssertionError("count_tokens must not convert history")
    )
    monkeypatch.setattr(routes_compat, "claude_to_gemini_request", convert)
    async with _client(_ASGIObserver()) as sdk:
        estimate = await sdk.messages.count_tokens(
            model=MODEL, messages=cast("list[MessageParam]", messages)
        )
    assert estimate.input_tokens == ascii_count(
        {"system": None, "tools": None, "messages": messages}
    )
    convert.assert_not_awaited()
