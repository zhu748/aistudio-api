"""Free-form custom tool history is neither a Gemini function nor an empty turn."""

import json
from unittest.mock import Mock

import openai
import pytest
from openai.types.chat import ChatCompletionMessageCustomToolCall
from openai.types.responses import ResponseCustomToolCall, ResponseCustomToolCallOutput
from test_compat_media_routes import _PNG, _serialized_contents
from test_ordered_tool_routes import wire_source as wire_source
from test_r21_chat_file_ref import (
    no_external_client as no_external_client,
    reject_before_io as reject_before_io,
)
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.application import compat_openai, responses_service
from aistudio_api.domain import tool_signatures
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_ID, _NAME = "private-card-id", "private_card_name"
_INPUT, _OUTPUT = "Read the 藍 swatch.", "The card is indigo."
_CUSTOM = {"name": _NAME, "input": _INPUT}
_CHAT = ChatCompletionMessageCustomToolCall.model_validate(
    {"type": "custom", "id": _ID, "custom": _CUSTOM}
)
_CALL = ResponseCustomToolCall(
    type="custom_tool_call", call_id=_ID, name=_NAME, input=_INPUT
)
_RESULT = ResponseCustomToolCallOutput(
    type="custom_tool_call_output", call_id=_ID, output=_OUTPUT
)
_FUNCTION = {"name": "unknown", "arguments": '{"swatch":"藍"}'}
_CHAT_FN = {"type": "function", "id": "A", "function": _FUNCTION}
_RESP_FN = {"type": "function_call", "call_id": "A", **_FUNCTION}


@pytest.fixture
def no_replay(monkeypatch, reject_before_io):
    unexpected = Mock(side_effect=AssertionError("unexpected tool history conversion"))
    for adapter in (compat_openai, responses_service):
        monkeypatch.setattr(adapter, "_openai_history_call", unexpected)
        monkeypatch.setattr(adapter, "result_part", unexpected)
    monkeypatch.setattr(tool_signatures, "get_signature_cache", unexpected)
    yield
    unexpected.assert_not_called()


async def _rejection(surface, history, stream=False, **kwargs):
    observer = _ASGIObserver()
    async with observer.client() as client:
        with pytest.raises(openai.BadRequestError) as exc:
            if surface == "chat":
                await client.chat.completions.create(
                    model=MODEL, messages=history, stream=stream, **kwargs
                )
            else:
                await client.responses.create(
                    model=MODEL, input=history, stream=stream, **kwargs
                )
    assert observer.statuses == [400] and not observer.heartbeat.is_set()
    assert exc.value.type == "invalid_request_error"
    body = exc.value.body
    assert isinstance(body, dict) and body["code"] == 400
    return body["message"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("surface", "item", "kind"),
    [
        ("chat", _CHAT, "custom"),
        ("chat", {**_CHAT.model_dump(), "function": _FUNCTION}, "custom"),
        ("chat", {"type": "custom"}, "custom"),
        ("responses", _CALL, "custom_tool_call"),
        ("responses", {"type": "custom_tool_call"}, "custom_tool_call"),
        ("responses", _RESULT, "custom_tool_call_output"),
        ("responses", {"type": "custom_tool_call_output"}, "custom_tool_call_output"),
    ],
    ids=[
        "chat",
        "stray-function",
        "empty-chat",
        "call",
        "empty-call",
        "output",
        "empty-output",
    ],
)
async def test_custom_history_rejects_before_conversion(
    no_replay, surface, item, kind, stream
):
    urls = ["data:image/png;base64," + _PNG["A"], "https://fixture.invalid/card"]
    if surface == "chat":
        blocks = [{"type": "image_url", "image_url": {"url": url}} for url in urls]
        history = [
            {"role": "user", "content": blocks},
            {"role": "assistant", "tool_calls": [_CHAT_FN, item]},
            {"role": "tool", "tool_call_id": _ID, "content": _OUTPUT},
        ]
        path, alternative = "messages[1].tool_calls[1].type", "function tool calls"
    else:
        blocks = [
            {"type": "input_image", "detail": "auto", "image_url": url} for url in urls
        ]
        history = [
            {"role": "user", "content": blocks},
            _RESP_FN,
            item,
            {"type": "function_call_output", "call_id": "A", "output": "Checked."},
        ]
        path, alternative = "input[2].type", "function_call_output items"
    message = await _rejection(surface, history, stream)
    assert f"{path} ({kind}) is not supported" in message
    assert alternative in message and "plain text" in message
    assert all(
        private not in message for private in (_ID, _NAME, _INPUT, _OUTPUT, *urls)
    )


