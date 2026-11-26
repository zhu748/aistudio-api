"""Round-trip signed Part ownership through the public routes and stock SDKs."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import anthropic
import httpx
import httpx2
import openai
import pytest
from fastapi import FastAPI
from google import genai
from google.genai import types

from aistudio_api.api.dependencies import get_client
from aistudio_api.api.routes_compat import router as compat_router
from aistudio_api.api.routes_gemini import router as native_router
from aistudio_api.application import api_service_gemini
from aistudio_api.domain import tool_signatures
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_part
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

MODEL = "gemini-3.8-flash"
PROMPT = {"role": "user", "content": "Start here."}
FOLLOWUP = {"role": "user", "content": "Continue."}
PROTOCOLS = ("native", "chat", "claude", "responses")
COMPAT = PROTOCOLS[1:]
HEADERS = {"X-Anti-Truncation": "false"}
ENDPOINT = {
    "chat": "/v1/chat/completions",
    "claude": "/v1/messages",
    "responses": "/v1/responses",
}


def signature(label):
    return base64.b64encode(("opaque-provider-fixture:" + label).encode()).decode()


class WireFixture:
    """Real parser and routes; capture the normalized replay with no raw-turn cache."""

    def __init__(self):
        self.raw = b""
        self.model_wire = []

    def reply(self, *parts):
        chunks = [
            [[[[[part.to_wire()], "model"], 1 if i + 1 == len(parts) else None]]]
            for i, part in enumerate(parts)
        ]
        self.raw = json.dumps([chunks], ensure_ascii=False).encode()

    async def prepare(self, *, contents, **kwargs):
        self.model_wire = [
            part.to_wire()
            for turn in contents
            if turn.role == "model"
            for part in turn.parts
        ]
        return PreparedRequest(url="https://fixture.invalid", headers={}, body="[]")

    async def send_request(self, **kwargs):
        return 200, self.raw

    async def send_streaming_request(self, **kwargs):
        yield "status", 200
        for offset in range(0, len(self.raw), 11):
            yield "chunk", self.raw[offset : offset + 11]

    def replayed(self):
        return [parse_response_part(part) for part in self.model_wire]


@pytest.fixture
async def route(monkeypatch, tmp_path):
    wire = WireFixture()
    gateway = cast(Any, AIStudioClient.__new__(AIStudioClient))
    gateway._session = wire
    gateway.prepare_request = wire.prepare
    gateway._streaming_gateway = StreamingGateway(cast(Any, wire))
    app = FastAPI()
    app.include_router(compat_router)
    app.include_router(native_router)
    app.dependency_overrides[get_client] = lambda: gateway
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    cache = tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json")
    monkeypatch.setattr(tool_signatures, "_cache_instance", cache)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture.invalid",
        headers=HEADERS,
    ) as raw:
        yield SimpleNamespace(wire=wire, app=app, raw=raw, cache=cache)


def thought(text, label):
    return AistudioPart(text=text, thought=True, thought_signature=signature(label))


def turn_params(protocol: str) -> dict[str, Any]:
    if protocol == "responses":
        return {"model": MODEL, "input": PROMPT["content"]}
    return {
        "model": MODEL,
        "messages": [PROMPT],
        **({"max_tokens": 128} if protocol == "claude" else {}),
    }


def _native_parts(response: types.GenerateContentResponse) -> Iterator[types.Part]:
    for candidate in response.candidates or []:
        if candidate.content is not None:
            yield from candidate.content.parts or []


async def _native_sdk_turn(route, stream):
    client = genai.Client(
        api_key="fixture",
        http_options=types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            httpx_async_client=route.raw,
            headers=HEADERS,
        ),
    )
    prompt = types.Content(role="user", parts=[types.Part(text=PROMPT["content"])])
    async with client.aio as sdk:
        if stream:
            output = [
                part
                async for frame in await sdk.models.generate_content_stream(
                    model=MODEL, contents=[prompt]
                )
                for part in _native_parts(frame)
            ]
        else:
            reply = await sdk.models.generate_content(model=MODEL, contents=[prompt])
            output = list(_native_parts(reply))
        route.wire.reply(AistudioPart(text="Continuing."))
        await sdk.models.generate_content(
            model=MODEL,
            contents=[
                prompt,
                types.Content(role="model", parts=output),
                types.Content(
                    role="user", parts=[types.Part(text=FOLLOWUP["content"])]
                ),
            ],
        )


async def _claude_sdk_turn(client, params, stream):
    if not stream:
        return await client.messages.create(**params)
    async with client.messages.stream(**params) as response:
        active = None
        block_kind = ""
        signature_seen = False
        next_index = 0
        async for event in response:
            if event.type == "content_block_start":
                assert active is None and event.index == next_index
                active = event.index
                block_kind = event.content_block.type
                signature_seen = False
                next_index += 1
            elif event.type == "content_block_delta":
                # Strict ordering in addition to the official accumulator, which
                # can accept a signature delta after content_block_stop.
                assert active == event.index
                assert not signature_seen
                if event.delta.type == "signature_delta":
                    assert block_kind == "thinking"
                    assert event.delta.signature
                    signature_seen = True
            elif event.type == "content_block_stop":
                assert active == event.index
                active = None
        assert active is None
        return await response.get_final_message()


async def _sdk_roundtrip(route, protocol, stream):
    if protocol == "native":
        await _native_sdk_turn(route, stream)
        return
    params = turn_params(protocol)
    transport = httpx2.ASGITransport(route.app)
    options: dict[str, Any] = {
        "api_key": "fixture",
        "http_client": httpx2.AsyncClient(transport=transport),
        "default_headers": HEADERS,
        "_strict_response_validation": True,
        "max_retries": 0,
    }
    if protocol == "claude":
        async with anthropic.AsyncAnthropic(
            base_url="http://fixture.invalid", **options
        ) as client:
            message = await _claude_sdk_turn(client, params, stream)
            content = [block.model_dump(exclude_none=True) for block in message.content]
            route.wire.reply(AistudioPart(text="Continuing."))
            params["messages"] = [
                PROMPT,
                {"role": "assistant", "content": content},
                FOLLOWUP,
            ]
            await client.messages.create(**params)
        return
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **options
    ) as client:
        if protocol == "chat":
            if stream:
                async with client.chat.completions.stream(**params) as response:
                    result = await response.get_final_completion()
            else:
                result = await client.chat.completions.create(**params)
            assistant = result.choices[0].message.model_dump(exclude_none=True)
            route.wire.reply(AistudioPart(text="Continuing."))
            params["messages"] = [PROMPT, assistant, FOLLOWUP]
            await client.chat.completions.create(**params)
        else:
            if stream:
                async with client.responses.stream(**params) as response:
                    result = await response.get_final_response()
            else:
                result = await client.responses.create(**params)
            items = [item.model_dump(exclude_none=True) for item in result.output]
            route.wire.reply(AistudioPart(text="Continuing."))
            params["input"] = [PROMPT, *items, FOLLOWUP]
            await client.responses.create(**params)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
async def test_signed_empty_thought_does_not_inherit_or_move_detached_signatures(
    route, protocol, stream
):
    owning = [
        thought("", "first, really empty"),
        AistudioPart(text="The answer.", thought_signature=signature("visible")),
        AistudioPart(thought=True, thought_signature=signature("no text key")),
        AistudioPart(thought_signature=signature("detached")),
        AistudioPart(text="", thought_signature=signature("empty non-thought tail")),
    ]
    route.wire.reply(*owning)
    await _sdk_roundtrip(route, protocol, stream)
    expected = (
        owning
        if protocol == "native"
        else [owning[0], AistudioPart(text="The answer.")]
    )
    assert route.wire.model_wire == [part.to_wire() for part in expected]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
async def test_independently_signed_thoughts_and_empty_sibling_round_trip_cold(
    route, protocol, stream
):
    owning = [
        thought("alpha", "first"),
        thought("", "middle"),
        thought("beta", "last"),
        AistudioPart(text="The answer."),
    ]
    route.wire.reply(*owning)
    await _sdk_roundtrip(route, protocol, stream)
    expected = (
        [AistudioPart(text="alphabeta", thought=True), owning[-1]]
        if protocol == "chat"
        else owning
    )
    assert route.wire.model_wire == [part.to_wire() for part in expected]


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_does_not_sign_a_thought_that_scalar_replay_would_move(
    route, stream
):
    first, second = AistudioPart(text="Body first"), thought("later", "late thought")
    route.wire.reply(first, second)
    await _sdk_roundtrip(route, "chat", stream)
    assert [(part.text, part.thought_signature) for part in route.wire.replayed()] == [
        ("later", ""),
        ("Body first", ""),
    ]


CALL: tuple[str, dict[str, object], str] = (
    "lookup",
    {"city": "Paris"},
    "call-fixture",
)
COMPOUNDS = (
    ("Compound caption", {"inline_data": ("image/png", "aGVsbG8=")}),
    (
        "Compound caption",
        {
            "executable_code": ("PYTHON", "print(1)"),
            "code_execution_result": ("OUTCOME_OK", "1"),
        },
    ),
    ("Compound caption", {"function_call": CALL}),
    ("", {"function_call": CALL}),
)


@pytest.mark.parametrize(("caption", "payload"), COMPOUNDS)
@pytest.mark.parametrize("stream", [False, True])
async def test_native_cold_sdk_replay_keeps_compound_fields_on_one_signed_part(
    route, caption, payload, stream
):
    # The function wire reader understands both browser response and request
    # slots; the request produced by echo must reproduce the complete Part.
    original = AistudioPart(
        text=caption, thought=True, thought_signature=signature("owner"), **payload
    )
    answer = AistudioPart(text="The answer.")
    route.wire.reply(original, answer)
    await _sdk_roundtrip(route, "native", stream)
    assert route.wire.model_wire == [original.to_wire(), answer.to_wire()]


@pytest.mark.parametrize(
    "unrepresentable",
    [
        {"executableCode": {"language": "PYTHON", "code": "1", "id": "code-id"}},
        {"codeExecutionResult": {"outcome": "OUTCOME_UNSPECIFIED", "output": "1"}},
    ],
)
async def test_native_does_not_replay_signature_on_lossy_code_metadata(
    route, unrepresentable
):
    route.wire.reply(AistudioPart(text="Continuing."))
    response = await route.raw.post(
        f"/v1beta/models/{MODEL}:generateContent",
        json={
            "contents": [
                {"role": "user", "parts": [{"text": "Start here."}]},
                {
                    "role": "model",
                    "parts": [
                        {
                            "text": "Earlier answer.",
                            "thoughtSignature": signature("non-replayable"),
                            **unrepresentable,
                        }
                    ],
                },
                {"role": "user", "parts": [{"text": "Continue."}]},
            ]
        },
    )
    assert response.status_code == 200, response.text
    part = route.wire.replayed()[0]
    assert part.text == "Earlier answer."
    assert not part.thought_signature


@pytest.mark.parametrize("protocol", COMPAT)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("caption", "payload"), COMPOUNDS)
async def test_compat_never_moves_compound_signature_or_caches_it_for_the_call(
    route, protocol, stream, caption, payload
):
    sig = signature("compound")
    route.wire.reply(
        AistudioPart(text=caption, thought=True, thought_signature=sig, **payload),
        AistudioPart(text="The answer."),
    )
    response = await route.raw.post(
        ENDPOINT[protocol], json={**turn_params(protocol), "stream": stream}
    )
    assert response.status_code == 200, response.text
    assert caption in response.text and "The answer." in response.text
    assert sig not in response.text
    assert (
        base64.b64encode(
            json.dumps({"aistudio_thought_signature": sig}).encode()
        ).decode()
        not in response.text
    )
    if "function_call" in payload:
        assert "lookup" in response.text
        assert route.cache.lookup("call-fixture", "lookup", {"city": "Paris"}) is None


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("anchor_has_caption", [False, True])
async def test_later_snapshot_does_not_move_signature_between_different_envelopes(
    route, protocol, stream, anchor_has_caption
):
    first = signature("exact initial owner")
    later = signature("different later owner")
    route.wire.reply(
        AistudioPart(
            text="Caption." if anchor_has_caption else None,
            thought_signature=first,
            function_call=CALL,
        ),
        AistudioPart(
            text=None if anchor_has_caption else "Caption.",
            thought_signature=later,
            function_call=CALL,
        ),
    )
    if protocol == "native":
        action = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1beta/models/{MODEL}:{action}"
        params = {"contents": [{"role": "user", "parts": [{"text": "Start."}]}]}
    else:
        path = ENDPOINT[protocol]
        params = {**turn_params(protocol), "stream": stream}
    response = await route.raw.post(path, json=params)
    assert response.status_code == 200, response.text
    assert later not in response.text
    assert (first in response.text) is (protocol == "native" or not anchor_has_caption)
    assert "lookup" in response.text and "Caption." in response.text
    cached = route.cache.lookup("call-fixture", "lookup", {"city": "Paris"})
    assert cached == (None if anchor_has_caption else first)


@pytest.mark.parametrize("protocol", COMPAT)
@pytest.mark.parametrize("shape", ["absent", "invalid"])
async def test_replay_does_not_infer_empty_thought_from_signature_alone(
    route, protocol, shape
):
    route.wire.reply(AistudioPart(text="Continuing."))
    sig = signature("no owner") if shape == "absent" else "invalid#base64"
    extra = {"extra_content": {"google": {"thought_signature": sig}}}
    params = turn_params(protocol)
    if protocol == "claude":
        envelope = base64.b64encode(
            json.dumps({"aistudio_thought_signature": sig}).encode()
        ).decode()
        params["messages"] = [
            PROMPT,
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "signature": envelope,
                        **({"thinking": ""} if shape == "invalid" else {}),
                    },
                    {"type": "text", "text": "Earlier answer."},
                ],
            },
            FOLLOWUP,
        ]
    elif protocol == "chat":
        params["messages"] = [
            PROMPT,
            {
                "role": "assistant",
                "content": "Earlier answer.",
                **extra,
                **({"reasoning_content": ""} if shape == "invalid" else {}),
            },
            FOLLOWUP,
        ]
    else:
        params["input"] = [
            PROMPT,
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": ""}]
                if shape == "invalid"
                else [],
                **extra,
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Earlier answer."}],
            },
            FOLLOWUP,
        ]
    response = await route.raw.post(ENDPOINT[protocol], json=params)
    assert response.status_code == 200, response.text
    assert route.wire.model_wire == [AistudioPart(text="Earlier answer.").to_wire()]
