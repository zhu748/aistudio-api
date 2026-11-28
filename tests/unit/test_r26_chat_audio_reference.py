"""Chat cannot replay an opaque assistant audio ID. Ordinary user audio is inline."""

import asyncio
import json
from unittest.mock import Mock

import openai
import pytest
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionAudio,
    ChatCompletionContentPartInputAudioParam,
    ChatCompletionMessage,
)
from test_compat_media_routes import _client_options, _serialized_contents
from test_ordered_tool_routes import wire_source as wire_source
from test_r21_chat_file_ref import (
    no_external_client as no_external_client,
    reject_before_io as reject_before_io,
)
from test_r22_responses_audio_input import _DATA, _WAV
from test_r25_openai_refusal_history import _wire
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api import routes_compat
from aistudio_api.application import compat_openai
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.domain import tool_signatures
from aistudio_api.infrastructure.gateway import model_turns
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_ID = "audio_private_fixture"
_PREVIOUS = ChatCompletionAudio(
    id=_ID, data=_DATA, transcript="Fixture private transcript.", expires_at=2345678901
)
_REF = ChatCompletionAssistantMessageParam(role="assistant", audio={"id": _PREVIOUS.id})
_CALL = {
    "type": "function",
    "id": "call-note",
    "function": {"name": "note", "arguments": "{}"},
}
_RESULT = {"role": "tool", "tool_call_id": "call-note", "content": "Read."}
_WAV_BLOCK = ChatCompletionContentPartInputAudioParam(
    type="input_audio", input_audio={"data": _DATA, "format": "wav"}
)
_URL = "https://fixture.invalid/private-audio"


async def _turn(client, stream, **params):
    if stream:
        async with client.chat.completions.stream(model=MODEL, **params) as events:
            return await events.get_final_completion()
    return await client.chat.completions.create(model=MODEL, **params)


@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
@pytest.mark.parametrize(
    "prior_turn",
    [
        [_REF],
        [ChatCompletionMessage(role="assistant", content=None, audio=_PREVIOUS)],
        [
            {
                "role": "assistant",
                "content": "Private visible text.",
                "refusal": "Private refusal.",
                "audio": _PREVIOUS.model_dump(),
                "tool_calls": [{**_CALL, "id": "call-recheck"}],
            },
            {**_RESULT, "tool_call_id": "call-recheck"},
        ],
        [{"role": " AsSiStAnT ", "audio": {"id": " " + _URL}}],
    ],
    ids=["reference-only", "imported-sdk-message", "mixed-turn", "normalized-role"],
)
async def test_audio_history_rejects_before_media_replay_or_dispatch(
    reject_before_io, monkeypatch, prior_turn, delivery, caplog
):
    replay = Mock(side_effect=AssertionError("unexpected history replay"))
    for target, name in (
        (compat_openai, "_openai_history_call"),
        (compat_openai, "result_part"),
        (tool_signatures, "get_signature_cache"),
        (model_turns, "get_signature_cache"),
    ):
        monkeypatch.setattr(target, name, replay)
    # Earlier replay, local bytes and a remote URL must not be touched before
    # rejecting the audio-only or partially representable assistant message.
    messages = [
        {"role": "user", "content": "Read the note."},
        {"role": "assistant", "tool_calls": [_CALL]},
        _RESULT,
        {
            "role": "user",
            "content": [_WAV_BLOCK, {"type": "image_url", "image_url": {"url": _URL}}],
        },
        *prior_turn,
        {"role": "user", "content": "How did it sound?"},
    ]
    bodies = []
    observer = _ASGIObserver(lambda msg: bodies.append(msg.get("body", b"")))
    if delivery == "committed-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.openai_to_gemini_request

        async def after_heartbeat(req):
            await observer.heartbeat.wait()
            return await convert(req)

        monkeypatch.setattr(routes_compat, "openai_to_gemini_request", after_heartbeat)
    async with asyncio.timeout(5), observer.client() as client:
        with pytest.raises(openai.APIError) as exc:
            await _turn(client, delivery != "unary", messages=messages)
    error = exc.value.body
    assert isinstance(error, dict)
    assert error["type"] == "invalid_request_error" and error["code"] == 400
    message = error["message"]
    assert message.startswith("messages[4].audio.id is not supported")
    assert "transcript as assistant text" in message and "user input_audio" in message
    logs = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("aistudio.")
    )
    for private in (
        _ID,
        _URL,
        _DATA,
        _PREVIOUS.transcript,
        "Private visible text.",
        "Private refusal.",
        "call-note",
    ):
        assert private not in message and private not in logs
    replay.assert_not_called()
    if delivery == "committed-stream":
        assert type(exc.value) is openai.APIError
        assert observer.statuses == [200] and observer.heartbeat.is_set()
        data = [s[6:] for s in b"".join(bodies).splitlines() if s.startswith(b"data: ")]
        assert len(data) == 2 and data[1] == b"[DONE]"
        assert json.loads(data[0]) == {"error": error}
    else:
        assert isinstance(exc.value, openai.BadRequestError)
        assert observer.statuses == [400] and not observer.heartbeat.is_set()


