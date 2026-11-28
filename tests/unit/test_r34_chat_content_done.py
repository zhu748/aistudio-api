"""A Chat content.done event covers all selected visible text, not a tool-delimited prefix."""

import json
from contextlib import aclosing
from typing import Any

import httpx2
import openai
import pytest
import test_ordered_tool_routes as wire
from pydantic import BaseModel

from aistudio_api.application import compat_openai
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart
from aistudio_api.infrastructure.signature_trace import ThoughtSignatureTrace

chat_wire = wire.wire_source
_SIGNATURES = {"A": "c2lnLUE=", "B": "c2lnLUI="}
_COLORS = {"A": "blue", "B": "green"}


class Color(BaseModel):
    color: str


@pytest.mark.parametrize("typed", [False, True], ids=["plain", "typed"])
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("prefix suffix", "prefix suffix"),
        ("prefix suffix A", "prefix suffix A"),
        ("A B prefix suffix", "A B prefix suffix"),
        ("prefix A suffix B", "prefix suffix A B"),
        # First call precedes *all* content. Only the later call can close a prefix.
        ("A prefix B suffix", "A prefix suffix B"),
    ],
)
async def test_sdk_content_done_and_parsed_snapshot(chat_wire, typed, source, expected):
    prefix, suffix = ('{"color":', '"blue"}') if typed else ("I found ", "blue.")
    parts = {
        "prefix": wire.text(prefix),
        "suffix": wire.text(suffix),
        **{
            key: wire.call("label", {"color": color}, key, signature=_SIGNATURES[key])
            for key, color in _COLORS.items()
        },
    }
    chat_wire.raw = wire.wire(
        *(wire.chunk([parts[key]]) for key in source.split()),
        [*wire.chunk([], 1), None, [12, 7, 19]],
    )
    fields: dict[str, Any] = {
        "model": wire.MODEL,
        "messages": [{"role": "user", "content": "Name a color."}],
        "tools": [openai.pydantic_function_tool(Color, name="label")],
    }
    if typed:
        fields["response_format"] = Color
    keys = [key for key in source.split() if key in _COLORS]
    async with openai.AsyncOpenAI(
        api_key="fixture",
        base_url="http://fixture.invalid/v1",
        default_headers={"x-anti-truncation": "0"},
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(wire.app)),
        max_retries=0,
        _strict_response_validation=True,
    ) as sdk:
        unary = await sdk.chat.completions.parse(**fields)
        wire_order, content_done, calls_done = [], [], []
        async with sdk.chat.completions.stream(
            **fields, stream_options={"include_usage": typed}
        ) as stream:
            async for event in stream:
                if event.type == "content.done":
                    content_done.append((event.content, event.parsed))
                elif event.type == "tool_calls.function.arguments.done":
                    calls_done.append((event.index, event.name, event.parsed_arguments))
                elif event.type == "chunk":
                    if not event.chunk.choices:
                        wire_order.append("usage")
                        assert (
                            event.chunk.usage and event.chunk.usage.total_tokens == 19
                        )
                        continue
                    choice = event.chunk.choices[0]
                    if choice.delta.content:
                        wire_order.append(
                            {prefix: "prefix", suffix: "suffix"}[choice.delta.content]
                        )
                    if choice.delta.tool_calls:
                        assert choice.finish_reason is None
                        for call in choice.delta.tool_calls:
                            key = call.id
                            assert key is not None and call.index == keys.index(key)
                            assert call.model_dump()["extra_content"] == {
                                "google": {"thought_signature": _SIGNATURES[key]}
                            }
                            assert call.function and call.function.arguments
                            assert json.loads(call.function.arguments) == {
                                "color": _COLORS[key]
                            }
                            wire_order.append(key)
                    if choice.finish_reason is not None:
                        wire_order.append("finish")
            final = await stream.get_final_completion()

    parsed = Color(color="blue") if typed else None
    assert content_done == [(prefix + suffix, parsed)]
    assert wire_order == [*expected.split(), "finish", *(["usage"] if typed else [])]
    assert calls_done == [
        (i, "label", Color(color=_COLORS[key])) for i, key in enumerate(keys)
    ]
    choice = final.choices[0]
    assert choice.message.content == unary.choices[0].message.content == prefix + suffix
    assert choice.message.parsed == unary.choices[0].message.parsed == parsed
    assert (
        choice.finish_reason
        == unary.choices[0].finish_reason
        == ("tool_calls" if keys else "stop")
    )


