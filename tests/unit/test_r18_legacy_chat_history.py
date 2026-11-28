"""Deprecated Chat calls are historical text, never new tool requests."""

import pytest
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionFunctionMessageParam,
)
from test_r15_result_owners import _assert_replay

from aistudio_api.api.schemas_compat import OpenAIChatCompletionRequest
from aistudio_api.application.compat_openai import openai_to_gemini_request
from aistudio_api.domain.tool_signatures import tool_history_text
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


@pytest.mark.parametrize("stream", [False, True])
async def test_deprecated_chat_history_reaches_wire_only_as_text(monkeypatch, stream):
    # The still-typed SDK field contains a string, not a parsed argument object.
    # Retain spacing, duplicate keys, escapes, and Unicode without a JSON round trip.
    raw = ' { "city" : "東京", "city" : "\\u6771" }\n'
    old: ChatCompletionAssistantMessageParam = {
        "role": "assistant",
        "content": "Prior note.",
        "function_call": {"name": "weather", "arguments": raw},
    }
    result: ChatCompletionFunctionMessageParam = {
        "role": "function",
        "name": "weather",
        "content": "12 °C",
    }
    sig = "dGhvdWdodA=="
    modern_sig = "bW9kZXJu"
    await _assert_replay(
        monkeypatch,
        "chat",
        stream,
        [
            (
                {
                    **old,
                    "reasoning_content": "The original thought.",
                    "thought_signature": sig,
                    # Extra fields on an old call cannot confer identity or metadata.
                    "function_call": {
                        "name": "weather",
                        "arguments": raw,
                        "id": "not-an-id",
                        "thought_signature": modern_sig,
                    },
                },
                [
                    AistudioPart(
                        text="The original thought.",
                        thought=True,
                        thought_signature=sig,
                    ),
                    AistudioPart(text="Prior note."),
                    AistudioPart(
                        text=tool_history_text("tool_call", "weather", None, raw)
                    ),
                ],
            ),
            (
                result,
                [
                    AistudioPart(
                        text=tool_history_text(
                            "tool_result", "weather", None, {"result": "12 °C"}
                        )
                    )
                ],
            ),
            (
                {
                    "role": "assistant",
                    "function_call": {"name": "empty", "arguments": ""},
                },
                [AistudioPart(text=tool_history_text("tool_call", "empty", None, ""))],
            ),
            (
                {
                    "role": "assistant",
                    "tool_calls": [],
                    "function_call": {"name": "partial", "arguments": ' {"value":'},
                },
                [
                    AistudioPart(
                        text=tool_history_text(
                            "tool_call", "partial", None, ' {"value":'
                        )
                    )
                ],
            ),
            (
                {
                    **old,
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "current",
                            "function": {"name": "weather", "arguments": '{"day": 2}'},
                            "extra_content": {
                                "google": {"thought_signature": modern_sig}
                            },
                        }
                    ],
                },
                [
                    AistudioPart(text="Prior note."),
                    AistudioPart(
                        function_call=("weather", {"day": 2}, "current"),
                        thought_signature=modern_sig,
                    ),
                ],
            ),
            (
                {"role": "tool", "tool_call_id": "current", "content": "14 °C"},
                [
                    AistudioPart(
                        function_response=("weather", {"result": "14 °C"}, "current")
                    )
                ],
            ),
        ],
    )


async def test_malformed_legacy_fields_and_generation_options_are_still_ignored():
    malformed = [
        None,
        12,
        "auto",
        [],
        {},
        {"name": "a"},
        {"arguments": "{}"},
        {"name": 1, "arguments": "{}"},
        {"name": "   ", "arguments": "{}"},
        {"name": "a", "arguments": None},
        {"name": "a", "arguments": {}},
    ]
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "functions": [{"name": "unused"}],
            "function_call": {"name": "unused"},
            "messages": [
                *(
                    {"role": "assistant", "content": "note", "function_call": value}
                    for value in malformed
                ),
                {
                    "role": "user",
                    "content": "note",
                    "function_call": {"name": "unused", "arguments": "{}"},
                },
            ],
        }
    )
    converted, _ = await openai_to_gemini_request(req)
    assert converted.tools is None and converted.toolConfig is None
    assert [
        [p.model_dump(exclude_none=True) for p in turn.parts]
        for turn in converted.contents
    ] == [[{"text": "note"}] for _ in req.messages]
