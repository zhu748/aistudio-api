"""Source hints belong to exact part objects in one request, not to their values."""

import base64
import gc
import json
import weakref
from typing import Any

import anthropic
import httpx2
from fastapi import FastAPI

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import GeminiPart
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart
from aistudio_api.infrastructure.request_context import RequestContext, current_request
from aistudio_api.infrastructure.signature_trace import (
    ThoughtSignatureTrace,
    get_signature_trace,
)


def test_retag_only_updates_the_exact_owner():
    trace = ThoughtSignatureTrace(protocol="openai")
    a, b, c = (GeminiPart(text="same") for _ in range(3))
    assert a == b == c
    trace.tag_inbound_source(a, "first")
    trace.tag_inbound_source(b, "second")
    trace.tag_inbound_source(a, "latest")
    trace.tag_inbound_source(a, "")
    trace.tag_inbound_source(c, "")
    assert [trace.lookup_inbound_source(p) for p in (a, b, c)] == [
        "latest",
        "second",
        "client_openai",
    ]
    assert len(trace._inbound_sources) == 2


def test_owner_lives_with_its_request_but_not_with_serialized_diagnostics():
    context = RequestContext("fixture")
    token = current_request.set(context)
    try:
        trace = get_signature_trace(create=True)
        assert trace is not None
        part = GeminiPart(text="private fixture contents")
        ref = weakref.ref(part)
        trace.tag_inbound_source(part, "fixture carrier")
        trace.reset_attempt()
        assert ThoughtSignatureTrace().lookup_inbound_source(part) == "client_native"
        del part
        gc.collect()
        assert ref() is not None
        assert trace.lookup_inbound_source(ref()) == "fixture carrier"
        trace.sync()
        saved = json.dumps(context.thought_signature)
        assert (
            "private fixture contents" not in saved and "fixture carrier" not in saved
        )
    finally:
        current_request.reset(token)
    del context, trace
    gc.collect()
    assert ref() is None


async def test_strict_messages_sdk_preserves_order_and_per_call_source(monkeypatch):
    # Only the in-process HTTP route, request normalization and SDK parser run.
    app = FastAPI()
    app.include_router(routes_compat.router)
    app.dependency_overrides[get_client] = lambda: object()
    markers = {
        key: base64.b64encode(f"offline source fixture {key}".encode()).decode()
        for key in "AB"
    }
    sources = [
        {"extra_content": {"google": {"thought_signature": markers["A"]}}},
        {"provider_specific_fields": {"thoughtSignature": markers["B"]}},
    ]
    calls = [
        {"type": "tool_use", "id": k, "name": "read", "input": {"key": k}, **src}
        for k, src in zip("AB", sources, strict=True)
    ]
    results = [
        {"type": "tool_result", "tool_use_id": k, "content": f"sample {k}"}
        for k in "BA"
    ]
    messages: Any = [
        {"role": "user", "content": "Compare."},
        {"role": "assistant", "content": calls},
        {"role": "user", "content": results},
    ]
    expected = [
        AistudioContent("user", [AistudioPart(text="Compare.")]),
        AistudioContent(
            "model",
            [
                AistudioPart(
                    function_call=("read", {"key": k}, k), thought_signature=markers[k]
                )
                for k in "AB"
            ],
        ),
        AistudioContent(
            "user",
            [
                AistudioPart(function_response=("read", {"result": f"sample {k}"}, k))
                for k in "BA"
            ],
        ),
    ]

    def ordered_bytes(contents):
        return json.dumps(
            [turn.to_wire() for turn in contents], separators=(",", ":")
        ).encode()

    async def local_reply(model, request, _client, **kwargs):
        assert ordered_bytes(
            normalize_gemini_request(request, model).contents
        ) == ordered_bytes(expected)
        trace = get_signature_trace()
        assert trace is not None
        assert [
            (item["call_id"], item["source"]) for item in trace.client_in_items
        ] == [
            ("A", "client_extra_content"),
            ("B", "client_provider_fields"),
        ]
        assert all(raw not in json.dumps(trace.to_dict()) for raw in markers.values())
        return GeminiGenerateContentResponse.model_validate(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "Noted."}]}, "finishReason": "STOP"}
                ]
            }
        )

    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", local_reply)
    token = current_request.set(RequestContext("sdk-fixture"))
    try:
        async with anthropic.AsyncAnthropic(
            base_url="http://fixture.invalid",
            api_key="fixture",
            http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app)),
            max_retries=0,
            _strict_response_validation=True,
        ) as client:
            reply = await client.messages.create(
                model="sample", max_tokens=16, messages=messages
            )
            assert reply.stop_reason == "end_turn"
            assert reply.content[0].type == "text" and reply.content[0].text == "Noted."
    finally:
        current_request.reset(token)
