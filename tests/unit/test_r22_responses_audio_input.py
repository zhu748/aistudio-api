"""Do not complete a Responses turn after discarding supplied user audio."""

import asyncio
import base64
import io
import wave
from typing import cast
from unittest.mock import Mock

import openai
import pytest
from openai.types.responses import (
    ResponseFailedEvent,
    ResponseInputAudioParam,
    ResponseInputFileParam,
    ResponseInputParam,
)
from test_compat_media_routes import (
    _client_options,
    _responses_turn,
    _serialized_contents,
)
from test_ordered_tool_routes import wire_source as wire_source
from test_r20_responses_file_refs import reject_before_io as reject_before_io
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api import routes_compat
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


def _silent_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as clip:
        clip.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        clip.writeframes(b"\0\0" * 8)
    return buf.getvalue()


_WAV = _silent_wav()
_DATA = base64.b64encode(_WAV).decode("ascii")
_AUDIO = ResponseInputAudioParam(
    type="input_audio", input_audio={"data": _DATA, "format": "wav"}
)
_FILE = ResponseInputFileParam(
    type="input_file", file_data="data:audio/wav;base64," + _DATA, filename="clip.wav"
)
_CAPTION = {"type": "input_text", "text": "Listen to the attached clip."}
_REMOTE = {"type": "input_image", "image_url": "https://fixture.invalid/image"}


@pytest.mark.parametrize(
    ("items", "path"),
    [
        ([{"role": "user", "content": [_AUDIO]}], "input[0].content[0]"),
        (
            [
                {"role": "user", "content": [_CAPTION, _AUDIO]},
                {"role": "assistant", "content": "Noted."},
                {"role": "user", "content": "What did you hear?"},
            ],
            "input[0].content[1]",
        ),
        (
            [
                {"role": "user", "content": [_FILE, _REMOTE]},
                {"role": "assistant", "content": "Noted."},
                {"type": "message", "role": "user", "content": [_CAPTION, _AUDIO]},
            ],
            "input[2].content[1]",
        ),
        (
            [
                {
                    "role": "user",
                    "content": [
                        _CAPTION,
                        {"type": "input_audio", "input_audio": {"data": "private??"}},
                    ],
                }
            ],
            "input[0].content[1]",
        ),
    ],
    ids=["audio-only", "audio-in-history", "after-earlier-media", "unvalidated-data"],
)
@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
async def test_rejects_supplied_user_audio_before_media_or_dispatch(
    reject_before_io, monkeypatch, items, path, delivery
):
    observer = _ASGIObserver()
    charge = Mock(side_effect=AssertionError("unexpected media charge"))
    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 5)
    if delivery == "committed-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.responses_to_gemini_request

        async def after_commit(request):
            await observer.heartbeat.wait()
            return await convert(request)

        monkeypatch.setattr(routes_compat, "responses_to_gemini_request", after_commit)

    # The SDK exports the audio shape but omits it from the typed message union.
    input_items = cast("ResponseInputParam", items)
    async with asyncio.timeout(5), observer.client() as client:
        if delivery == "committed-stream":
            async with client.responses.stream(
                model=MODEL, input=input_items
            ) as stream:
                events = [event async for event in stream]
            assert observer.statuses == [200]
            assert [e.type for e in events] == ["response.created", "response.failed"]
            created, failed = events
            assert created.type == "response.created" and isinstance(
                failed, ResponseFailedEvent
            )
            assert (
                failed.response.id == created.response.id and not failed.response.output
            )
            assert (
                failed.response.error is not None
                and failed.response.error.code == "invalid_prompt"
            )
            message = failed.response.error.message
        else:
            with pytest.raises(openai.BadRequestError) as exc:
                await _responses_turn(client, input_items, delivery == "early-stream")
            assert (
                observer.statuses == [400] and exc.value.type == "invalid_request_error"
            )
            message = exc.value.message
    assert path + ".input_audio" in message
    assert "input_file.file_data" in message and "inline base64 data URL" in message
    for private_value in (
        _DATA,
        "private??",
        _CAPTION["text"],
        "clip.wav",
        "fixture.invalid",
    ):
        assert private_value not in message
    charge.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
async def test_inline_audio_file_preserves_bytes_and_ignores_empty_placeholders(
    route, monkeypatch, stream
):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    charge = Mock(wraps=MediaSession.account_bytes)
    monkeypatch.setattr(MediaSession, "account_bytes", lambda self, n: charge(self, n))
    placeholders = [
        {"type": "input_audio"},
        {"type": "input_audio", "input_audio": _DATA},
        *(
            {"type": "input_audio", "input_audio": {"data": value, "format": "wav"}}
            for value in (None, 7, "", " \t")
        ),
    ]
    items = [
        {"role": "user", "content": "Earlier."},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Heard."}, _AUDIO],
        },
        {"role": "system", "content": [_AUDIO]},
        {"role": "developer", "content": [_AUDIO]},
        {
            "type": "message",
            "role": "user",
            "content": [
                _CAPTION,
                *placeholders,
                _FILE,
                {"type": "input_text", "text": "End."},
            ],
        },
    ]
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        reply = await _responses_turn(client, cast("ResponseInputParam", items), stream)
    assert reply.status == "completed" and reply.output_text == "Noted."
    assert calls == [
        [
            [[AistudioPart(text="Earlier.").to_wire()], "user"],
            [[AistudioPart(text="Heard.").to_wire()], "model"],
            [
                [
                    AistudioPart(text=_CAPTION["text"]).to_wire(),
                    AistudioPart(
                        inline_data=("audio/wav", _DATA), inline_filename="clip.wav"
                    ).to_wire(),
                    AistudioPart(text="End.").to_wire(),
                ],
                "user",
            ],
        ]
    ]
    charge.assert_called_once()
    assert charge.call_args.args[1] == len(_WAV)
