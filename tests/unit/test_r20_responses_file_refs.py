"""Ordinary Responses user messages must not silently lose referenced attachments."""

import asyncio
import base64
from typing import cast
from unittest.mock import AsyncMock

import openai
import pytest
from openai.types.responses import (
    ResponseFailedEvent,
    ResponseInputFileParam,
    ResponseInputImageParam,
    ResponseInputParam,
)
from test_compat_media_routes import (
    _PNG,
    _client_options,
    _responses_turn,
    _serialized_contents,
)
from test_ordered_tool_routes import wire_source as wire_source
from test_r16_claude_tool_pdfs import _PDF
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import compat_shared
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_ID = "fixture-private-reference"
_URL = "https://fixture.invalid/private-report.pdf"
_REMOTE_IMAGE = "https://fixture.invalid/swatch.png"
_IMAGE_ID = ResponseInputImageParam(type="input_image", detail="auto", file_id=_ID)
_FILE_ID = ResponseInputFileParam(type="input_file", file_id=_ID)
_FILE_URL = ResponseInputFileParam(type="input_file", file_url=_URL)
_TEXT = {"type": "input_text", "text": "请比较附件的颜色。"}
_REMOTE = {"type": "input_image", "detail": "auto", "image_url": _REMOTE_IMAGE}


@pytest.fixture(autouse=True)
def no_media_client(monkeypatch):
    client = AsyncMock(side_effect=AssertionError("unexpected external media client"))
    monkeypatch.setattr(MediaSession, "get_client", client)
    yield
    client.assert_not_awaited()


@pytest.fixture
def reject_before_io(monkeypatch, wire_source):
    prepare = app.dependency_overrides[get_client]().prepare_request
    dispatch = AsyncMock(side_effect=AssertionError("unexpected dispatch"))
    fetch = AsyncMock(side_effect=AssertionError("unexpected media fetch"))
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", dispatch)
    monkeypatch.setattr(compat_shared, "_fetch_remote_media", fetch)
    monkeypatch.setattr(settings, "request_history", False)
    yield
    dispatch.assert_not_awaited()
    prepare.assert_not_awaited()
    fetch.assert_not_awaited()


@pytest.mark.parametrize(
    ("reference", "field", "alternative"),
    [
        (_IMAGE_ID, "file_id", "image_url"),
        (_FILE_ID, "file_id", "file_data"),
        (_FILE_URL, "file_url", "file_data"),
    ],
    ids=["image-id", "file-id", "file-url"],
)
@pytest.mark.parametrize("shape", ["only", "mixed", "followup"])
@pytest.mark.parametrize("delivery", ["unary", "early-stream", "late-stream"])
async def test_unsupported_reference_fails_whole_turn(
    reject_before_io, monkeypatch, reference, field, alternative, shape, delivery
):
    if shape == "only":
        items = [{"role": "user", "content": [reference]}]
        path = "input[0].content[0]"
    elif shape == "mixed":
        items = [
            {
                "type": "message",
                "role": "user",
                "content": [_TEXT, _REMOTE, reference, _TEXT],
            }
        ]
        path = "input[0].content[2]"
    else:
        items = [
            {"role": "user", "content": [_REMOTE, _TEXT]},
            {"role": "assistant", "content": "Noted."},
            {"type": "message", "role": "user", "content": [reference]},
        ]
        path = "input[2].content[0]"

    observer = _ASGIObserver()
    if delivery == "late-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.responses_to_gemini_request

        async def after_first_heartbeat(req):
            await observer.heartbeat.wait()
            return await convert(req)

        monkeypatch.setattr(
            routes_compat, "responses_to_gemini_request", after_first_heartbeat
        )
    else:
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 5)
    input_items = cast("ResponseInputParam", items)
    async with asyncio.timeout(5), observer.client() as client:
        if delivery == "late-stream":
            async with client.responses.stream(
                model=MODEL, input=input_items
            ) as stream:
                events = [event async for event in stream]
            assert observer.heartbeat.is_set() and observer.statuses == [200]
            assert [event.type for event in events] == [
                "response.created",
                "response.failed",
            ]
            assert [event.sequence_number for event in events] == [0, 1]
            created, failed = events
            assert created.type == "response.created" and isinstance(
                failed, ResponseFailedEvent
            )
            assert (
                failed.response.id == created.response.id and not failed.response.output
            )
            error = failed.response.error
            assert error is not None and error.code == "invalid_prompt"
            message = error.message
        else:
            with pytest.raises(openai.BadRequestError) as exc:
                await _responses_turn(client, input_items, delivery == "early-stream")
            assert observer.statuses == [400] and not observer.heartbeat.is_set()
            assert exc.value.type == "invalid_request_error"
            message = exc.value.message
    assert f"{path}.{field}" in message and alternative in message
    assert _ID not in message and "fixture.invalid" not in message


