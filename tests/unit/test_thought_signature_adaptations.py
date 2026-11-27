"""Regression tests for Phase 2 thoughtSignature protocol & resilience adaptations."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import httpx2
import openai
import pytest
from fastapi import FastAPI
from openai.types.responses import ResponseInputParam

from aistudio_api.api.dependencies import get_client
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.routes_compat import router as compat_router
from aistudio_api.api.routes_gemini import router as gemini_router
from aistudio_api.api.schemas_compat import OpenAIChatCompletionRequest
from aistudio_api.application import api_service_gemini
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.compat_openai import openai_to_gemini_request
from aistudio_api.application.compat_shared import (
    decode_signature_envelope,
    encode_signature_envelope,
)
from aistudio_api.config import settings
from aistudio_api.domain import tool_signatures
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    valid_thought_signature,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_turns import ModelTurnHistory
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_part
from aistudio_api.infrastructure.gateway.wire_spec import PartIndex, TopLevelIndex
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioPart,
)
from aistudio_api.infrastructure.request_context import RequestContext, current_request
from aistudio_api.infrastructure.request_history import request_history
from aistudio_api.infrastructure.signature_trace import get_signature_trace
from aistudio_api.infrastructure.utils import logger as log_module

MODEL = "gemini-3.8-flash"
SIG_A = base64.b64encode(b"phase2-fixture-signature-alpha").decode()
SIG_B = base64.b64encode(b"phase2-fixture-signature-beta").decode()
TOKEN = "v1_" + "p" * 24


def _wire_response(parts: list[AistudioPart], token: str | None = TOKEN) -> bytes:
    chunk = [
        [[[[p.to_wire() for p in parts], "model"], 1]],
        None,
        [10, 5, 15],
        None,
        None,
        None,
        None,
        token,
    ]
    return json.dumps([[chunk]], ensure_ascii=False).encode()


@pytest.fixture
async def gateway_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AsyncIterator[tuple[httpx.AsyncClient, AIStudioClient, list[bytes], list[str]]]:
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "persist_stats", False)
    monkeypatch.setattr(settings, "persist_rotator", False)
    monkeypatch.setattr(settings, "request_history", True)
    monkeypatch.setattr(log_module, "_dump_requests_override", False)
    monkeypatch.setattr(log_module, "_dump_to_file_override", False)
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(
        tool_signatures,
        "_cache_instance",
        tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json"),
    )

    replies: list[bytes] = []
    sent_bodies: list[str] = []
    gw_client = AIStudioClient()
    active_ctx = [
        SessionContext(
            auth_file="/data/account-a/auth.json",
            cookies={"SAPISID": "fixture"},
        )
    ]
    gw_client.session.ensure_ready = AsyncMock(side_effect=lambda: active_ctx[0])
    gw_client.session.generate_snapshot = AsyncMock(return_value="!proof")

    async def _send_request(**kwargs: object) -> tuple[int, bytes]:
        sent_bodies.append(str(kwargs.get("body", "")))
        return 200, replies.pop(0)

    async def _send_streaming(**kwargs: object):
        sent_bodies.append(str(kwargs.get("body", "")))
        raw = replies.pop(0)
        yield "status", 200
        yield "chunk", raw

    gw_client.session.send_request = AsyncMock(side_effect=_send_request)
    gw_client.session.send_streaming_request = _send_streaming
    gw_client._test_active_ctx = active_ctx  # type: ignore[attr-defined]

    test_app = FastAPI()
    test_app.add_middleware(RequestMiddleware)
    test_app.include_router(compat_router)
    test_app.include_router(gemini_router)
    test_app.dependency_overrides[get_client] = lambda: gw_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
        headers={"x-anti-truncation": "false"},
    ) as http:
        yield http, gw_client, replies, sent_bodies

    hist = request_history()
    if hist._write_conn is not None:
        hist._write_conn.close()
        hist._write_conn = None


# ---------------------------------------------------------------------------
# P0: Account switch / dynamic prompt + echoed unsigned thought + FC -> SKIP
# ---------------------------------------------------------------------------


async def test_account_switch_with_echoed_thought_recovers_caching_and_isolates_thoughts(
    gateway_app,
):
    """After switching accounts (scope miss), an unobserved turn with unsigned
    echoed thoughts must preserve the exact-owner invariant (no fabricated SKIP
    alongside echoed thoughts), yet keep the history prefix compatible so newly
    generated turns on the new account are cached and replayed — while still
    isolating distinct echoed thoughts."""
    http, gw_client, replies, sent_bodies = gateway_app

    # Turn 1 on Account A: returns unsigned thought + signed functionCall
    replies.append(
        _wire_response(
            [
                AistudioPart(text="Planning step 1.", thought=True),
                AistudioPart(
                    function_call=("read_file", {"path": "a.py"}, "call_1"),
                    thought_signature=SIG_A,
                ),
            ]
        )
    )
    r1 = await http.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Inspect a.py"}],
        },
    )
    assert r1.status_code == 200

    # Switch to Account B (simulating 429/403 rotation) -> scope changes!
    gw_client._test_active_ctx[0] = SessionContext(  # type: ignore[attr-defined]
        auth_file="/data/account-b/auth.json",
        cookies={"SAPISID": "fixture-b"},
    )

    # Turn 2: client echoes unsigned thought + unsigned functionCall (stripped extra_content)
    replies.append(
        _wire_response(
            [
                AistudioPart(text="Planning step 2.", thought=True),
                AistudioPart(
                    function_call=("edit_file", {"path": "a.py"}, "call_2"),
                    thought_signature=SIG_B,
                ),
            ]
        )
    )
    turn1_history = [
        {"role": "user", "content": "Inspect a.py"},
        {
            "role": "assistant",
            "reasoning_content": "Planning step 1.",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "a.py"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "print('hi')"},
    ]
    r2 = await http.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": turn1_history},
    )
    assert r2.status_code == 200

    # Exact-owner invariant: unobserved turn with echoed thought is NOT fabricated with SKIP,
    # and its replay reason is recorded as unobserved_thought while caching succeeds for Turn 2.
    body2 = json.loads(sent_bodies[-1])
    model1_wire = [parse_response_part(w) for w in body2[TopLevelIndex.CONTENTS][1][0]]
    fc1 = [p for p in model1_wire if p.function_call is not None]
    assert len(fc1) == 1
    assert fc1[0].thought_signature == ""
    rec2 = request_history().get(r2.headers["x-request-id"])
    assert rec2 is not None
    sig2 = cast(dict[str, Any], rec2["thought_signature"])
    assert "unobserved_thought" in sig2["replay"]["miss_reasons"]
    assert sig2["upstream_in"]["cached_warm"] is True

    # Turn 3 on Account B: Turn 2 should now HIT Account B's warm cache and replay SIG_B!
    replies.append(_wire_response([AistudioPart(text="All done.")]))
    turn2_history = [
        *turn1_history,
        {
            "role": "assistant",
            "reasoning_content": "Planning step 2.",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({"path": "a.py"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
    ]
    r3 = await http.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": turn2_history},
    )
    assert r3.status_code == 200
    body3 = json.loads(sent_bodies[-1])
    model2_wire = [parse_response_part(w) for w in body3[TopLevelIndex.CONTENTS][3][0]]
    fc2 = [p for p in model2_wire if p.function_call is not None]
    assert len(fc2) == 1
    assert fc2[0].thought_signature == SIG_B

    # Tampering Turn 1's echoed thought text must isolate the prefix and NOT unlock SIG_B!
    replies.append(_wire_response([AistudioPart(text="Isolated.")]))
    tampered_history = [
        turn2_history[0],
        {**turn2_history[1], "reasoning_content": "Tampered step 1."},
        *turn2_history[2:],
    ]
    r4 = await http.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": tampered_history},
    )
    assert r4.status_code == 200
    body4 = json.loads(sent_bodies[-1])
    model2_tampered = [
        parse_response_part(w) for w in body4[TopLevelIndex.CONTENTS][3][0]
    ]
    fc2_tampered = [p for p in model2_tampered if p.function_call is not None]
    assert len(fc2_tampered) == 1
    assert fc2_tampered[0].thought_signature == ""


def test_unobserved_unsigned_thought_does_not_block_later_turn_skip_or_collide():
    """An earlier unobserved text+thought turn must not block SKIP on a later FC turn,
    and distinct unobserved thoughts must hash to distinct branch prefixes."""
    history = ModelTurnHistory()
    turn1_a = [
        AistudioContent("user", [AistudioPart(text="Hello")]),
        AistudioContent(
            "model",
            [
                AistudioPart(text="Thought branch A", thought=True),
                AistudioPart(text="Hi there."),
            ],
        ),
        AistudioContent("user", [AistudioPart(text="Run tool")]),
        AistudioContent(
            "model",
            [
                AistudioPart(function_call=("run", {"x": 1}, "c1")),
            ],
        ),
        AistudioContent(
            "user", [AistudioPart(function_response=("run", {"ok": True}, "c1"))]
        ),
    ]
    restored, token, ctx_a = history.prepare("scope-x", turn1_a)
    assert token is None
    assert ctx_a is not None
    fc_parts = [p for p in restored[3].parts if p.function_call is not None]
    assert fc_parts[0].thought_signature == SKIP_THOUGHT_SIGNATURE

    # Different thought text in turn 1 must yield a distinct prefix digest
    turn1_b = [
        turn1_a[0],
        AistudioContent(
            "model",
            [
                AistudioPart(text="Thought branch B", thought=True),
                AistudioPart(text="Hi there."),
            ],
        ),
        *turn1_a[2:],
    ]
    _, _, ctx_b = history.prepare("scope-x", turn1_b)
    assert ctx_b is not None
    assert ctx_a.prefix != ctx_b.prefix


# ---------------------------------------------------------------------------
# Responses API: include=["reasoning.encrypted_content"] roundtrip (Codex CLI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
async def test_responses_encrypted_content_opt_in_and_strict_sdk_roundtrip(
    gateway_app, stream: bool
):
    http, gw_client, replies, sent_bodies = gateway_app
    test_app = http._transport.app  # type: ignore[attr-defined]

    # 1. Without include: encrypted_content must NOT be emitted
    replies.append(
        _wire_response(
            [
                AistudioPart(
                    text="Internal reasoning", thought=True, thought_signature=SIG_A
                ),
                AistudioPart(text="Answer."),
            ]
        )
    )
    r_plain = await http.post(
        "/v1/responses",
        json={"model": MODEL, "stream": False, "input": "Plain question"},
    )
    assert r_plain.status_code == 200
    reasoning_plain = next(
        item for item in r_plain.json()["output"] if item["type"] == "reasoning"
    )
    assert "encrypted_content" not in reasoning_plain

    # 2. With include=["reasoning.encrypted_content"]: emits standard field & passes strict SDK
    replies.append(
        _wire_response(
            [
                AistudioPart(text="Deep check", thought=True, thought_signature=SIG_A),
                AistudioPart(text="Verified."),
            ]
        )
    )
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1",
        api_key="fixture",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(test_app)),
        default_headers={"x-anti-truncation": "false"},
        _strict_response_validation=True,
        max_retries=0,
    ) as sdk:
        if stream:
            final_response = None
            async with sdk.responses.stream(
                model=MODEL,
                input="Strict question",
                include=["reasoning.encrypted_content"],
            ) as s:
                async for _ in s:
                    pass
                final_response = await s.get_final_response()
            assert final_response is not None
            output_items = [
                item.model_dump(exclude_none=True) for item in final_response.output
            ]
        else:
            resp = await sdk.responses.create(
                model=MODEL,
                input="Strict question",
                include=["reasoning.encrypted_content"],
            )
            output_items = [item.model_dump(exclude_none=True) for item in resp.output]

    reasoning_item = next(item for item in output_items if item["type"] == "reasoning")
    enc = reasoning_item.get("encrypted_content")
    assert isinstance(enc, str) and enc
    assert decode_signature_envelope(enc) == SIG_A

    # 3. Stateless Codex CLI replay: strip extra_content & clear warm cache, keep ONLY encrypted_content
    reasoning_only_enc = {
        "id": reasoning_item["id"],
        "type": "reasoning",
        "summary": reasoning_item["summary"],
        "encrypted_content": enc,
    }
    msg_item = next(item for item in output_items if item["type"] == "message")
    gw_client._model_turns.clear()

    replies.append(_wire_response([AistudioPart(text="Follow-up ok.")]))
    r_next = await http.post(
        "/v1/responses",
        json={
            "model": MODEL,
            "input": cast(
                ResponseInputParam,
                [
                    {"role": "user", "content": "Strict question"},
                    reasoning_only_enc,
                    msg_item,
                    {"role": "user", "content": "Continue"},
                ],
            ),
        },
    )
    assert r_next.status_code == 200
    body_next = json.loads(sent_bodies[-1])
    replayed_parts = [
        parse_response_part(w) for w in body_next[TopLevelIndex.CONTENTS][1][0]
    ]
    assert replayed_parts[0].thought is True
    assert replayed_parts[0].thought_signature == SIG_A


async def test_responses_foreign_encrypted_content_is_safely_ignored(gateway_app):
    http, _gw_client, replies, sent_bodies = gateway_app
    replies.append(_wire_response([AistudioPart(text="Handled safely.")]))

    foreign_blob = base64.b64encode(b"gAAAAAB_openai_native_encrypted_blob").decode()
    resp = await http.post(
        "/v1/responses",
        json={
            "model": MODEL,
            "input": [
                {"role": "user", "content": "Hi"},
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Foreign thought"}],
                    "encrypted_content": foreign_blob,
                },
                {"role": "user", "content": "Next"},
            ],
        },
    )
    assert resp.status_code == 200
    body = json.loads(sent_bodies[-1])
    replayed_parts = [
        parse_response_part(w) for w in body[TopLevelIndex.CONTENTS][1][0]
    ]
    assert replayed_parts[0].thought is True
    assert not replayed_parts[0].thought_signature

    rec = request_history().get(resp.headers["x-request-id"])
    assert rec is not None
    sig = cast(dict[str, Any], rec["thought_signature"])
    assert sig["client_in"]["dropped"] == 1
    assert any(
        item.get("note") == "foreign_encrypted_content_ignored"
        for item in sig["client_in"]["items"]
    )


# ---------------------------------------------------------------------------
# OpenAI Chat Completions: reasoning_details, provider_specific_fields, aliases
# ---------------------------------------------------------------------------


async def test_openai_chat_accepts_reasoning_details_and_provider_aliases():
    ctx_token = current_request.set(
        RequestContext("req_chat_alias", category="model", protocol="openai")
    )
    try:
        foreign_blob = base64.b64encode(b"anthropic-or-openai-blob").decode()
        req = OpenAIChatCompletionRequest.model_validate(
            {
                "model": MODEL,
                "messages": [
                    {"role": "user", "content": "Run checks"},
                    {
                        "role": "assistant",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "Step 1"},
                            {
                                "type": "reasoning.encrypted",
                                "id": "call_rd",
                                "data": SIG_A,
                                "format": "google-gemini-v1",
                            },
                            {
                                "type": "reasoning.encrypted",
                                "data": foreign_blob,
                                "format": "openai-responses-v1",
                            },
                        ],
                        "tool_calls": [
                            {
                                "id": "call_rd",
                                "type": "function",
                                "function": {"name": "search", "arguments": "{}"},
                            },
                            {
                                "id": "call_pf",
                                "type": "function",
                                "provider_specific_fields": {
                                    "thought_signature": SIG_B
                                },
                                "function": {"name": "fetch", "arguments": "{}"},
                            },
                            {
                                "id": "call_fn",
                                "type": "function",
                                "function": {
                                    "name": "save",
                                    "arguments": "{}",
                                    "thought_signature": SIG_A,
                                },
                            },
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_rd", "content": "r1"},
                    {"role": "tool", "tool_call_id": "call_pf", "content": "r2"},
                    {"role": "tool", "tool_call_id": "call_fn", "content": "r3"},
                ],
            }
        )
        gemini_req, resolved = await openai_to_gemini_request(req)
        normalized = normalize_gemini_request(gemini_req, resolved)
        model_parts = normalized.contents[1].parts

        assert model_parts[0].thought is True
        assert model_parts[0].text == "Step 1"
        fc_parts = [p for p in model_parts if p.function_call is not None]
        assert [p.thought_signature for p in fc_parts] == [SIG_A, SIG_B, SIG_A]

        trace = get_signature_trace()
        assert trace is not None
        assert trace.client_dropped == 1
        assert "client_reasoning_details" in trace.client_sources
        assert "client_provider_fields" in trace.client_sources
        assert "client_tool_function" in trace.client_sources
    finally:
        current_request.reset(ctx_token)


# ---------------------------------------------------------------------------
# Wire Base64 normalization consistency for < 16 char thought signatures
# ---------------------------------------------------------------------------


def test_short_urlsafe_unpadded_thought_signature_normalizes_consistently():
    raw = b"\xfb\xff\xfe\x01"
    short_urlsafe = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    expected_standard = base64.b64encode(raw).decode()
    assert len(short_urlsafe) < 16
    assert valid_thought_signature(short_urlsafe)

    wire = AistudioPart(
        function_call=("ping", {}), thought_signature=short_urlsafe
    ).to_wire()
    assert wire[PartIndex.THOUGHT_SIGNATURE] == expected_standard


def test_signature_envelope_roundtrip_and_rejection():
    encoded = encode_signature_envelope(SIG_A)
    assert decode_signature_envelope(encoded) == SIG_A
    assert decode_signature_envelope("not-base64!!!") is None
    assert decode_signature_envelope(base64.b64encode(b'{"other": 1}').decode()) is None