_FILE = {"role": "user", "content": [{"type": "file", "file": {"file_id": "opaque"}}]}
_CUSTOM = {"role": "assistant", "tool_calls": [{"type": "custom"}]}


@pytest.mark.parametrize(
    ("messages", "kwargs", "first"),
    [
        ([_REF], {"n": 2}, "Only n=1"),
        ([_FILE, _REF], {}, "messages[0].content[0].file.file_id"),
        ([_CUSTOM, _REF], {}, "messages[0].tool_calls[0].type (custom)"),
        ([{**_REF, **_CUSTOM}], {}, "messages[0].tool_calls[0].type (custom)"),
        ([_REF, _FILE], {}, "messages[0].audio.id"),
        ([_REF, _CUSTOM], {}, "messages[0].audio.id"),
    ],
)
async def test_audio_reference_follows_existing_preflight_order(
    reject_before_io, messages, kwargs, first
):
    async with _ASGIObserver().client() as client:
        with pytest.raises(openai.BadRequestError) as exc:
            await _turn(client, False, messages=messages, **kwargs)
    body = exc.value.body
    assert isinstance(body, dict) and body["message"].startswith(first)


@pytest.mark.parametrize("stream", [False, True])
async def test_ignored_audio_fields_keep_transcript_and_user_wav_on_wire(
    route, monkeypatch, stream
):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    charge = Mock(wraps=MediaSession.account_bytes)
    monkeypatch.setattr(MediaSession, "account_bytes", lambda self, n: charge(self, n))
    placeholders = [
        None,
        _URL,
        [],
        {},
        {"id": None},
        {"id": 42},
        {"id": True},
        {"id": {}},
        {"id": ""},
        {"id": " \t\u2003"},
        {"audio_id": _ID},
        {"data": _DATA, "transcript": "Ignore."},
    ]
    texts = [f"Transcript {n}." for n in range(len(placeholders))]
    messages = [
        *(
            {"role": r, "content": "Begin.", "audio": {"id": _ID}}
            for r in ("system", "developer", "user")
        ),
        *(
            {"role": "assistant", "content": t, "audio": v}
            for t, v in zip(texts, placeholders, strict=True)
        ),
        {"role": "assistant", "tool_calls": [_CALL]},
        {**_RESULT, "audio": {"id": _ID}},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "First."},
                _WAV_BLOCK,
                {"type": "text", "text": "Last."},
            ],
        },
    ]
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        reply = await _turn(
            client,
            stream,
            messages=messages,
            audio={"format": "wav", "voice": "alloy"},
            modalities=["text", "audio"],
        )
    assert reply.choices[0].message.content == "Noted."
    assert reply.choices[0].message.audio is None
    assert calls == [
        [
            _wire("user", "Begin."),
            _wire(
                "model", *texts, AistudioPart(function_call=("note", {}, "call-note"))
            ),
            _wire(
                "user",
                AistudioPart(
                    function_response=("note", {"result": "Read."}, "call-note")
                ),
                "First.",
                AistudioPart(inline_data=("audio/wav", _DATA)),
                "Last.",
            ),
        ]
    ]
    charge.assert_called_once()
    assert charge.call_args.args[1] == len(_WAV)
