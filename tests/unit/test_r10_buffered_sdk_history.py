"""Buffered native SSE exposes partial output without adding it to SDK history."""

from __future__ import annotations

import pytest
import test_anti_truncation_variants as variants
import test_ordered_tool_routes as wire
import test_signed_part_roundtrip as signed
from google import genai
from google.genai import types

from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

route = signed.route
variant_config = variants.variant_config
_BASE = "gemini-3.7-flash"
_MODEL = f"{_BASE}-at"
_URL = f"/v1beta/models/{_MODEL}"
_NODE = {"x-goog-api-client": "google-genai-sdk/3.6.1 gl-node/v24.22.3"}
_WEB = {"x-goog-api-client": "google-genai-sdk/3.6.1 gl-node/web"}
_RID = "buffered-response-id"
_SIGNATURE = signed.signature("second part")
_CASES = [
    pytest.param(1, True, "STOP", id="complete"),
    pytest.param(2, True, "MAX_TOKENS", id="partial"),
    pytest.param(1, False, "STOP", id="diagnostic"),
]


def _set_reply(route, finish, has_output):
    parts = (
        [
            AistudioPart(text="Tall "),
            AistudioPart(text="ships.", thought_signature=_SIGNATURE),
        ]
        if has_output
        else []
    )
    encoded = [part.to_wire() for part in parts]
    route.wire.raw = wire.wire(
        [*wire.chunk(encoded, finish), None, [8, 3, 11], None, None, None, None, _RID]
    )
    return encoded


@pytest.mark.parametrize(("finish", "has_output", "reason"), _CASES)
@pytest.mark.parametrize("headers", [_NODE, _WEB], ids=["node", "web"])
async def test_buffered_native_sdk_frames_keep_raw_parts_and_terminal_metadata(
    route, variant_config, finish, has_output, reason, headers
):
    _set_reply(route, finish, has_output)
    _, body = wire.input_request("native", True)
    stream_url = f"{_URL}:streamGenerateContent?alt=sse"
    raw = await route.raw.post(stream_url, json=body)
    sdk = await route.raw.post(stream_url, json=body, headers=headers)
    nonstream = await route.raw.post(
        f"{_URL}:generateContent", json=body, headers=headers
    )
    assert raw.status_code == sdk.status_code == nonstream.status_code == 200

    # Raw buffered SSE and nonstream JSON still carry the full original response.
    (original,) = wire.sse_events(raw.text)
    assert original == nonstream.json()
    assert original["modelVersion"] == f"models/{_BASE}"
    assert original["responseId"] == _RID
    assert original["usageMetadata"]["totalTokenCount"] == 11
    candidate = original["candidates"][0]
    assert candidate["finishReason"] == reason
    assert candidate["index"] == 0
    content = candidate["content"]
    parts = content["parts"]
    if has_output:
        assert parts == [
            {"text": "Tall "},
            {"text": "ships.", "thoughtSignature": _SIGNATURE},
        ]
    else:
        assert len(parts) == 1 and "空回诊断" in parts[0]["text"]

    events = wire.sse_events(sdk.text)
    if has_output and reason == "STOP":
        assert events == [original]
    else:
        display, terminal = events
        assert wire.native_parts([display]) == parts
        assert "finishReason" not in display["candidates"][0]
        assert "usageMetadata" not in display
        # Only Parts move. Every other field survives unchanged on the sole terminal.
        content["parts"] = []
        assert terminal == original


@pytest.mark.parametrize(("finish", "has_output", "reason"), _CASES)
async def test_python_chat_replays_only_completed_buffered_turn(
    route, variant_config, finish, has_output, reason
):
    expected = _set_reply(route, finish, has_output)
    client = genai.Client(
        api_key="fixture",
        http_options=types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            httpx_async_client=route.raw,
        ),
    )
    async with client.aio as sdk:
        chat = sdk.chats.create(model=_MODEL)
        frames = [
            frame
            async for frame in await chat.send_message_stream("Tell me about ships.")
        ]
        assert frames[-1].candidates
        assert frames[-1].candidates[0].finish_reason == types.FinishReason(reason)
        parts = [part for frame in frames for part in signed._native_parts(frame)]
        if has_output:
            assert [part.text for part in parts] == ["Tall ", "ships."]
        else:
            assert len(parts) == 1 and "空回诊断" in (parts[0].text or "")
        replayable = has_output and reason == "STOP"
        assert [turn.role for turn in chat.get_history(curated=True)] == (
            ["user", "model"] if replayable else []
        )
        route.wire.reply(AistudioPart(text="Next answer."))
        await chat.send_message("Continue.")
    assert route.wire.model_wire == (expected if replayable else [])