@pytest.mark.parametrize(
    ("surface", "history", "kwargs", "first"),
    [
        ("chat", [{"role": "assistant", "tool_calls": [_CHAT]}], {"n": 2}, "Only n=1"),
        (
            "chat",
            [
                {
                    "role": "user",
                    "content": [{"type": "file", "file": {"file_id": _ID}}],
                },
                {"role": "assistant", "tool_calls": [_CHAT]},
            ],
            {},
            "messages[0].content[0].file.file_id",
        ),
        (
            "responses",
            [_CALL],
            {"previous_response_id": _ID, "conversation": _ID},
            "previous_response_id",
        ),
        ("responses", [_CALL], {"conversation": _ID}, "conversation is not supported"),
        ("responses", [{"id": _ID}, _RESULT], {}, "input[0].id (item_reference)"),
        (
            "responses",
            [_RESULT, {"id": _ID}],
            {},
            "input[0].type (custom_tool_call_output)",
        ),
    ],
)
async def test_existing_preflight_priority(no_replay, surface, history, kwargs, first):
    assert first in await _rejection(surface, history, **kwargs)


@pytest.mark.parametrize("surface", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_nearby_function_history_keeps_exact_calls_and_results(
    route, surface, stream
):
    wire = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Done."))
    # Only the discriminator on an assistant call or top-level Responses item
    # selects the unsupported variant. Keep loose clients' old shapes intact.
    types = [{"type": "function"}, {}]  # Chat also accepts an omitted discriminator.
    if surface == "chat":
        types.extend(
            {"type": value}
            for value in (None, "custom_tool_call", ["custom"], {"type": "custom"})
        )
    calls, results, expected_calls, expected_results = [], [], [], []
    for row, kind in enumerate(types):
        key = f"row-{row}"
        name = "unknown" if row == 0 else "read_swatch"
        args = {"swatch": "藍", "row": row}
        fn = {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
        output = key + " blue"
        if surface == "chat":
            calls.append({"id": key, "function": fn, "custom": _CUSTOM, **kind})
            results.append({"role": "tool", "tool_call_id": key, "content": output})
        else:
            calls.append(
                {"type": "function_call", "call_id": key, **fn, "custom": _CUSTOM}
            )
            results.append(
                {"type": "function_call_output", "call_id": key, "output": output}
            )
        expected_calls.append(AistudioPart(function_call=(name, args, key)).to_wire())
        expected_results.append(
            AistudioPart(function_response=(name, {"result": output}, key)).to_wire()
        )
    if surface == "chat":
        history = [
            {"role": "user", "content": "Start.", "tool_calls": [_CHAT.model_dump()]},
            {"role": "assistant", "tool_calls": calls},
            *reversed(results),
        ]
        path, fields = "/v1/chat/completions", {"messages": history}
        declaration = {"type": "custom", "custom": {"name": _NAME}}
    else:
        history = [
            {
                "type": None,
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Start."},
                    _CALL.model_dump(),
                ],
            },
            *(
                {"type": value}
                for value in (
                    "custom",
                    ["custom_tool_call"],
                    {"type": "custom_tool_call_output"},
                )
            ),
            *calls,
            *reversed(results),
        ]
        path, fields = "/v1/responses", {"input": history}
        declaration = {"type": "custom", "name": _NAME}
    response = await route.raw.post(
        path,
        json={
            "model": MODEL,
            "stream": stream,
            **fields,
            "tools": [declaration],
        },
    )
    assert response.status_code == 200 and "Done." in response.text
    assert wire == [
        [
            [[AistudioPart(text="Start.").to_wire()], "user"],
            [expected_calls, "model"],
            [expected_results[::-1], "user"],
        ]
    ]
