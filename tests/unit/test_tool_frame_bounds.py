"""Tool argument limits and identity apply equally to structured and JSON inputs."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from aistudio_api.application import stream_utils


async def collect(calls: list[dict[str, object]]) -> list[dict[str, Any]]:
    async def source() -> AsyncGenerator[dict[str, object], None]:
        yield {
            "candidates": [
                {
                    "content": {"parts": [{"functionCall": call} for call in calls]},
                    "finishReason": "STOP",
                }
            ]
        }

    return [
        cast(dict[str, Any], frame)
        async for frame in stream_utils.coalesce_tool_frames(source())
    ]


async def test_complete_idless_json_calls_remain_independent():
    frames = await collect(
        [
            {"name": "weather", "args": '{"city":"Paris"}'},
            {"name": "weather", "args": '{"city":"Tokyo"}'},
        ]
    )
    calls = [
        part["functionCall"]
        for frame in frames
        for candidate in frame.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    ]
    assert [call["args"] for call in calls] == [{"city": "Paris"}, {"city": "Tokyo"}]
    assert len({call["id"] for call in calls}) == 2


@pytest.mark.parametrize("structured", [False, True])
async def test_tool_arguments_are_bounded_in_utf8_bytes(monkeypatch, structured):
    monkeypatch.setattr(stream_utils, "_MAX_TOOL_ARGUMENT_BYTES", 100, raising=False)
    args = {"text": "中" * 40} if structured else '{"text":"' + "中" * 40 + '"}'
    with pytest.raises(ValueError, match="limit"):
        await collect([{"name": "write", "args": args}])


async def test_total_response_tool_arguments_are_bounded(monkeypatch):
    monkeypatch.setattr(stream_utils, "_MAX_TOOL_RESPONSE_BYTES", 100, raising=False)
    with pytest.raises(ValueError, match="limit"):
        await collect(
            [
                {"id": str(index), "name": "work", "args": {"text": "x" * 40}}
                for index in range(3)
            ]
        )
