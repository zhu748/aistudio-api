"""Ordinary Chat user files: reject unresolvable IDs without losing attachments."""

import asyncio
import base64
from typing import cast
from unittest.mock import AsyncMock, Mock

import openai
import pytest
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.chat_completion_content_part_param import File, FileFile
from test_compat_media_routes import _client_options, _serialized_contents
from test_ordered_tool_routes import wire_source as wire_source
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import HEADERS, MODEL, route as route

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import compat_shared
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_ID = "file-private-fixture"
_NAME = "色見本.txt"
_BYTES = "A swatch: 藍".encode()
_B64 = base64.b64encode(_BYTES).decode()
_DATA = "data:text/plain;base64," + _B64
_REF = File(type="file", file=FileFile(file_id=_ID, filename=_NAME))
_INLINE = File(type="file", file=FileFile(file_data=_DATA, filename=_NAME))
_TEXT = {"type": "text", "text": "Read the swatch."}
_IMAGE = {"type": "image_url", "image_url": {"url": "https://fixture.invalid/p.png"}}


def _user(content):
    return {"role": "user", "content": content}


async def _turn(client: openai.AsyncOpenAI, messages, stream: bool):
    messages = cast("list[ChatCompletionMessageParam]", messages)
    if stream:
        async with client.chat.completions.stream(
            model=MODEL, messages=messages, extra_headers=HEADERS
        ) as result:
            return await result.get_final_completion()
    return await client.chat.completions.create(
        model=MODEL, messages=messages, extra_headers=HEADERS
    )


@pytest.fixture(autouse=True)
def no_external_client(monkeypatch):
    client = AsyncMock(side_effect=AssertionError("unexpected media client"))
    monkeypatch.setattr(MediaSession, "get_client", client)
    yield
    client.assert_not_awaited()


@pytest.fixture
def reject_before_io(monkeypatch, wire_source):
    prepare = app.dependency_overrides[get_client]().prepare_request
    dispatch = AsyncMock(side_effect=AssertionError("unexpected dispatch"))
    fetch = AsyncMock(side_effect=AssertionError("unexpected media fetch"))
    charge = Mock(side_effect=AssertionError("unexpected media charge"))
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", dispatch)
    monkeypatch.setattr(compat_shared, "_fetch_remote_media", fetch)
    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 5)
    yield
    dispatch.assert_not_awaited()
    prepare.assert_not_awaited()
    fetch.assert_not_awaited()
    charge.assert_not_called()


@pytest.mark.parametrize("delivery", ["unary", "early-stream", "late-stream"])
@pytest.mark.parametrize(
    ("messages", "path"),
    [
        ([_user([_REF])], "messages[0].content[0]"),
        (
            [
                _user([_TEXT, _REF]),
                {"role": "assistant", "content": "Noted."},
                _user("Continue."),
            ],
            "messages[0].content[1]",
        ),
        ([_user([_TEXT, _IMAGE, _INLINE, _REF])], "messages[0].content[3]"),
        (
            [
                _user([_IMAGE]),
                {"role": "assistant", "content": [_IMAGE]},
                _user([_TEXT, _REF]),
            ],
            "messages[2].content[1]",
        ),
    ],
    ids=["only-file", "replayed-history", "same-turn-media", "prior-turn-media"],
)
async def test_file_id_rejects_whole_request(
    reject_before_io, monkeypatch, messages, path, delivery
):
    bodies: list[bytes] = []
    observer = _ASGIObserver(lambda msg: bodies.append(msg.get("body", b"")))
    if delivery == "late-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.openai_to_gemini_request

        async def after_heartbeat(req):
            await observer.heartbeat.wait()
            return await convert(req)

        monkeypatch.setattr(routes_compat, "openai_to_gemini_request", after_heartbeat)

    async with asyncio.timeout(5), observer.client() as client:
        with pytest.raises(openai.APIError) as exc:
            await _turn(client, messages, delivery != "unary")
    error = exc.value.body
    assert isinstance(error, dict)
    assert error["type"] == "invalid_request_error" and error["code"] == 400
    message = error["message"]
    assert f"{path}.file.file_id" in message
    assert "file.file_data" in message and "base64" in message
    assert all(value not in message for value in (_ID, _NAME, _B64, "fixture.invalid"))
    if delivery == "late-stream":
        assert type(exc.value) is openai.APIError
        assert observer.statuses == [200] and observer.heartbeat.is_set()
        wire = b"".join(bodies)
        assert wire.count(b"data: [DONE]") == 1
        assert wire.index(b'"error"') < wire.index(b"data: [DONE]")
    else:
        assert isinstance(exc.value, openai.BadRequestError)
        assert observer.statuses == [400] and not observer.heartbeat.is_set()


@pytest.mark.parametrize(
    "data",
    [
        42,
        _B64,
        "data:text/plain;base64,",
        "data:malformed," + _B64,
        " " + _DATA,
        "DATA:" + _DATA[5:],
    ],
)
async def test_unusable_file_data_does_not_hide_reference(reject_before_io, data):
    part = {"type": "file", "file": {**_REF["file"], "file_data": data}}
    async with _ASGIObserver().client() as client:
        with pytest.raises(openai.BadRequestError) as exc:
            await _turn(client, [_user([_TEXT, part])], False)
    assert "messages[0].content[1].file.file_id" in exc.value.message
    assert all(value not in exc.value.message for value in (_ID, _NAME, _B64))


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_id", [False, True])
async def test_inline_file_and_ignored_extras_keep_chat_wire(
    route, monkeypatch, stream, with_id
):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    monkeypatch.setattr(settings, "max_request_bytes", len(_BYTES))
    charges: list[int] = []
    account = MediaSession.account_bytes

    def charge(session, size):
        charges.append(size)
        account(session, size)

    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    source = _INLINE["file"].copy()
    if with_id:
        source["file_id"] = _ID
    messages = [
        {"role": "system", "content": [_TEXT, _REF]},
        {"role": "developer", "content": [_TEXT, _REF]},
        _user("Start."),
        {"role": "assistant", "content": [_TEXT, _REF]},
        _user(
            [
                _TEXT,
                File(type="file", file=source),
                {"type": "file", "file": {"file_id": "  "}},
                {"type": "file", "file": {"file_id": 42}},
                {"type": "file", "file_id": _ID, "file": {}},
                {"type": "text", "text": "End.", "file": {"file_id": _ID}},
            ]
        ),
    ]
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        reply = await _turn(client, messages, stream)
    assert reply.choices[0].message.content == "Noted." and charges == [len(_BYTES)]
    text = AistudioPart(text=_TEXT["text"]).to_wire()
    inline = AistudioPart(inline_data=("text/plain", _B64), inline_filename=_NAME)
    assert calls == [
        [
            [[AistudioPart(text="Start.").to_wire()], "user"],
            [[text], "model"],
            [[text, inline.to_wire(), AistudioPart(text="End.").to_wire()], "user"],
        ]
    ]
