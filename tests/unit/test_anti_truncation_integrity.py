"""Incomplete synthetic arguments must retain the upstream failure status."""

from __future__ import annotations

import pytest

from aistudio_api.application.anti_truncation import (
    AntiTruncationContext,
    restore_anti_truncation_stream,
)


@pytest.mark.parametrize("finish", [10, 12, 13])
@pytest.mark.parametrize(
    "arguments",
    [
        '{"content":"partial',
        '{"content":"partial",',
        '{"content":"partial"} broken',
    ],
)
async def test_partial_synthetic_body_cannot_turn_failure_into_stop(finish, arguments):
    async def source():
        yield "tool_calls", [{"name": "router_emit", "arguments": arguments[:15]}]
        yield "tool_calls", [{"name": "router_emit", "arguments": arguments[15:]}]
        yield "finish_reason", finish
        yield "done", None

    context = AntiTruncationContext(tool_name="router_emit", active=True)
    events = [
        event async for event in restore_anti_truncation_stream(source(), context)
    ]
    assert ("finish_reason", finish) in events


@pytest.mark.parametrize("finish", [10, 12, 13])
@pytest.mark.parametrize("structured", [False, True])
async def test_complete_synthetic_body_can_normalize_tool_finish(finish, structured):
    async def source():
        call: dict[str, object] = {"name": "router_emit"}
        if structured:
            call["args"] = {"content": "complete"}
        else:
            call["arguments"] = '{"content":"complete"}'
        yield "tool_calls", [call]
        yield "finish_reason", finish
        yield "done", None

    context = AntiTruncationContext(tool_name="router_emit", active=True)
    events = [
        event async for event in restore_anti_truncation_stream(source(), context)
    ]
    assert ("body", "complete") in events
    assert ("finish_reason", 1) in events


@pytest.mark.parametrize("finish", [10, 12, 13])
@pytest.mark.parametrize("first_complete", [False, True])
async def test_every_synthetic_call_must_be_complete_before_normalizing_finish(
    finish, first_complete
):
    async def source():
        yield (
            "tool_calls",
            [
                {
                    "id": "first",
                    "name": "router_emit",
                    "arguments": '{"content":"first"}'
                    if first_complete
                    else '{"content":"partial',
                }
            ],
        )
        yield (
            "tool_calls",
            [
                {
                    "id": "second",
                    "name": "router_emit",
                    "args": {"content": "second"},
                }
            ],
        )
        yield "finish_reason", finish
        yield "done", None

    context = AntiTruncationContext(tool_name="router_emit", active=True)
    events = [
        event async for event in restore_anti_truncation_stream(source(), context)
    ]
    assert ("finish_reason", 1 if first_complete else finish) in events


async def test_large_sharded_synthetic_body_and_final_snapshot_do_not_duplicate():
    text = "中文🙂" * 10000

    async def source():
        yield "tool_calls", [{"name": "router_emit", "arguments": '{"content":"'}]
        for offset in range(0, len(text), 97):
            yield (
                "tool_calls",
                [{"name": "router_emit", "arguments": text[offset : offset + 97]}],
            )
        yield "tool_calls", [{"name": "router_emit", "arguments": '"}'}]
        yield "tool_calls", [{"name": "router_emit", "args": {"content": text}}]
        yield "finish_reason", 10
        yield "done", None

    context = AntiTruncationContext(tool_name="router_emit", active=True)
    bodies = []
    async for tag, value in restore_anti_truncation_stream(source(), context):
        if tag == "body":
            bodies.append(value)
    assert "".join(bodies) == text


@pytest.mark.parametrize("content", [None, 42, {"text": "invalid"}])
async def test_invalid_snapshot_cannot_confirm_partial_synthetic_arguments(content):
    async def source():
        yield (
            "tool_calls",
            [{"name": "router_emit", "arguments": '{"content":"partial'}],
        )
        yield "tool_calls", [{"name": "router_emit", "args": {"content": content}}]
        yield "finish_reason", 10
        yield "done", None

    context = AntiTruncationContext(tool_name="router_emit", active=True)
    events = [
        event async for event in restore_anti_truncation_stream(source(), context)
    ]
    assert ("finish_reason", 10) in events
