"""A compatibility result's display label is not evidence of call ownership."""

import base64
import json

import anthropic
import httpx2
import openai
import pytest
from anthropic.types import TextBlock
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from test_compat_media_routes import _PNG

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import (
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiPart,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.compat_result_owners import result_part
from aistudio_api.domain.tool_signatures import tool_history_text
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_MODEL = "gemini-3.8-flash"
_ID = "observation"
_SURFACES = ["chat", "responses", "claude"]


def _call(surface, name, n, key: str | None = _ID):
    args = {"n": n}
    sig = base64.b64encode(f"fixture {n}".encode()).decode()
    common = {"extra_content": {"google": {"thought_signature": sig}}}
    ids = {"id": key} if key else {}
    if surface == "chat":
        tool = {
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
            **ids,
            **common,
        }
        message = {"role": "assistant", "tool_calls": [tool]}
    elif surface == "responses":
        message = {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(args),
            **({"call_id": key} if key else {}),
            **common,
        }
    else:
        tool = {"type": "tool_use", "name": name, "input": args, **ids, **common}
        message = {"role": "assistant", "content": [tool]}
    return message, [
        AistudioPart(
            function_call=(name, args, key) if key else (name, args),
            thought_signature=sig,
        )
    ]


def _output(
    surface, text, label, *, key=_ID, orphan=False, hint=None, media=False, legacy=False
):
    attachments = []
    if surface == "chat":
        message = {
            "role": "function" if legacy else "tool",
            "content": text,
            **({"name": hint} if hint else {}),
            **({"tool_call_id": key} if not legacy else {}),
        }
    elif surface == "responses":
        content = text
        if media:
            content = [
                {"type": "input_text", "text": text},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + _PNG["A"],
                },
                {
                    "type": "input_file",
                    "filename": "note.txt",
                    "file_data": "data:text/plain;base64,YWI=",
                },
            ]
            attachments = [
                AistudioPart(inline_data=("image/png", _PNG["A"])),
                AistudioPart(
                    inline_data=("text/plain", "YWI="), inline_filename="note.txt"
                ),
            ]
        message = {"type": "function_call_output", "call_id": key, "output": content}
    else:
        content = text
        if media:
            content = [
                {"type": "text", "text": text},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _PNG["A"],
                    },
                },
            ]
            attachments = [AistudioPart(inline_data=("image/png", _PNG["A"]))]
        result = {"type": "tool_result", "tool_use_id": key, "content": content}
        message = {"role": "user", "content": [result]}
    payload = {"result": text}
    if orphan:
        part = AistudioPart(text=tool_history_text("tool_result", label, key, payload))
    else:
        part = AistudioPart(function_response=(label, payload, key))
    return message, [part, *attachments]