@pytest.mark.parametrize(
    "part",
    [
        {**_IMAGE_ID, "image_url": {"url": _REMOTE_IMAGE}},
        {**_IMAGE_ID, "image_url": "data:malformed," + _PNG["A"]},
        {**_IMAGE_ID, "image_url": "https://"},
        {**_IMAGE_ID, "image_url": "https://fixture.invalid:not-a-port/swatch.png"},
        {**_IMAGE_ID, "image_url": "  "},
        {**_FILE_URL, "file_data": 42},
        {**_FILE_ID, "file_data": _PDF["A"]},
        {**_FILE_URL, "file_data": "data:application/pdf;base64,"},
        # These are not source shapes the existing file converter consumes.
        {**_FILE_ID, "file_data": " DATA:application/pdf;base64," + _PDF["A"]},
    ],
)
async def test_unusable_primary_does_not_hide_reference(reject_before_io, part):
    input_items = cast(
        "ResponseInputParam", [{"role": "user", "content": [_TEXT, part]}]
    )
    async with _ASGIObserver().client() as client:
        with pytest.raises(openai.BadRequestError) as exc:
            await client.responses.create(model=MODEL, input=input_items)
    field = "file_url" if "file_url" in part else "file_id"
    assert f"input[0].content[1].{field}" in exc.value.message
    for private in (_ID, "fixture.invalid", _PNG["A"], _PDF["A"]):
        assert private not in exc.value.message


@pytest.mark.parametrize("stream", [False, True])
async def test_supported_sources_win_without_changing_order_or_media_budget(
    route, monkeypatch, stream
):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    # Preflight must not pre-charge either inline attachment. A supported HTTP
    # image is still read normally; its secondary file_id is never resolved.
    monkeypatch.setattr(
        settings,
        "max_request_bytes",
        sum(len(base64.b64decode(d)) for d in (_PNG["A"], _PDF["A"])),
    )
    fetch = AsyncMock(return_value=("image/png", _PNG["B"]))
    monkeypatch.setattr(compat_shared, "_fetch_remote_media", fetch)
    content = [
        {**_TEXT, "file_url": _URL},
        {**_IMAGE_ID, "image_url": " DATA:image/png;base64," + _PNG["A"] + " "},
        {
            **_FILE_ID,
            "file_url": _URL,
            "filename": "图表.pdf",
            "file_data": "data:application/pdf;base64," + _PDF["A"],
        },
        {**_REMOTE, "file_id": _ID},
        {"type": "input_file", "file_id": None, "file_url": "  "},
        {"type": "input_image", "file_id": "", "file_url": _URL},
        {"type": "input_image", "file_id": 42},
        {"type": "input_text", "text": "End."},
    ]
    # Only ordinary user content is subject to the reference check.
    items = [
        {"role": "user", "content": "Earlier."},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Previous."}, _IMAGE_ID],
        },
        {"type": "message", "role": "user", "content": content},
    ]
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        result = await _responses_turn(
            client, cast("ResponseInputParam", items), stream
        )
    assert result.status == "completed" and result.output_text == "Noted."
    fetch.assert_awaited_once_with(_REMOTE_IMAGE)
    expected = [
        AistudioPart(text=_TEXT["text"]),
        AistudioPart(inline_data=("image/png", _PNG["A"])),
        AistudioPart(
            inline_data=("application/pdf", _PDF["A"]), inline_filename="图表.pdf"
        ),
        AistudioPart(inline_data=("image/png", _PNG["B"])),
        AistudioPart(text="End."),
    ]
    assert calls == [
        [
            [[AistudioPart(text="Earlier.").to_wire()], "user"],
            [[AistudioPart(text="Previous.").to_wire()], "model"],
            [[part.to_wire() for part in expected], "user"],
        ]
    ]


@pytest.mark.parametrize("stream", [False, True])
async def test_references_in_function_output_keep_omission_notices(route, stream):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    items = [
        {"role": "user", "content": "Describe the swatch."},
        {"type": "function_call", "name": "swatch", "call_id": "A", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "A",
            "output": [_TEXT, _IMAGE_ID, _FILE_ID, _FILE_URL, _TEXT],
        },
    ]
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        result = await _responses_turn(
            client, cast("ResponseInputParam", items), stream
        )
    assert result.output_text == "Noted."
    notice = "\n".join(
        [
            _TEXT["text"],
            "[image omitted]",
            "[file omitted]",
            "[file omitted]",
            _TEXT["text"],
        ]
    )
    part = AistudioPart(function_response=("swatch", {"result": notice}, "A"))
    assert calls[-1][-1] == [[part.to_wire()], "user"]
