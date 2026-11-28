"""A stateless Responses route must not silently discard an explicit history ID."""

import asyncio
from unittest.mock import Mock

import openai
import pytest
from openai.types.responses import (
    ResponseConversationParamParam,
    ResponseFailedEvent,
    ResponseOutputMessage,
)
from openai.types.responses.response_input_param import ItemReference
from test_compat_media_routes import _PNG, _client_options, _serialized_contents
from test_ordered_tool_routes import wire_source as wire_source
from test_r20_responses_file_refs import (
    no_media_client as no_media_client,
    reject_before_io as reject_before_io,
)
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api import routes_compat
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_CONV_ID = "private-conversation-fixture"
_REF = ItemReference(type="item_reference", id="private-item-fixture")
_PREV_ID = "private-response-fixture"
_INLINE = {"type": "input_image", "image_url": "data:image/png;base64," + _PNG["A"]}
_REMOTE = {"type": "input_image", "image_url": "https://fixture.invalid/swatch"}
_PAST = {"role": "user", "content": [_INLINE, _REMOTE]}
_NEXT = {"role": "user", "content": "What did the earlier swatch show?"}


async def _turn(client, stream, **kwargs):
    if stream:
        async with client.responses.stream(model=MODEL, **kwargs) as response:
            return await response.get_final_response()
    return await client.responses.create(model=MODEL, **kwargs)


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"conversation": _CONV_ID}, "conversation"),
        (
            {"conversation": ResponseConversationParamParam(id=_CONV_ID)},
            "conversation.id",
        ),
        ({"input": [_REF]}, "input[0].id"),
        (
            {"input": [_PAST, {"role": "assistant", "content": "OK."}, _REF, _NEXT]},
            "input[2].id",
        ),
        (
            {
                "previous_response_id": _PREV_ID,
                "conversation": _CONV_ID,
                "input": [_REF],
            },
            "previous_response_id",
        ),
    ],
    ids=[
        "conversation-id",
        "conversation-object",
        "reference-only",
        "mixed-history",
        "previous-id-precedence",
    ],
)
@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
async def test_state_reference_stops_the_whole_request(
    reject_before_io, monkeypatch, params, field, delivery
):
    observer = _ASGIObserver()
    charge = Mock(side_effect=AssertionError("unexpected inline media charge"))
    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 5)
    if delivery == "committed-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.responses_to_gemini_request

        async def after_commit(req):
            await observer.heartbeat.wait()
            return await convert(req)

        monkeypatch.setattr(routes_compat, "responses_to_gemini_request", after_commit)

    kwargs = {"input": [_PAST, _NEXT], **params}
    async with asyncio.timeout(5), observer.client() as client:
        if delivery == "committed-stream":
            async with client.responses.stream(model=MODEL, **kwargs) as stream:
                events = [event async for event in stream]
            assert observer.statuses == [200] and observer.heartbeat.is_set()
            assert [(e.type, e.sequence_number) for e in events] == [
                ("response.created", 0),
                ("response.failed", 1),
            ]
            created, failed = events
            assert created.type == "response.created" and isinstance(
                failed, ResponseFailedEvent
            )
            assert created.response.id == failed.response.id
            assert failed.response.status == "failed" and not failed.response.output
            assert (
                failed.response.error is not None
                and failed.response.error.code == "invalid_prompt"
            )
            message = failed.response.error.message
        else:
            with pytest.raises(openai.BadRequestError) as exc:
                await _turn(client, delivery == "early-stream", **kwargs)
            assert observer.statuses == [400] and not observer.heartbeat.is_set()
            assert exc.value.type == "invalid_request_error"
            message = exc.value.message
    assert (
        field
        + (" (item_reference)" if field.startswith("input[") else "")
        + " is not supported"
        in message
    )
    if field == "previous_response_id":
        assert "send the complete conversation in input" in message
    else:
        assert "complete conversation" in message and "inline input items" in message
    for private in (_CONV_ID, _REF["id"], _PREV_ID, _PNG["A"], "fixture.invalid"):
        assert private not in message
    charge.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
async def test_inline_id_bearing_history_and_irrelevant_placeholders_still_work(
    route, stream
):
    wire = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="A green swatch."))
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        first = await _turn(client, stream, input="Earlier swatch.")
        answer = first.output[0]
        assert isinstance(answer, ResponseOutputMessage) and answer.id
        history = [
            {"role": "user", "content": [_INLINE]},
            answer.model_dump(exclude_none=True),
            {"type": "item_reference"},
            *(
                {"type": "item_reference", "id": value}
                for value in (None, "", " \t", 5, {"id": _REF["id"]})
            ),
            {**_NEXT, "id": _REF["id"]},
        ]
        expected = [
            [[AistudioPart(inline_data=("image/png", _PNG["A"])).to_wire()], "user"],
            [[AistudioPart(text="A green swatch.").to_wire()], "model"],
            [[AistudioPart(text=_NEXT["content"]).to_wire()], "user"],
        ]
        for value in (
            None,
            "",
            " \t",
            5,
            [_CONV_ID],
            {},
            {"id": ""},
            {"id": None},
            {"id": 42},
            {"id": " \t"},
        ):
            result = await _turn(
                client,
                stream,
                input=history,
                extra_body={"conversation": value},
                metadata={"conversation": _CONV_ID, "item_reference": _REF["id"]},
            )
            assert result.status == "completed" and wire[-1] == expected
