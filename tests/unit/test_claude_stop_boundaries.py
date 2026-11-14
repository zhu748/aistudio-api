"""Claude stop handling must preserve block order and suppress later actions."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.compat_service import (
    build_claude_message,
    claude_stream_adapter,
)


async def render(parts: list[dict[str, object]], streaming: bool, stop: str):
    frame = {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
    if not streaming:
        result = cast(
            dict[str, Any],
            build_claude_message(
                GeminiGenerateContentResponse.model_validate(frame), "test", [stop]
            ),
        )
        return result["content"], result["stop_reason"]

    async def source():
        yield frame

    blocks: list[dict[str, Any]] = []
    reason = None
    async for event in claude_stream_adapter(
        source(), requested_model="test", stop_sequences=[stop]
    ):
        for line in event.split("\n"):
            if not line.startswith("data: "):
                continue
            value = json.loads(line[6:])
            if value["type"] == "content_block_start":
                blocks.append(value["content_block"])
            elif value["type"] == "content_block_delta":
                delta = value["delta"]
                if delta["type"] == "text_delta":
                    blocks[-1]["text"] += delta["text"]
            elif value["type"] == "message_delta":
                reason = value["delta"]["stop_reason"]
    return blocks, reason


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        {"text": "reason", "thought": True},
        {"functionCall": {"id": "c", "name": "work", "args": {}}},
        {"inlineData": {"mimeType": "image/png", "data": "aA=="}},
    ],
)
async def test_pending_stop_prefix_stays_before_non_text_block(streaming, boundary):
    blocks, reason = await render([{"text": "hello <EN"}, boundary], streaming, "<END>")
    assert blocks[0] == {"type": "text", "text": "hello <EN"}
    assert blocks[1]["type"] != "text"
    assert reason in ("tool_use", "end_turn")


@pytest.mark.parametrize("streaming", [False, True])
async def test_stop_match_discards_following_tools_thinking_and_images(streaming):
    blocks, reason = await render(
        [
            {"text": "before <END> hidden"},
            {"functionCall": {"id": "c", "name": "write", "args": {}}},
            {"text": "hidden reasoning", "thought": True},
            {"inlineData": {"mimeType": "image/png", "data": "aA=="}},
            {"text": "hidden tail"},
        ],
        streaming,
        "<END>",
    )
    assert blocks == [{"type": "text", "text": "before "}]
    assert reason == "stop_sequence"


@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_before_stop_is_preserved(streaming):
    blocks, reason = await render(
        [
            {"functionCall": {"id": "c", "name": "work", "args": {}}},
            {"text": "before <END> hidden"},
        ],
        streaming,
        "<END>",
    )
    assert [block["type"] for block in blocks] == ["tool_use", "text"]
    assert blocks[0]["id"] == "c" and blocks[1]["text"] == "before "
    assert reason == "stop_sequence"
