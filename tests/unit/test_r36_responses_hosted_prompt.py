"""A hosted Responses prompt is a template reference, not optional metadata."""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import openai
import pytest
from openai.types.responses import (
    ResponseFailedEvent,
    ResponseInputParam,
    ResponsePromptParam,
)
from test_compat_media_routes import _client_options
from test_ordered_tool_routes import wire_source as wire_source
from test_r20_responses_file_refs import (
    no_media_client as no_media_client,
    reject_before_io as reject_before_io,
)
from test_r23_responses_state_refs import _NEXT, _PAST, _turn
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

_ID = "pmpt_fixture_private"
_VERSION = "938172649150384927"
_VALUE = "private-shade-value"
_URL = "https://fixture.invalid/private-attachment"
_PROMPT = ResponsePromptParam(
    id=_ID,
    version=_VERSION,
    variables={
        "private_shade": _VALUE,
        "private_image": {"type": "input_image", "detail": "auto", "image_url": _URL},
    },
)


@pytest.mark.parametrize("mixed", [False, True], ids=["bare-id", "variables-and-media"])
@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
async def test_hosted_prompt_fails_before_conversion_io(
    monkeypatch, reject_before_io, mixed, delivery
):
    body = bytearray()
    observer = _ASGIObserver(lambda frame: body.extend(frame.get("body", b"")))
    charge = Mock(side_effect=AssertionError("unexpected inline media charge"))
    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 5)
    if delivery == "committed-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.responses_to_gemini_request

        async def after_commit(request):
            await observer.heartbeat.wait()
            return await convert(request)

        monkeypatch.setattr(routes_compat, "responses_to_gemini_request", after_commit)

    prompt = _PROMPT if mixed else ResponsePromptParam(id="  opaque-fixture-ref  ")
    items = cast("ResponseInputParam", [_PAST, _NEXT]) if mixed else "Continue."
    async with asyncio.timeout(5), observer.client() as client:
        if delivery == "committed-stream":
            async with client.responses.stream(
                model=MODEL, prompt=prompt, input=items
            ) as stream:
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
                await _turn(
                    client, delivery == "early-stream", prompt=prompt, input=items
                )
            assert observer.statuses == [400] and not observer.heartbeat.is_set()
            assert exc.value.type == "invalid_request_error"
            message = exc.value.message
    assert "prompt.id is not supported" in message
    assert all(word in message for word in ("inline", "instructions", "input"))
    for private in (
        _ID,
        _VERSION,
        "opaque-fixture-ref",
        "private_shade",
        "private_image",
        _VALUE,
        _URL,
    ):
        assert private.encode() not in body
    charge.assert_not_called()


@pytest.mark.parametrize(
    ("state", "field"),
    [
        ({"conversation": {"id": _ID}}, "conversation.id"),
        (
            {"previous_response_id": _ID, "conversation": {"id": _ID}},
            "previous_response_id",
        ),
    ],
)
async def test_prompt_does_not_mask_an_earlier_state_error(
    reject_before_io, state, field
):
    async with _ASGIObserver().client() as client:
        with pytest.raises(openai.BadRequestError) as exc:
            await _turn(client, False, prompt=_PROMPT, input="Continue.", **state)
    assert field + " is not supported" in exc.value.message
    assert "prompt.id" not in exc.value.message and _ID not in exc.value.message


@pytest.mark.parametrize("stream", [False, True])
async def test_nonreference_placeholders_preserve_inline_instructions_and_input(
    route, stream
):
    prepare = AsyncMock(wraps=route.wire.prepare)
    route.app.dependency_overrides[get_client]().prepare_request = prepare
    route.wire.reply(AistudioPart(text="Noted."))
    instructions = "Return the text literally."
    text = "The label is {{private_shade}}."
    cases = [
        {},  # Omitted.
        *(
            {"prompt": value}
            for value in (
                None,
                _ID,
                [_PROMPT],
                {},
                {"version": _VERSION, "variables": _PROMPT.get("variables")},
                {"id": None},
                {"id": 7},
                {"id": ""},
                {"id": " \t"},
                {"id": {"id": _ID}},
            )
        ),
    ]
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        for extra in cases:
            reply = await _turn(
                client,
                stream,
                instructions=instructions,
                input=text,
                metadata={"prompt": _ID},
                prompt_cache_key=_ID,
                extra_body=extra,
            )
            assert reply.status == "completed" and reply.output_text == "Noted."
    assert prepare.await_count == len(cases)
    for call in prepare.await_args_list:
        assert call.kwargs["contents"] == [
            AistudioContent(role="user", parts=[AistudioPart(text=text)])
        ]
        assert call.kwargs["system_instruction_content"] == AistudioContent(
            role="user", parts=[AistudioPart(text=instructions)]
        )
