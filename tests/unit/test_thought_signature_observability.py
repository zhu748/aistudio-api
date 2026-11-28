"""End-to-end observability tests for thoughtSignature tracing and log filtering."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
import test_ordered_tool_routes as wire_routes
from fastapi import FastAPI
from starlette.requests import Request

from aistudio_api.api.app import app as _app  # noqa: F401 # pyright: ignore[reportUnusedImport]
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.routes_compat import router as compat_router
from aistudio_api.api.routes_gemini import router as gemini_router
from aistudio_api.api.routes_logs import get_runtime_logs, stream_runtime_logs
from aistudio_api.application import api_service_gemini
from aistudio_api.application.compat_shared import encode_signature_envelope
from aistudio_api.config import settings
from aistudio_api.domain import tool_signatures
from aistudio_api.domain.models import ResponsePart
from aistudio_api.domain.tool_signatures import SKIP_THOUGHT_SIGNATURE
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart
from aistudio_api.infrastructure.request_history import request_history
from aistudio_api.infrastructure.signature_trace import (
    ThoughtSignatureTrace,
    get_signature_trace,
    signature_fingerprint,
)
from aistudio_api.infrastructure.utils import log_files, logger as log_module
from aistudio_api.infrastructure.utils.logger import (
    _mask_json_value,
    clear_recent_logs,
    get_recent_logs,
)

MODEL = "gemini-3.8-flash"
SIG_A = base64.b64encode(b"observability-fixture-sig-alpha").decode()
SIG_B = base64.b64encode(b"observability-fixture-sig-beta").decode()
TOKEN = "v1_" + "t" * 24


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


def _make_stream_request() -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/logs/stream",
        "raw_path": b"/api/logs/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 54321),
        "server": ("test", 80),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": "", "more_body": False}

    return Request(scope, receive=receive)


@pytest.fixture
async def observed_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AsyncIterator[tuple[httpx.AsyncClient, AIStudioClient, list[bytes]]]:
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
    clear_recent_logs()

    replies: list[bytes] = []
    gw_client = AIStudioClient()
    ctx = SessionContext(
        auth_file="/data/acc-alpha/auth.json",
        cookies={"SAPISID": "fixture"},
    )
    gw_client.session.ensure_ready = AsyncMock(return_value=ctx)
    gw_client.session.generate_snapshot = AsyncMock(return_value="!proof")

    async def _send_request(**_kwargs: object) -> tuple[int, bytes]:
        return 200, replies.pop(0)

    async def _send_streaming(**_kwargs: object):
        raw = replies.pop(0)
        yield "status", 200
        yield "chunk", raw

    gw_client.session.send_request = AsyncMock(side_effect=_send_request)
    gw_client.session.send_streaming_request = _send_streaming

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
        yield http, gw_client, replies

    hist = request_history()
    if hist._write_conn is not None:
        hist._write_conn.close()
        hist._write_conn = None


def test_fingerprint_canonicalizes_base64_variants_and_skip():
    raw = b"canonical-fingerprint-bytes-for-test-\xfb\xff\xfe"
    std_padded = base64.b64encode(raw).decode()
    std_unpadded = std_padded.rstrip("=")
    url_padded = base64.urlsafe_b64encode(raw).decode()
    url_unpadded = url_padded.rstrip("=")

    fp = signature_fingerprint(std_padded)
    assert len(fp) == 8
    assert signature_fingerprint(std_unpadded) == fp
    assert signature_fingerprint(url_padded) == fp
    assert signature_fingerprint(url_unpadded) == fp
    assert signature_fingerprint(SKIP_THOUGHT_SIGNATURE) == "SKIP"


def test_trace_payload_is_never_corrupted_by_sensitive_key_masking():
    trace = ThoughtSignatureTrace(protocol="claude", attempt=1)
    trace.record_client_part(
        carrier="function_call",
        raw_signature=SIG_A,
        source="client_extra_content",
        call_id="toolu_1",
        call_name="lookup",
        turn_index=1,
    )
    trace.record_replay_turn(
        turn_index=1,
        action="warm_hit",
        fc_count=1,
        restored_sigs=1,
        token_reused=True,
    )
    trace.record_cache_store(cached_warm=True, cached_cold=True, status="stored")
    trace.record_client_out_sig(
        carrier="function_call",
        wire_field="extra_content",
        signature=SIG_B,
        call_id="toolu_2",
        call_name="finish",
    )
    raw = trace.to_dict()
    masked = _mask_json_value(raw)
    assert masked == raw
    assert "***" not in json.dumps(masked)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"call_id": "normalized", "id": "legacy"}, "normalized"),
        ({"id": "legacy"}, "legacy"),
        ({"call_id": "", "id": "legacy"}, "legacy"),
        ({"call_id": {"not": "an id"}, "id": "legacy"}, "legacy"),
        ({"call_id": "x" * 80}, "x" * 64),
        ({"call_id": [], "id": {"private": "payload"}}, None),
        ({}, None),
    ],
)
def test_upstream_signature_item_reads_only_nonempty_string_ids(fields, expected):
    trace = ThoughtSignatureTrace()
    trace.record_upstream_in_parts(
        [
            ResponsePart(
                function_call={"name": "measure", "raw": ["private body"], **fields},
                thought_signature=SIG_A,
            )
        ]
    )
    assert trace.upstream_in_items == [
        {
            "carrier": "function_call",
            "kind": "real",
            "source": "upstream_wire",
            "call_name": "measure",
            "fp": signature_fingerprint(SIG_A),
            "head": SIG_A[:12],
            "len": len(SIG_A),
            **({"call_id": expected} if expected is not None else {}),
        }
    ]


@pytest.mark.parametrize("protocol", wire_routes.PROTOCOLS)
@pytest.mark.parametrize("interrupted", [False, True], ids=["parallel_calls", "cut"])
async def test_stream_history_matches_observed_signatures(
    observed_app, monkeypatch, protocol, interrupted
):
    http, gateway, _ = observed_app
    # Empty session, in-process wire fixture, no signer or provider.
    monkeypatch.setattr(
        gateway.session, "ensure_ready", AsyncMock(return_value=SessionContext())
    )
    parts = (
        [
            wire_routes.text("private reasoning", thought=True, signature=SIG_A),
            wire_routes.text("visible text"),
        ]
        if interrupted
        else [
            wire_routes.call("measure", {"private": "input"}, "left", signature=SIG_A),
            wire_routes.call("measure", {"private": "input"}, "right", signature=SIG_B),
        ]
    )
    live = []

    async def send_streaming(**_kwargs):
        trace = get_signature_trace()
        assert trace is not None
        live.append(trace)
        yield "status", 200
        yield (
            "chunk",
            wire_routes.wire(wire_routes.chunk(parts, None if interrupted else 1)),
        )
        if interrupted:
            yield "error", "fixture stream interrupted"

    monkeypatch.setattr(gateway.session, "send_streaming_request", send_streaming)
    path, payload = wire_routes.input_request(protocol, stream=True)
    response = await http.post(path, json=payload)
    assert response.status_code == 200
    req_id = response.headers["x-request-id"]
    row = request_history().get(req_id)
    assert row is not None and row["status_code"] == 200
    assert row["result"] == ("error" if interrupted else "success")
    (observed,) = live
    saved = cast(dict[str, Any], row["thought_signature"])
    assert saved == observed.to_dict()
    upstream, client = saved["upstream_in"], saved["client_out"]
    assert all(secret not in json.dumps(saved) for secret in (SIG_A, SIG_B, "private"))
    logs = get_recent_logs(category="thought_signature", request_id=req_id, order="asc")
    directions = [
        meta["direction"]
        for log in logs
        if isinstance(meta := log.get("sig_meta"), dict)
    ]
    if interrupted:
        # Chat defers the signature to a success-only finish delta. Other adapters
        # already emitted it; persisting the observation must not synthesize a send.
        emitted = protocol != "chat"
        carrier = encode_signature_envelope(SIG_A) if protocol == "claude" else SIG_A
        assert (carrier in response.text) == emitted
        assert "fixture stream interrupted" in response.text
        events = wire_routes.sse_events(response.text)
        if protocol == "responses":
            assert events[-1]["type"] == "response.failed"
        else:
            assert "error" in events[-1]
        assert upstream["total_sigs"] == 1 and client["total_sigs"] == int(emitted)
        assert upstream["fingerprints"] == [signature_fingerprint(SIG_A)]
        assert client["fingerprints"] == (upstream["fingerprints"] if emitted else [])
        assert directions == ["outbound"]
    else:
        assert SIG_A in response.text and SIG_B in response.text
        for stage in (upstream, client):
            assert stage["total_sigs"] == 2
            assert [
                (i["call_name"], i.get("call_id"), i["fp"]) for i in stage["items"]
            ] == [
                ("measure", "left", signature_fingerprint(SIG_A)),
                ("measure", "right", signature_fingerprint(SIG_B)),
            ]
        assert directions == ["outbound", "inbound"]


@pytest.mark.parametrize("protocol", ["gemini", "openai", "responses", "claude"])
@pytest.mark.parametrize("stream", [False, True])
async def test_full_signature_lifecycle_persisted_and_logged_across_protocols(
    observed_app, protocol: str, stream: bool
):
    http, _gw_client, replies = observed_app
    fp_a = signature_fingerprint(SIG_A)

    # Turn 1: upstream returns a signed functionCall + trailing empty signed carrier
    replies.append(
        _wire_response(
            [
                AistudioPart(text="Thinking...", thought=True),
                AistudioPart(
                    function_call=("lookup", {"city": "Paris"}, "call_1"),
                    thought_signature=SIG_A,
                ),
            ]
        )
    )

    gemini_path = (
        f"/v1beta/models/{MODEL}:streamGenerateContent"
        if stream
        else f"/v1beta/models/{MODEL}:generateContent"
    )
    if protocol == "gemini":
        r1 = await http.post(
            gemini_path,
            json={"contents": [{"role": "user", "parts": [{"text": "Check Paris"}]}]},
        )
    elif protocol == "openai":
        r1 = await http.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "stream": stream,
                "messages": [{"role": "user", "content": "Check Paris"}],
            },
        )
    elif protocol == "responses":
        r1 = await http.post(
            "/v1/responses",
            json={"model": MODEL, "stream": stream, "input": "Check Paris"},
        )
    else:
        r1 = await http.post(
            "/v1/messages",
            json={
                "model": MODEL,
                "max_tokens": 256,
                "stream": stream,
                "messages": [{"role": "user", "content": "Check Paris"}],
            },
        )

    assert r1.status_code == 200, r1.text
    req1_id = r1.headers["x-request-id"]
    rec1 = request_history().get(req1_id)
    assert rec1 is not None
    sig1 = rec1.get("thought_signature")
    assert isinstance(sig1, dict)
    assert sig1["protocol"] == protocol
    assert sig1["upstream_in"]["total_sigs"] == 1
    assert sig1["upstream_in"]["by_carrier"] == {"function_call": 1}
    assert sig1["upstream_in"]["cached_warm"] is True
    assert sig1["upstream_in"]["cached_cold"] is True
    assert fp_a in sig1["upstream_in"]["fingerprints"]
    assert sig1["client_out"]["total_sigs"] == 1
    assert fp_a in sig1["client_out"]["fingerprints"]
    for stage in ("upstream_in", "client_out"):
        assert [item["call_id"] for item in sig1[stage]["items"]] == ["call_1"]

    # Turn 2: client strips the signature, gateway restores via warm cache
    replies.append(
        _wire_response(
            [
                AistudioPart(text="Done."),
                AistudioPart(text="", thought_signature=SIG_B),
            ]
        )
    )
    if protocol == "gemini":
        r2 = await http.post(
            gemini_path,
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Check Paris"}]},
                    {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "call_1",
                                    "name": "lookup",
                                    "args": {"city": "Paris"},
                                }
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": "call_1",
                                    "name": "lookup",
                                    "response": {"result": "20C"},
                                }
                            }
                        ],
                    },
                ]
            },
        )
    elif protocol == "openai":
        r2 = await http.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "stream": stream,
                "messages": [
                    {"role": "user", "content": "Check Paris"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": json.dumps({"city": "Paris"}),
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "20C"},
                ],
            },
        )
    elif protocol == "responses":
        r2 = await http.post(
            "/v1/responses",
            json={
                "model": MODEL,
                "stream": stream,
                "input": [
                    {"role": "user", "content": "Check Paris"},
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": json.dumps({"city": "Paris"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "20C",
                    },
                ],
            },
        )
    else:
        r2 = await http.post(
            "/v1/messages",
            json={
                "model": MODEL,
                "max_tokens": 256,
                "stream": stream,
                "messages": [
                    {"role": "user", "content": "Check Paris"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "lookup",
                                "input": {"city": "Paris"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": "20C",
                            }
                        ],
                    },
                ],
            },
        )

    assert r2.status_code == 200, r2.text
    req2_id = r2.headers["x-request-id"]
    rec2 = request_history().get(req2_id)
    assert rec2 is not None
    sig2 = cast(dict[str, Any], rec2["thought_signature"])
    assert sig2["client_in"]["fc_total"] == 1
    assert sig2["client_in"]["fc_signed"] == 0
    assert sig2["client_in"]["fc_missing"] == 1
    assert sig2["client_in"]["fc_unsigned_lead_turns"] == 1
    assert sig2["replay"]["warm_hits"] == 1
    assert sig2["replay"]["continuation_reused"] is True
    assert sig2["upstream_out"]["real_sigs"] == 1
    assert sig2["upstream_out"]["fc_unsigned"] == 0
    assert fp_a in sig2["upstream_out"]["fingerprints"]

    # Compat protocols cannot emit trailing empty carrier; native Gemini emits it
    if protocol == "gemini":
        assert sig2["client_out"]["total_sigs"] == 1
        assert sig2["client_out"]["dropped"] == 0
    else:
        assert sig2["client_out"]["total_sigs"] == 0
        assert sig2["client_out"]["dropped"] == 1
        assert "tail_empty_unrepresentable" in sig2["client_out"]["drop_reasons"]

    # Runtime logs with category=thought_signature return ONLY thoughtSignature logs
    logs_resp = await get_runtime_logs(
        category="thought_signature", search=fp_a, level="INFO"
    )
    entries = logs_resp["logs"]
    assert isinstance(entries, list)
    assert len(entries) >= 2
    assert all(e["name"] == "aistudio.thought_signature" for e in entries)
    directions = {e["sig_meta"]["direction"] for e in entries if "sig_meta" in e}
    assert directions == {"inbound", "outbound"}


async def test_native_signed_parallel_history_replays_without_missing_lead(
    observed_app,
):
    http, _, replies = observed_app
    ids = ("call_1234567890123001", "call_1234567890123002", "call_1234567890123003")
    replies.append(
        _wire_response(
            [
                AistudioPart(text="Checking three cities.", thought=True),
                *[
                    AistudioPart(
                        function_call=("lookup", {"n": index}, call_id),
                        thought_signature=SIG_A if index == 0 else None,
                    )
                    for index, call_id in enumerate(ids)
                ],
            ]
        )
    )
    path = f"/v1beta/models/{MODEL}:generateContent"
    user = {"role": "user", "parts": [{"text": "Check three cities."}]}
    first = await http.post(path, json={"contents": [user]})
    assert first.status_code == 200
    echoed = first.json()["candidates"][0]["content"]
    replies.append(_wire_response([AistudioPart(text="Done.")]))
    second = await http.post(
        path,
        json={
            "contents": [
                user,
                echoed,
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "lookup",
                                "id": call_id,
                                "response": {"ok": True},
                            }
                        }
                        for call_id in ids
                    ],
                },
            ]
        },
    )
    assert second.status_code == 200
    request = request_history().get(second.headers["x-request-id"])
    assert request is not None
    trace = cast(dict[str, Any], request["thought_signature"])
    client_in, upstream_out = trace["client_in"], trace["upstream_out"]
    assert (client_in["fc_signed"], client_in["fc_total"]) == (1, 3)
    assert (client_in["thought_signed"], client_in["thought_total"]) == (0, 1)
    assert client_in["fc_missing"] == upstream_out["fc_unsigned"] == 2
    assert (
        client_in["fc_unsigned_lead_turns"]
        == upstream_out["fc_unsigned_lead_turns"]
        == 0
    )
    inbound_calls, outbound_calls = [
        [item for item in stage["items"] if item["carrier"] == "function_call"]
        for stage in (client_in, upstream_out)
    ]
    for entries in (inbound_calls, outbound_calls):
        assert [entry["lead_call"] for entry in entries] == [True, False, False]
        assert [entry["call_id"] for entry in entries] == list(ids)
    assert inbound_calls[0]["fp"] == outbound_calls[0]["fp"]
    assert all(entry["source"] == "warm_cache" for entry in outbound_calls)
    assert trace["replay"]["warm_hits"] == 1
    assert trace["replay"]["skip_fills"] == 0
    assert upstream_out["continuation_sent"] is True


async def test_logs_stream_and_history_filter_thought_signature_category(
    observed_app, tmp_path
):
    http, _gw_client, replies = observed_app
    fp_a = signature_fingerprint(SIG_A)

    log_files.start_file_logging()
    try:
        replies.append(
            _wire_response(
                [
                    AistudioPart(
                        function_call=("lookup", {"city": "Tokyo"}, "call_t"),
                        thought_signature=SIG_A,
                    )
                ]
            )
        )
        resp = await http.post(
            "/v1/messages",
            json={
                "model": MODEL,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Check Tokyo"}],
            },
        )
        assert resp.status_code == 200
        req_id = resp.headers["x-request-id"]

        # Verify SSE /api/logs/stream?category=thought_signature snapshot
        stream_resp = await stream_runtime_logs(
            _make_stream_request(),
            level="INFO",
            category="thought_signature",
            request_id=req_id,
        )
        gen = cast(AsyncGenerator[object, None], stream_resp.body_iterator)
        try:
            ready_raw = await gen.__anext__()
            assert "event: ready" in str(ready_raw)
            snap_raw = await gen.__anext__()
            assert "event: snapshot" in str(snap_raw)
            data_line = next(
                line[5:].strip()
                for line in str(snap_raw).splitlines()
                if line.startswith("data:")
            )
            snap = json.loads(data_line)
            assert snap["count"] == 2
            assert all(
                item["name"] == "aistudio.thought_signature" for item in snap["logs"]
            )
            assert any(
                fp_a in item["sig_meta"]["fingerprints"] for item in snap["logs"]
            )
        finally:
            await gen.aclose()
    finally:
        log_files.stop_file_logging()

    # Verify JSONL archived history filtering preserves sig_meta
    history = log_files.read_log_history(
        category="thought_signature",
        request_id=req_id,
        level="INFO",
    )
    items = history["items"]
    assert isinstance(items, list)
    assert len(items) == 2
    assert all(item["name"] == "aistudio.thought_signature" for item in items)
    assert all(isinstance(item.get("sig_meta"), dict) for item in items)

    # Also verify ordinary model logs still include thought_signature entries
    model_logs = get_recent_logs(category="model", request_id=req_id, min_level="INFO")
    assert any(e["name"] == "aistudio.thought_signature" for e in model_logs)