@pytest.mark.parametrize(
    ("source", "expected", "reason"),
    [
        # Neither reasoning nor a function result is Chat delta.content.
        ("thought result A suffix", ["think", "A", "tail"], "tool_calls"),
        ("prefix A thought stop B", ["lead", "A", "think"], "stop"),
        # The later stop discards B, not the already selected A.
        ("prefix A thought mixed B", ["lead", "think", "kept", "A"], "stop"),
        # A partial stop may become real content at a call boundary or at EOF.
        ("held A suffix", ["<EN", "tail", "A"], "tool_calls"),
        ("prefix A held", ["lead", "<EN", "A"], "tool_calls"),
        (
            "prefix A code image",
            ["lead", "FOUR", "\n\n![image](data:image/png;base64,YQ==)", "A"],
            "tool_calls",
        ),
    ],
)
async def test_order_uses_only_selected_visible_content(
    chat_wire, source, expected, reason
):
    parts = {
        "A": wire.call("label", {"color": "blue"}, "A", signature=_SIGNATURES["A"]),
        "B": wire.call("label", {}, "B", signature=_SIGNATURES["B"]),
        "thought": wire.text("think", thought=True),
        "result": [None, None, None, None, ["label", {"color": "red"}, "other"]],
        "prefix": wire.text("lead"),
        "suffix": wire.text("tail"),
        "held": wire.text("<EN"),
        "stop": wire.text("<END>omitted"),
        "mixed": wire.text("kept<END>omitted"),
        "code": [None] * 9 + [[1, "FOUR"]],
        "image": AistudioPart(inline_data=("image/png", "YQ==")).to_wire(),
    }
    chat_wire.raw = wire.wire(wire.chunk([parts[key] for key in source.split()], 1))
    events = await wire.request("chat", True, stop="<END>")
    deltas = [event["choices"][0]["delta"] for event in events]
    assert [
        delta["tool_calls"][0]["id"]
        if "tool_calls" in delta
        else delta.get("content") or delta.get("reasoning_content")
        for delta in deltas
        if "role" not in delta and delta
    ] == expected
    assert events[-1]["choices"][0]["finish_reason"] == reason


@pytest.mark.parametrize("ending", ["tool_last", "mixed", "close_before_tool"])
async def test_call_signature_preserves_trace_budget_and_publication(
    monkeypatch, ending
):
    trace = ThoughtSignatureTrace()
    monkeypatch.setattr(compat_openai, "get_signature_trace", lambda: trace)

    async def frames():
        parts = [
            {"text": "prefix"},
            {
                "functionCall": {"id": "A", "name": "label", "args": {}},
                "thoughtSignature": _SIGNATURES["A"],
            },
            # Later hidden carriers must not take all 32 audit slots before A.
            *[{"text": "", "thoughtSignature": "ZW1wdHk="} for _ in range(32)],
            {"text": "later thought", "thought": True, "thoughtSignature": "dGhpbms="},
        ]
        if ending != "tool_last":
            parts.append({"text": "suffix"})
        yield {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}

    async with aclosing(
        compat_openai.openai_stream_adapter(
            frames(), requested_model=wire.MODEL, include_usage=False
        )
    ) as stream:
        async for frame in stream:
            events = wire.sse_events(frame)
            if not events:
                continue
            choice = events[0]["choices"][0]
            if choice["delta"].get("content") == "suffix":
                assert trace.client_out_total == 0
                if ending == "close_before_tool":
                    break
            if choice["delta"].get("tool_calls"):
                assert trace.client_out_total == 1
                assert trace.client_out_items[0]["call_id"] == "A"
            if choice["finish_reason"]:
                assert "extra_content" not in choice["delta"]
    if ending == "close_before_tool":
        assert not trace.client_out_total and not trace.client_out_items
    else:
        assert (trace.client_out_total, trace.client_out_dropped) == (1, 33)
        assert trace.client_out_items[0]["call_id"] == "A"
