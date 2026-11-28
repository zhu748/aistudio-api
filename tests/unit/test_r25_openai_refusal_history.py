"""Existing assistant refusals are ordinary model history, not new safety decisions."""

from unittest.mock import AsyncMock

import openai
import pytest
from openai.types.chat import ChatCompletionAssistantMessageParam, ChatCompletionMessage
from openai.types.responses import (
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
)
from test_compat_media_routes import _PNG, _client_options, _serialized_contents
from test_r15_result_owners import _call, _output
from test_r21_chat_file_ref import _turn as chat_turn
from test_r23_responses_state_refs import _turn as responses_turn
from test_signed_part_roundtrip import FOLLOWUP, MODEL, PROMPT, route as route

from aistudio_api.api.schemas_compat import (
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application import compat_shared
from aistudio_api.application.compat_openai import openai_to_gemini_request
from aistudio_api.application.responses_service import responses_to_gemini_request
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_REASON = "  I can describe 青, but not a swatch I haven’t seen.\n"
_BLOCK = {"type": "refusal", "refusal": _REASON}


def _wire(role, *parts):
    return [
        [(AistudioPart(text=p) if isinstance(p, str) else p).to_wire() for p in parts],
        role,
    ]


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_refusal_blocks_keep_order_around_ordinary_tool_history(
    route, protocol, stream
):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    call, call_parts = _call(protocol, "read_note", 1)
    result, result_parts = _output(protocol, "Cobalt.", "read_note")

    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        if protocol == "chat":
            assistant = ChatCompletionAssistantMessageParam(
                role="assistant",
                content=[
                    {"type": "text", "text": "First."},
                    {"type": "text", "text": "Second."},
                    {"type": "refusal", "refusal": _REASON},
                    {"type": "text", "text": "Last."},
                ],
                refusal=_REASON,  # A scalar echo must not repeat the block.
                tool_calls=call["tool_calls"],
            )
            reply = await chat_turn(
                client, [PROMPT, assistant, result, FOLLOWUP], stream
            )
            assert reply.choices[0].finish_reason == "stop"
            assert reply.choices[0].message.content == "Noted."
            prefix = ["First.\n\nSecond."]
        else:
            assistant = ResponseOutputMessage(
                id="msg-note",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text", text="First.", annotations=[]
                    ),
                    ResponseOutputText(
                        type="output_text", text="Second.", annotations=[]
                    ),
                    ResponseOutputRefusal(type="refusal", refusal=_REASON),
                    ResponseOutputText(
                        type="output_text", text="Last.", annotations=[]
                    ),
                ],
            )
            reply = await responses_turn(
                client, stream, input=[PROMPT, assistant, call, result, FOLLOWUP]
            )
            assert reply.status == "completed" and reply.output_text == "Noted."
            prefix = ["First.", "Second."]
    assert calls == [
        [
            _wire("user", PROMPT["content"]),
            _wire("model", *prefix, _REASON, "Last.", *call_parts),
            _wire("user", *result_parts, FOLLOWUP["content"]),
        ]
    ]


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_sdk_scalar_refusal_only_alongside_text_and_exact_mirror(
    route, stream
):
    route.wire.reply(AistudioPart(text="Noted."))
    messages: list[object] = [PROMPT]
    for content in (None, "An earlier note.", _REASON):
        messages.extend(
            [
                ChatCompletionMessage(
                    role="assistant", content=content, refusal=_REASON
                ),
                FOLLOWUP,
            ]
        )
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        reply = await chat_turn(client, messages, stream)
    assert reply.choices[0].message.content == "Noted."
    assert (
        route.wire.model_wire
        == _wire("model", _REASON, "An earlier note.", _REASON, _REASON)[0]
    )


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_role_boundaries_placeholders_and_ordinary_text_unchanged(protocol):
    ordinary = [{"type": "text", "text": "A"}, _BLOCK, {"type": "text", "text": "B"}]
    placeholders = [
        {"type": "refusal", "refusal": x} for x in (None, 42, [], {}, "", " \t")
    ]
    messages = [
        {"role": role, "content": ordinary, "refusal": _REASON}
        for role in ("system", "developer", "user")
    ] + [
        {
            "role": "assistant",
            "refusal": {"ignored": _REASON},
            "content": [
                ordinary[0],
                *placeholders,
                {"type": "other", "refusal": _REASON},
                ordinary[-1],
            ],
        },
        {"role": "assistant", "content": "No change.", "refusal": 12},
    ]
    if protocol == "chat":
        request = OpenAIChatCompletionRequest.model_validate(
            {"model": MODEL, "messages": messages}
        )
        converted, _ = await openai_to_gemini_request(request)
        expected_user, expected_model = ["A", _REASON, "B"], ["A\n\nB"]
    else:
        request = OpenAIResponsesRequest.model_validate(
            {"model": MODEL, "input": messages}
        )
        converted, _ = await responses_to_gemini_request(request)
        expected_user, expected_model = ["A", "B"], ["A", "B"]
    assert converted.systemInstruction is not None
    assert [p.text for p in converted.systemInstruction.parts] == ["A\n\nB\n\nA\n\nB"]
    assert [(t.role, [p.text for p in t.parts]) for t in converted.contents] == [
        ("user", expected_user),
        ("model", expected_model),
        ("model", ["No change."]),
    ]


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_mixed_assistant_media_keeps_refusals_on_their_own_side(
    monkeypatch, protocol
):
    url = "https://fixture.invalid/swatch.png"
    fetch = AsyncMock(return_value=("image/png", _PNG["A"]))
    monkeypatch.setattr(compat_shared, "_fetch_remote_media", fetch)
    if protocol == "chat":
        image = {"type": "image_url", "image_url": {"url": url}}
        content = [_BLOCK, image, _BLOCK]
        converted, _ = await openai_to_gemini_request(
            OpenAIChatCompletionRequest.model_validate(
                {
                    "model": MODEL,
                    "messages": [{"role": "assistant", "content": content}],
                }
            )
        )
    else:
        image = {"type": "input_image", "image_url": url}
        content = [_BLOCK, image, _BLOCK]
        converted, _ = await responses_to_gemini_request(
            OpenAIResponsesRequest.model_validate(
                {"model": MODEL, "input": [{"role": "assistant", "content": content}]}
            )
        )
    fetch.assert_awaited_once_with(url)
    (turn,) = converted.contents
    assert turn.role == "model"
    before, media, after = turn.parts
    assert (
        before.model_dump(exclude_none=True)
        == after.model_dump(exclude_none=True)
        == {"text": _REASON}
    )
    assert media.inlineData is not None and media.inlineData.data == _PNG["A"]
