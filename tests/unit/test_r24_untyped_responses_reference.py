"""Only the SDK's exact undiscriminated ItemReference shapes require stored state."""

import asyncio
from unittest.mock import Mock

import openai
import pytest
from openai.types.responses import ResponseFailedEvent
from openai.types.responses.response_input_param import ItemReference
from test_compat_media_routes import _client_options, _serialized_contents
from test_ordered_tool_routes import wire_source as wire_source
from test_r20_responses_file_refs import (
    no_media_client as no_media_client,
    reject_before_io as reject_before_io,
)
from test_r23_responses_state_refs import _NEXT, _PAST, _turn
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api import routes_compat
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_ID = "opaque-item-fixture"
_OMITTED = ItemReference(id=_ID)
_NULL = ItemReference(type=None, id=_ID)


@pytest.mark.parametrize("reference", [_OMITTED, _NULL], ids=["omitted", "null"])
@pytest.mark.parametrize("mixed", [False, True], ids=["alone", "after-media"])
@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
async def test_untyped_reference_fails_before_io(
    monkeypatch, reject_before_io, reference, mixed, delivery
):
    observer = _ASGIObserver()
    convert = routes_compat.responses_to_gemini_request
    seen = []

    async def observe_input(req):
        seen.append(req.input)
        if delivery == "committed-stream":
            await observer.heartbeat.wait()
        return await convert(req)

    monkeypatch.setattr(routes_compat, "responses_to_gemini_request", observe_input)
    monkeypatch.setattr(
        settings,
        "stream_heartbeat_interval",
        0.01 if delivery == "committed-stream" else 5,
    )
    charge = Mock(side_effect=AssertionError("unexpected inline media accounting"))
    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    items = [_PAST, _NEXT, reference, _NEXT] if mixed else [reference]
    async with asyncio.timeout(5), observer.client() as client:
        if delivery == "committed-stream":
            async with client.responses.stream(model=MODEL, input=items) as stream:
                events = [event async for event in stream]
            assert observer.statuses == [200] and observer.heartbeat.is_set()
            created, failed = events
            assert created.type == "response.created" and isinstance(
                failed, ResponseFailedEvent
            )
            assert [created.sequence_number, failed.sequence_number] == [0, 1]
            assert failed.response.id == created.response.id
            assert failed.response.status == "failed" and failed.response.output == []
            error = failed.response.error
            assert error is not None and error.code == "invalid_prompt"
            message = error.message
        else:
            with pytest.raises(openai.BadRequestError) as exc:
                await _turn(client, delivery == "early-stream", input=items)
            assert observer.statuses == [400] and not observer.heartbeat.is_set()
            assert exc.value.type == "invalid_request_error"
            message = exc.value.message
    assert seen == [items]  # The SDK sent the omitted/null discriminator as supplied.
    assert (
        f"input[{2 if mixed else 0}].id (item_reference) is not supported; "
        "send the complete conversation as inline input items" in message
    )
    assert _ID not in message
    charge.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
async def test_nonreference_shapes_and_id_bearing_inline_replay(route, stream):
    calls = _serialized_contents(route)
    route.wire.reply(
        AistudioPart(text="Short summary.", thought=True),
        AistudioPart(text="Running the check."),
        AistudioPart(function_call=("check", {}, "call-fixture")),
    )
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        first = await _turn(client, stream, input=_NEXT["content"])
        assert [item.type for item in first.output] == [
            "reasoning",
            "message",
            "function_call",
        ]
        history = [item.model_dump(exclude_none=True) for item in first.output]
        assert all(item.get("id") for item in history)
        result = {
            "type": "function_call_output",
            "call_id": "call-fixture",
            "output": "Done.",
        }
        route.wire.reply(AistudioPart(text="All set."))
        without_ids = [
            {key: value for key, value in item.items() if key != "id"}
            for item in history
        ]
        await _turn(client, stream, input=[_NEXT, *without_ids, result, _NEXT])
        placeholders = [
            {"id": _ID, "role": "user"},
            {"id": _ID, "type": None, "content": None},
            {"id": _ID, "metadata": {}},
            {"id": _ID, "type": None, "extension": None},
            {"id": _ID, "type": "unknown"},
            {"id": _ID, "type": ""},
            {"id": " \t\u2003"},
            {"type": None, "id": 7},
            {"type": None, "id": {"id": _ID}},
        ]
        inline = {
            "type": None,
            "id": _ID,
            "role": "user",
            "content": [_OMITTED, {"type": "input_text", "text": _NEXT["content"]}],
        }
        reply = await _turn(
            client,
            stream,
            input=[
                *placeholders,
                {**_NEXT, "id": _ID},
                *history,
                {**result, "id": _ID},
                inline,
            ],
            metadata={"id": _ID},
        )
    assert reply.status == "completed" and reply.output_text == "All set."
    assert len(calls) == 3 and calls[-1] == calls[-2]
