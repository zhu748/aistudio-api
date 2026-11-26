"""Verify media contracts through official clients and the actual wire encoder."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import anthropic
import httpx2
import openai
import pytest
from openai.types.responses import Response, ResponseInputParam
from test_signed_part_roundtrip import (
    HEADERS,
    MODEL,
    _claude_sdk_turn,
    route as route,
    signature,
    thought,
)

from aistudio_api.api.dependencies import get_client
from aistudio_api.application import compat_shared
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


def _png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0" + bytes(rgb)))
        + chunk(b"IEND", b"")
    )


_PNG = {
    key: base64.b64encode(_png(rgb)).decode("ascii")
    for key, rgb in (("A", (7, 71, 1)), ("B", (11, 2, 220)))
}
_NOTE = {
    key: base64.b64encode(f"Palette note {key}".encode()).decode("ascii")
    for key in _PNG
}
_PROMPT = {"role": "user", "content": "Read both swatch notes."}


def _serialized_contents(route: SimpleNamespace) -> list[list[Any]]:
    calls: list[list[Any]] = []
    codec = AistudioWireCodec()

    async def prepare(*, contents, **kwargs):
        # Use the production codec; the in-memory prepared request never
        # enters a real account or network transport.
        body = codec.encode(codec.build_request(model=MODEL, contents=contents))
        calls.append(json.loads(body)[codec.CONTENTS_INDEX])
        return PreparedRequest(url="http://fixture.invalid", headers={}, body=body)

    route.app.dependency_overrides[get_client]().prepare_request = prepare
    return calls


def _client_options(route: SimpleNamespace) -> dict[str, Any]:
    return {
        "api_key": "fixture",
        "http_client": httpx2.AsyncClient(transport=httpx2.ASGITransport(route.app)),
        "default_headers": HEADERS,
        "_strict_response_validation": True,
        "max_retries": 0,
    }


def _attachments(call_id: str) -> list[dict[str, str]]:
    return [
        {
            "type": "input_image",
            "detail": "auto",
            "image_url": "data:image/png;base64," + _PNG[call_id],
        },
        {
            "type": "input_file",
            "file_data": "data:text/plain;base64," + _NOTE[call_id],
            "filename": call_id + ".txt",
        },
    ]


def _tool_call(call_id: str) -> dict[str, str]:
    return {
        "type": "function_call",
        "name": "read_swatch",
        "call_id": call_id,
        "arguments": "{}",
    }


def _tool_result(call_id: str, output: list[dict[str, str]]) -> dict[str, object]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


async def _responses_turn(
    client: openai.AsyncOpenAI, input_items: str | ResponseInputParam, stream: bool
) -> Response:
    if stream:
        async with client.responses.stream(model=MODEL, input=input_items) as response:
            return await response.get_final_response()
    return await client.responses.create(model=MODEL, input=input_items)


@pytest.mark.parametrize("stream", [False, True])
async def test_responses_sdk_keeps_inline_attachments_with_reversed_result_owners(
    route, stream
):
    calls = _serialized_contents(route)
    route.wire.reply(
        *(
            AistudioPart(function_call=("read_swatch", {"sample": key}, key))
            for key in "AB"
        )
    )
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as client:
        turn = await _responses_turn(client, _PROMPT["content"], stream)
        ids = [item.call_id for item in turn.output if item.type == "function_call"]
        assert ids == ["A", "B"]
        history = [
            _PROMPT,
            *(item.model_dump(exclude_none=True) for item in turn.output),
            *(
                _tool_result(
                    key,
                    [
                        {"type": "input_text", "text": "Palette " + key},
                        *_attachments(key),
                    ],
                )
                for key in reversed(ids)
            ),
        ]
        route.wire.reply(AistudioPart(text="Next."))
        reply = await _responses_turn(
            client, cast("ResponseInputParam", history), stream
        )
    assert reply.output_text == "Next."
    assert calls[-1][-1] == [
        [
            part.to_wire()
            for key in "BA"
            for part in (
                AistudioPart(
                    function_response=("read_swatch", {"result": "Palette " + key}, key)
                ),
                AistudioPart(inline_data=("image/png", _PNG[key])),
                AistudioPart(
                    inline_data=("text/plain", _NOTE[key]), inline_filename=key + ".txt"
                ),
            )
        ],
        "user",
    ]


@pytest.mark.parametrize(
    ("attachment", "error"),
    [
        ({"type": "input_file", "file_data": "data:text/plain;base64,a==?"}, "base64"),
        ({"type": "input_file", "file_data": "data:text/plain;base64,YQ="}, "base64"),
        ({"type": "input_file", "file_data": "data:text/plain;base64,"}, "data URL"),
        (
            {"type": "input_image", "image_url": "data:text/plain;base64,YQ=="},
            "media type",
        ),
    ],
)
async def test_responses_rejects_invalid_claimed_inline_tool_attachments(
    route, attachment, error
):
    calls = _serialized_contents(route)
    request = {
        "model": MODEL,
        "input": [_PROMPT, _tool_call("A"), _tool_result("A", [attachment])],
    }
    response = await route.raw.post("/v1/responses", json=request)
    assert response.status_code == 400 and not calls
    assert "function_call_output.output[0]" in response.text
    assert error in response.text


@pytest.mark.parametrize("limit", ["single", "aggregate"])
async def test_responses_limits_inline_tool_attachments_before_preparing(
    route, monkeypatch, limit
):
    calls = _serialized_contents(route)
    size = len(base64.b64decode(_PNG["A"]))
    history: list[object] = [_PROMPT, _tool_call("A"), _tool_call("B")]
    if limit == "single":
        monkeypatch.setattr(compat_shared, "_MAX_MEDIA_BYTES", size - 1)
    else:
        # Each image fits alone, but separate outputs share one request budget.
        monkeypatch.setattr(settings, "max_request_bytes", size + 1)
        history.append(_tool_result("B", _attachments("B")[:1]))
    history.append(_tool_result("A", _attachments("A")[:1]))
    response = await route.raw.post(
        "/v1/responses", json={"model": MODEL, "input": history}
    )
    assert response.status_code == 400 and not calls
    assert "limit" in response.text


async def test_responses_omits_unavailable_tool_attachments_without_fetching(
    route, monkeypatch
):
    calls = _serialized_contents(route)
    get_media_client = AsyncMock(side_effect=AssertionError("unexpected download"))
    monkeypatch.setattr(MediaSession, "get_client", get_media_client)
    attachments = [
        {"type": "input_text", "text": "Palette A"},
        {"type": "input_image", "image_url": "https://fixture.invalid/palette.png"},
        {"type": "input_file", "file_id": "fixture-file"},
    ]
    route.wire.reply(AistudioPart(text="Next."))
    response = await route.raw.post(
        "/v1/responses",
        json={
            "model": MODEL,
            "input": [_PROMPT, _tool_call("A"), _tool_result("A", attachments)],
        },
    )
    assert response.status_code == 200
    get_media_client.assert_not_awaited()
    assert calls[-1][-1] == [
        [
            AistudioPart(
                function_response=(
                    "read_swatch",
                    {"result": "Palette A\n[image omitted]\n[file omitted]"},
                    "A",
                )
            ).to_wire()
        ],
        "user",
    ]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
async def test_strict_claude_image_notice_preserves_order_and_does_not_replay_bytes(
    route, stream, mixed
):
    calls = _serialized_contents(route)
    image_signature = signature("image owner")
    image = AistudioPart(
        inline_data=("image/png", _PNG["A"]), thought_signature=image_signature
    )
    owned_thought = thought("Thought.", "different thought owner")
    if mixed:
        route.wire.reply(
            owned_thought,
            AistudioPart(text="Before <EN"),
            image,
            AistudioPart(text="D> After."),
            AistudioPart(function_call=("read_swatch", {}, "A")),
        )
    else:
        route.wire.reply(image)
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as client:
        message = await _claude_sdk_turn(
            client,
            {
                "model": MODEL,
                "messages": [_PROMPT],
                "max_tokens": 128,
                "stop_sequences": ["<END>", "unavailable"],
            },
            stream,
        )
        parts = [block.text for block in message.content if block.type == "text"]
        assert message.stop_reason == ("tool_use" if mixed else "end_turn")
        assert len(parts) == (3 if mixed else 1)
        notice = parts[1] if mixed else parts[0]
        assert "Image output is unavailable" in notice and len(notice) < 256
        assert "base64" not in notice and _PNG["A"] not in notice
        if mixed:
            assert parts[0] == "Before <EN" and parts[2] == "D> After."
            assert message.content[-1].type == "tool_use"
        blocks = [block.model_dump(exclude_none=True) for block in message.content]
        next_content = (
            [{"type": "tool_result", "tool_use_id": "A", "content": "Done."}]
            if mixed
            else [{"type": "text", "text": "Continue."}]
        )
        route.wire.reply(AistudioPart(text="Next."))
        await client.messages.create(
            model=MODEL,
            messages=[
                cast(Any, _PROMPT),
                {"role": "assistant", "content": cast(Any, blocks)},
                {"role": "user", "content": cast(Any, next_content)},
            ],
            max_tokens=128,
        )
    model_wire = next(parts for parts, role in calls[-1] if role == "model")
    wire_text = json.dumps(model_wire)
    assert image_signature not in wire_text and _PNG["A"] not in wire_text
    if mixed:
        assert model_wire[0] == owned_thought.to_wire()