async def _assert_replay(monkeypatch, surface, stream, steps):
    first, last = "Begin.", "End."
    messages = [
        {"role": "user", "content": first},
        *(message for message, _ in steps),
        {"role": "user", "content": last},
    ]
    expected = [
        AistudioPart(text=first),
        *(part for _, parts in steps for part in parts),
        AistudioPart(text=last),
    ]
    seen = []

    async def generate(model, request, client, *, stream, **kwargs):
        # Exercise the production normalizer and wire part encoder. Nothing is
        # sent to a provider and none of the historical calls are invoked.
        normalized = normalize_gemini_request(request, model)
        seen.append(
            [part.to_wire() for turn in normalized.contents for part in turn.parts]
        )
        for turn in request.contents:
            for part in turn.parts:
                if (fr := part.functionResponse) is not None:
                    public = fr.model_dump(exclude_unset=True)
                    assert not any(k.startswith("_") for k in public)
                    assert fr == GeminiFunctionResponse.model_validate(public)
        frame = {
            "candidates": [
                {"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": 1,
                "candidatesTokenCount": 1,
                "totalTokenCount": 2,
            },
        }
        if stream:
            return StreamingResponse(
                iter([f"data: {json.dumps(frame)}\n\n"]), media_type="text/event-stream"
            )
        return GeminiGenerateContentResponse.model_validate(frame)

    app = FastAPI()
    app.include_router(routes_compat.router)
    app.dependency_overrides[get_client] = object
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", generate)
    options = {
        "api_key": "fixture",
        "http_client": httpx2.AsyncClient(transport=httpx2.ASGITransport(app)),
        "_strict_response_validation": True,
        "max_retries": 0,
    }
    if surface == "claude":
        async with anthropic.AsyncAnthropic(
            base_url="http://fixture.invalid", **options
        ) as client:
            kwargs = {
                "model": _MODEL,
                "messages": messages,
                "max_tokens": 8,
                "tools": [],
            }
            if stream:
                async with client.messages.stream(**kwargs) as response:
                    reply = await response.get_final_message()
            else:
                reply = await client.messages.create(**kwargs)
            (block,) = reply.content
            assert isinstance(block, TextBlock) and block.text == "OK"
    else:
        async with openai.AsyncOpenAI(
            base_url="http://fixture.invalid/v1", **options
        ) as client:
            if surface == "chat":
                kwargs = {"model": _MODEL, "messages": messages, "tools": []}
                if stream:
                    async with client.chat.completions.stream(**kwargs) as response:
                        reply = await response.get_final_completion()
                else:
                    reply = await client.chat.completions.create(**kwargs)
                assert reply.choices[0].message.content == "OK"
            else:
                kwargs = {"model": _MODEL, "input": messages, "tools": []}
                if stream:
                    async with client.responses.stream(**kwargs) as response:
                        reply = await response.get_final_response()
                else:
                    reply = await client.responses.create(**kwargs)
                assert reply.output_text == "OK"
    assert seen == [[p.to_wire() for p in expected]]


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("stream", [False, True])
async def test_conflicting_id_is_exact_only_within_its_pending_turn(
    monkeypatch, surface, stream
):
    steps = [
        # Different adapters used to pick either a past or a future name, then
        # attach the result to a different call that did not even have an ID.
        _call(surface, "temperature", 0, None),
        _output(surface, "early 🌦", "unknown", orphan=True, media=True),
        _call(surface, "temperature", 1),
        _output(surface, "12 °C", "temperature", hint="display"),
        _call(surface, "humidity", 2),
        _output(surface, "56%", "humidity", hint="display"),
        _call(surface, "humidity", 3, None),
        _output(surface, "stale", "unknown", orphan=True),
        _call(surface, "unknown", 4, None),
        _output(surface, "literal tool name", "unknown", orphan=True),
        _call(surface, "display", 5, None),
        _output(
            surface,
            "hint only",
            "display" if surface == "chat" else "unknown",
            hint="display",
            orphan=True,
        ),
        # Neither of two current calls nor two results with the same ID identify a unique pair.
        _call(surface, "temperature", 6),
        _call(surface, "humidity", 7),
        _output(surface, "shared", "unknown", orphan=True),
        _call(surface, "humidity", 8),
        _output(surface, "one", "unknown", orphan=True),
        _output(surface, "two", "unknown", orphan=True),
        # A conflicting global ID cannot displace the unique exact results in a
        # mixed pending batch, even when a different call matches its display name.
        _call(surface, "display", 9, None),
        _call(surface, "humidity", 10),
        _call(surface, "wind", 11, "independent"),
        _output(surface, "calm", "wind", key="independent"),
        _output(surface, "40%", "humidity", hint="display"),
    ]
    if surface == "chat":
        item, parts = _output(
            surface, "old client", "legacy", hint="legacy", orphan=True
        )
        steps.extend(
            [_call(surface, "legacy", 12, None), ({**item, "role": "function"}, parts)]
        )
    elif surface == "claude":
        # A malformed model-side tool_result isn't a native server-issued result.
        item, parts = _output(surface, "misplaced", "unknown", orphan=True)
        steps.append(({**item, "role": "assistant"}, parts))
    await _assert_replay(monkeypatch, surface, stream, steps)


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("stream", [False, True])
async def test_enumerable_names_and_legacy_results_are_unchanged(
    monkeypatch, surface, stream
):
    steps = [
        _output(surface, "early", "temperature", orphan=True, hint="unused"),
        _call(surface, "temperature", 1),
        _output(surface, "now", "temperature", hint="unused"),
        _call(surface, "temperature", 2),
        _call(surface, "temperature", 3),
        _output(surface, "same-name duplicate", "temperature", orphan=True),
        # A real, nonconflicting name "unknown" can still pair by name.
        _call(surface, "unknown", 4, None),
        _output(surface, "matched", "unknown", key="other"),
        _call(surface, "unknown", 5, "other"),
        _output(surface, "exact", "unknown", key="other"),
    ]
    if surface == "chat":
        steps += [
            _call(surface, "legacy", 6, "old"),
            _output(
                surface,
                "by name alone",
                "legacy",
                key="old",
                hint="legacy",
                legacy=True,
            ),
        ]
    elif surface == "responses":
        draft, _ = _call(surface, "not an owner", 6)
        steps.insert(0, ({**draft, "status": "in_progress"}, []))
    await _assert_replay(monkeypatch, surface, stream, steps)


def test_native_result_still_matches_its_literal_name_and_ignores_private_input():
    request = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [
                {"role": "model", "parts": [{"functionCall": {"name": "unknown"}}]},
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": _ID,
                                "name": "unknown",
                                "response": {"value": 3},
                                "_compat_owner": {"exact_id_only": True},
                            },
                            "thoughtSignature": "bmF0aXZl",
                        }
                    ],
                },
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"id": _ID, "name": name}}
                        for name in ("a", "b")
                    ],
                },
                {"role": "user", "parts": [{"text": "Done."}]},
            ],
            "tools": [],
        }
    )
    result = normalize_gemini_request(request, _MODEL).contents[1].parts[0]
    assert result.function_response == ("unknown", {"value": 3}, _ID)
    assert result.thought_signature == "bmF0aXZl"


@pytest.mark.parametrize("exact", [False, True], ids=["orphan", "canonicalized"])
@pytest.mark.parametrize("child", [False, True], ids=["scalar", "attachment"])
def test_resolving_a_display_label_never_moves_an_opaque_part_marker(exact, child):
    result = result_part(
        {_ID: None},
        GeminiFunctionResponse(
            id=_ID,
            name="display",
            response={"number": 7},
            parts=[GeminiPart(text="caption", thoughtSignature="Y2hpbGQ=")]
            if child
            else None,
        ),
    )
    result.thoughtSignature = "cGFyZW50"
    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"id": _ID, "name": "recorded"}
                            if exact
                            else {"name": "display"}
                        }
                    ],
                },
                {"role": "user", "parts": [result]},
            ],
            "tools": [],
        }
    )
    parts = normalize_gemini_request(req, _MODEL).contents[1].parts
    expected = [
        AistudioPart(function_response=("recorded", {"number": 7}, _ID))
        if exact
        else AistudioPart(
            text=tool_history_text("tool_result", "display", _ID, {"number": 7})
        )
    ]
    if child:
        expected.append(AistudioPart(text="caption", thought_signature="Y2hpbGQ="))
    assert parts == expected
