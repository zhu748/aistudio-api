"""A dot in a data URL parameter is metadata, not part of the decoded file."""

import base64
from typing import cast
from unittest.mock import AsyncMock

import openai
import pytest
from openai.types.responses import ResponseInputParam
from test_compat_media_routes import (
    _client_options,
    _responses_turn,
    _serialized_contents,
    _tool_call,
    _tool_result,
)
from test_signed_part_roundtrip import route as route

from aistudio_api.application import compat_shared
from aistudio_api.application.media_session import MediaSession
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_META = "data:text/plain;charset=utf-8;name=swatch.txt"
_BYTES = b"blue\r\n"
_DATA = base64.b64encode(_BYTES).decode("ascii")
_TEXT = "Read the sample."


def _input(owner: str, *files: dict[str, str]) -> ResponseInputParam:
    content = [{"type": "input_text", "text": _TEXT}, *files]
    if owner == "user":
        return cast("ResponseInputParam", [{"role": "user", "content": content}])
    return cast(
        "ResponseInputParam",
        [
            {"role": "user", "content": "Inspect the palette."},
            _tool_call("A"),
            _tool_result("A", content),
        ],
    )


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    client = AsyncMock(side_effect=AssertionError("unexpected media download"))
    monkeypatch.setattr(MediaSession, "get_client", client)
    yield
    client.assert_not_awaited()


@pytest.mark.parametrize("owner", ["user", "tool"])
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_dotted_file_param_keeps_bytes_and_outer_filename(
    route, owner, stream
):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    file = {"type": "input_file", "file_data": f"{_META};base64,{_DATA}"}
    items = _input(owner, {**file, "filename": "outer.txt"}, file)
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        response = await _responses_turn(client, items, stream)
    assert response.status == "completed" and response.output_text == "Noted."
    lead = (
        AistudioPart(text=_TEXT)
        if owner == "user"
        else AistudioPart(function_response=("read_swatch", {"result": _TEXT}, "A"))
    )
    parts = [
        lead,
        AistudioPart(inline_data=("text/plain", _DATA), inline_filename="outer.txt"),
        AistudioPart(inline_data=("text/plain", _DATA)),
    ]
    assert len(calls) == 1 and calls[0][-1] == [[p.to_wire() for p in parts], "user"]


@pytest.mark.parametrize(
    ("suffix", "limit", "error"),
    [
        ("," + _DATA, None, "must be an inline base64 data URL"),
        (";base64,!!!!", None, "contains invalid base64 data"),
        (";base64," + _DATA, len(_BYTES) - 1, "bytes limit"),
    ],
    ids=["missing-encoding", "bad-alphabet", "byte-limit"],
)
async def test_dotted_tool_file_keeps_inline_validation(
    route, monkeypatch, suffix, limit, error
):
    calls = _serialized_contents(route)
    if limit is not None:
        monkeypatch.setattr(compat_shared, "_MAX_MEDIA_BYTES", limit)
    url = _META + suffix
    items = _input("tool", {"type": "input_file", "file_data": url})
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        with pytest.raises(openai.BadRequestError) as exc:
            await _responses_turn(client, items, False)
    assert exc.value.status_code == 400 and not calls
    message = exc.value.response.text
    assert error in message and "swatch.txt" not in message and _DATA not in message
