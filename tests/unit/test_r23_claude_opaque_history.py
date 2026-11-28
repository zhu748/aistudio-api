"""Claude Messages must not silently omit opaque imported assistant history."""

import asyncio
import json

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from test_compat_media_routes import _PNG, _client_options, _serialized_contents
from test_ordered_tool_routes import sse_events, wire_source as wire_source
from test_r8_claude_estimator_scope import ascii_count
from test_r21_chat_file_ref import reject_before_io as reject_before_io
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import (
    FOLLOWUP,
    HEADERS,
    MODEL,
    PROMPT,
    _claude_sdk_turn,
    route as route,
    thought,
)

from aistudio_api.api import routes_compat
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_DATA = "redacted-provider-fixture"
_SIG = "foreign-provider-fixture"
_RED = {"type": "redacted_thinking", "data": _DATA}
_EMPTY = {"type": "thinking", "thinking": "", "signature": _SIG}
_ANSWER = {"type": "text", "text": "Earlier answer."}
_URL = "https://fixture.invalid/earlier.png"
_MEDIA = [
    {"type": "image", "source": {"type": "url", "url": _URL}},
    {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG["A"]},
    },
]


def _imported(blocks):
    # Echo the stock SDK's actual replay shape, not an invented history model.
    return Message.model_validate(
        {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": blocks,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 4},
        },
        strict=True,
    ).to_param()


def _client(observer):
    return anthropic.AsyncAnthropic(
        api_key="fixture",
        base_url="http://fixture.invalid",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(observer)),
        default_headers=HEADERS,
        max_retries=0,
        _strict_response_validation=True,
    )


@pytest.mark.parametrize(
    ("block", "field"),
    [(_RED, "redacted_thinking.data"), (_EMPTY, "thinking.signature")],
)
@pytest.mark.parametrize("mixed", [False, True], ids=["only", "mixed"])
@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
async def test_rejects_missing_history_before_media_or_prepare(
    reject_before_io, monkeypatch, block, field, mixed, delivery
):
    # Media in a prior turn must not be fetched or charged during preflight.
    messages = [
        {"role": "user", "content": _MEDIA},
        _imported([_ANSWER, block] if mixed else [block]),
        FOLLOWUP,
    ]
    bodies = []
    observer = _ASGIObserver(lambda event: bodies.append(event.get("body", b"")))
    monkeypatch.setattr(settings, "nonstream_keepalive", False)
    if delivery == "committed-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.claude_to_gemini_request

        async def after_commit(req):
            await observer.heartbeat.wait()
            return await convert(req)

        monkeypatch.setattr(routes_compat, "claude_to_gemini_request", after_commit)

    async with asyncio.timeout(5), _client(observer) as client:
        with pytest.raises(anthropic.APIStatusError) as exc:
            await _claude_sdk_turn(
                client,
                {"model": MODEL, "max_tokens": 40, "messages": messages},
                delivery != "unary",
            )
    body = b"".join(bodies).decode()
    if delivery == "committed-stream":
        assert observer.statuses == [200] and observer.heartbeat.is_set()
        # Only a single terminal error, no successful message_start/stop.
        (error,) = sse_events(body)
    else:
        assert isinstance(exc.value, anthropic.BadRequestError)
        assert observer.statuses == [400] and not observer.heartbeat.is_set()
        error = json.loads(body)
    assert (
        error["type"] == "error" and error["error"]["type"] == "invalid_request_error"
    )
    message = error["error"]["message"]
    assert f"messages[1].content[{int(mixed)}].{field}" in message
    assert "original provider" in message and "visible transcript" in message
    assert message in str(exc.value)
    for value in (_SIG, _DATA, _ANSWER["text"], _PNG["A"], _URL):
        assert value not in body


@pytest.mark.parametrize("stream", [False, True])
async def test_owned_replay_and_visible_foreign_thoughts_keep_their_parts(
    route, stream
):
    calls = _serialized_contents(route)
    owned = [thought("", "owned empty"), thought("Note.", "owned nonempty")]
    answer = AistudioPart(text=_ANSWER["text"])
    route.wire.reply(*owned, answer)
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as client:
        params = {"model": MODEL, "max_tokens": 40, "messages": [PROMPT]}
        previous = await _claude_sdk_turn(client, params, stream)
        route.wire.reply(AistudioPart(text="Continuing."))
        for prior, expected in [
            (previous.to_param(), [*owned, answer]),
            (
                _imported(
                    [
                        {**_EMPTY, "thinking": "  藍\r\n"},
                        _ANSWER,
                        {**_EMPTY, "thinking": " \t\u2002"},
                    ]
                ),
                [
                    AistudioPart(text="  藍\r\n", thought=True),
                    answer,
                    AistudioPart(text=" \t\u2002", thought=True),
                ],
            ),
        ]:
            result = await _claude_sdk_turn(
                client, {**params, "messages": [PROMPT, prior, FOLLOWUP]}, stream
            )
            assert result.stop_reason == "end_turn"
            assert calls[-1][1] == [[part.to_wire() for part in expected], "model"]


@pytest.mark.parametrize("role", ["user", "other"])
async def test_placeholders_and_non_assistant_roles_remain_ignored(route, role):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Continuing."))
    placeholders = [
        {"type": "redacted_thinking"},
        {"type": "thinking", "signature": _SIG},
        {**_EMPTY, "thinking": None},
        {**_EMPTY, "thinking": 0},
        {"type": "thinking", "thinking": ""},
    ]
    for value in (None, "", " \t", 7, {"nested": _SIG}):
        placeholders.extend([{**_RED, "data": value}, {**_EMPTY, "signature": value}])
    messages = [
        {"role": role, "content": [_RED, _EMPTY, {"type": "text", "text": "Start."}]},
        {"role": "assistant", "content": [*placeholders, _ANSWER]},
        FOLLOWUP,
    ]
    reply = await route.raw.post(
        "/v1/messages", json={"model": MODEL, "max_tokens": 40, "messages": messages}
    )
    assert reply.status_code == 200 and reply.json()["stop_reason"] == "end_turn"
    assert calls == [
        [
            [[AistudioPart(text="Start.").to_wire()], "user"],
            [[AistudioPart(text=_ANSWER["text"]).to_wire()], "model"],
            [[AistudioPart(text=FOLLOWUP["content"]).to_wire()], "user"],
        ]
    ]


async def test_count_tokens_still_estimates_the_supplied_opaque_history(
    reject_before_io,
):
    blocks = [_RED, _EMPTY, _ANSWER]
    messages = [PROMPT, _imported(blocks), FOLLOWUP]
    async with _client(_ASGIObserver()) as client:
        estimate = await client.messages.count_tokens(model=MODEL, messages=messages)
    assert estimate.input_tokens == ascii_count(
        {
            "system": None,
            "tools": None,
            "messages": [PROMPT, {"role": "assistant", "content": blocks}, FOLLOWUP],
        }
    )
