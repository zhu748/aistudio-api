"""Public route regression tests with a hermetic, byte-fragmented wire source."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import api_service_gemini
from aistudio_api.domain.tool_signatures import SKIP_THOUGHT_SIGNATURE
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_spec import PartIndex, TopLevelIndex

PROTOCOLS = ("native", "chat", "claude", "responses")
MODEL = "gemini-3.8-flash"


class WireSource:
    raw = b""
    pause: asyncio.Event | None = None
    chunk_boundary: int = 0

    async def send_request(self, **kwargs):
        return 200, self.raw

    async def send_streaming_request(self, **kwargs):
        yield "status", 200
        pieces = (
            (self.raw[: self.chunk_boundary], self.raw[self.chunk_boundary :])
            if self.pause is not None
            else (self.raw,)
        )
        for index, piece in enumerate(pieces):
            if index and self.pause is not None:
                await self.pause.wait()
            for offset in range(0, len(piece), 11):
                yield "chunk", piece[offset : offset + 11]


@pytest.fixture
def wire_source(monkeypatch):
    source = WireSource()
    gateway = cast(Any, AIStudioClient.__new__(AIStudioClient))
    gateway._session = source
    gateway._streaming_gateway = StreamingGateway(cast(Any, source))
    gateway.prepare_request = AsyncMock(
        return_value=PreparedRequest(
            url="https://fixture.invalid", headers={}, body="[]"
        )
    )
    monkeypatch.setitem(app.dependency_overrides, get_client, lambda: gateway)
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    return source


def wire(*chunks):
    return json.dumps([chunks], ensure_ascii=False).encode()


def chunk(parts=(), finish=None):
    return [[[[list(parts), "model"], finish]]]


def text(text_value, *, thought=False, signature=None):
    part = [thought or None, text_value]
    if signature:
        part += [None] * 12 + [signature]
    return part


def call(name, args=None, call_id=None, *, signature=None):
    part = [None, None, None, [name, args, call_id]]
    if signature:
        part += [None] * 10 + [signature]
    return part


def input_request(protocol, stream):
    messages = [{"role": "user", "content": "Look up a city."}]
    params = {"model": MODEL, "stream": stream}
    if protocol == "native":
        action = "streamGenerateContent" if stream else "generateContent"
        return f"/v1beta/models/{MODEL}:{action}", {
            "contents": [{"role": "user", "parts": [{"text": "Look up a city."}]}]
        }
    if protocol == "chat":
        return "/v1/chat/completions", {**params, "messages": messages}
    if protocol == "claude":
        return "/v1/messages", {**params, "messages": messages, "max_tokens": 128}
    return "/v1/responses", {**params, "input": "Look up a city."}


def sse_events(body):
    return [
        json.loads(line[6:])
        for line in body.split("\n")
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


async def request(protocol, stream, *, stop=None):
    path, params = input_request(protocol, stream)
    if stop is not None:
        params["stop" if protocol == "chat" else "stop_sequences"] = [stop]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path, json=params, headers={"X-Anti-Truncation": "0"}
        )
    assert response.status_code == 200, response.text
    return sse_events(response.text) if stream else [response.json()]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("statuses", [(200,), (400, 200), (400, 400), (400,)])
async def test_native_resumes_history_after_corrupted_external_signature(
    monkeypatch, stream, statuses
):
    gateway = AIStudioClient()
    snapshot = AsyncMock(side_effect=["!first", "!retry", "!continued"])
    monkeypatch.setitem(app.dependency_overrides, get_client, lambda: gateway)
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(
        gateway.session, "ensure_ready", AsyncMock(return_value=SessionContext())
    )
    monkeypatch.setattr(gateway.session, "generate_snapshot", snapshot)
    monkeypatch.setattr(
        gateway.session, "schedule_browser_counttokens_followup", lambda *_: None
    )
    sent = []
    new_signature = "bmV3LWdhdGV3YXktc2lnbmF0dXJl"
    accepted = wire(chunk([text("History retained.", signature=new_signature)], 1))

    async def send_request(*, body, **_kwargs):
        index = len(sent)
        sent.append(json.loads(body))
        status = statuses[index] if index < len(statuses) else 200
        if status == 200:
            return status, accepted
        if statuses == (400,):
            return status, b'[,[3,"Invalid request."]]'
        return status, b'[,[3,"Corrupted thought signature."]]'

    async def send_streaming_request(**kwargs):
        status, raw = await send_request(**kwargs)
        yield "status", status
        yield "chunk", raw

    monkeypatch.setattr(gateway.session, "send_request", send_request)
    monkeypatch.setattr(
        gateway.session, "send_streaming_request", send_streaming_request
    )
    path, params = input_request("native", stream)
    signature = "ZXh0ZXJuYWwtZml4dHVyZS1zaWduYXR1cmU="
    params["contents"].extend(
        [
            {
                "role": "model",
                "parts": [
                    {
                        "text": "Check both cities.",
                        "thought": True,
                        "thoughtSignature": signature,
                    },
                    {
                        "functionCall": {"name": "weather", "id": "call-a", "args": {}},
                        "thoughtSignature": signature,
                    },
                    {"functionCall": {"name": "weather", "id": "call-b", "args": {}}},
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "weather",
                            "id": call_id,
                            "response": {"city": city},
                        },
                    }
                    for call_id, city in (("call-a", "Paris"), ("call-b", "London"))
                ],
            },
            # A fallback model continued the conversation and produced its own
            # tool batch. The client echoes it together with the older signatures.
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "name": "weather",
                            "id": "call-fallback",
                            "args": {"city": "Rome"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "weather",
                            "id": "call-fallback",
                            "response": {"city": "Rome"},
                        }
                    }
                ],
            },
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path, json=params, headers={"X-Anti-Truncation": "0"}
        )
        events = sse_events(response.text) if stream else [response.json()]
        assert len(sent) == len(statuses)
        original = sent[0][TopLevelIndex.CONTENTS]
        assert original[1][0][1][PartIndex.THOUGHT_SIGNATURE] == signature
        assert original[3][0][0][PartIndex.THOUGHT_SIGNATURE] == SKIP_THOUGHT_SIGNATURE
        if statuses[-1] == 200:
            assert native_parts(events) == [
                {"text": "History retained.", "thoughtSignature": new_signature}
            ]
        else:
            assert events[-1]["error"]["code"] == 400
        if len(statuses) == 2:
            retried = sent[1][TopLevelIndex.CONTENTS]
            assert retried[0] == original[0]
            assert retried[2:] == original[2:]
            migrated_thought = retried[1][0][0]
            assert len(migrated_thought) <= PartIndex.THOUGHT_SIGNATURE
            assert migrated_thought == original[1][0][0][: len(migrated_thought)]
            assert (
                retried[1][0][1][: PartIndex.THOUGHT_SIGNATURE]
                == original[1][0][1][: PartIndex.THOUGHT_SIGNATURE]
            )
            assert (
                retried[1][0][1][PartIndex.THOUGHT_SIGNATURE] == SKIP_THOUGHT_SIGNATURE
            )
            assert retried[1][0][2] == original[1][0][2]
            assert (
                snapshot.await_args_list[0].args[0]
                != snapshot.await_args_list[1].args[0]
            )

        if statuses == (400, 200):
            # DSH keeps echoing its original local history. Reuse the accepted
            # migration instead of incurring another failed upstream attempt.
            params["contents"].extend(
                [
                    {"role": "model", "parts": [{"text": "History retained."}]},
                    {"role": "user", "parts": [{"text": "Continue."}]},
                ]
            )
            await client.post(path, json=params, headers={"X-Anti-Truncation": "0"})
            assert len(sent) == 3
            continued = sent[2][TopLevelIndex.CONTENTS]
            migrated_history = sent[1][TopLevelIndex.CONTENTS]
            assert continued[: len(migrated_history)] == migrated_history
            next_turn = continued[len(migrated_history)]
            assert next_turn[0][0][PartIndex.THOUGHT_SIGNATURE] == new_signature


def native_parts(events):
    return [
        p
        for e in events
        for c in e.get("candidates", [])
        for p in c["content"]["parts"]
    ]


def tool_calls(protocol, stream, events):
    if protocol == "native":
        return [p["functionCall"] for p in native_parts(events) if "functionCall" in p]
    if protocol == "chat":
        items = [
            tc
            for e in events
            for c in e.get("choices", [])
            for tc in c.get("delta" if stream else "message", {}).get("tool_calls", [])
        ]
        return [
            {
                "id": item["id"],
                "name": item["function"]["name"],
                "args": json.loads(item["function"]["arguments"]),
            }
            for item in items
        ]
    if protocol == "claude":
        if stream:
            blocks = {
                e["index"]: dict(e["content_block"])
                for e in events
                if e.get("type") == "content_block_start"
                and e["content_block"]["type"] == "tool_use"
            }
            for e in events:
                if e.get("delta", {}).get("type") == "input_json_delta":
                    blocks[e["index"]]["input"] = json.loads(e["delta"]["partial_json"])
        else:
            blocks = {
                i: b
                for i, b in enumerate(events[0]["content"])
                if b["type"] == "tool_use"
            }
        return [
            {"id": b["id"], "name": b["name"], "args": b["input"]}
            for b in blocks.values()
        ]
    result = events[-1]["response"] if stream else events[0]
    return [
        {"id": p["call_id"], "name": p["name"], "args": json.loads(p["arguments"])}
        for p in result["output"]
        if p["type"] == "function_call"
    ]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
async def test_wire_parallel_mixed_ids_and_sparse_replay(wire_source, protocol, stream):
    wire_source.raw = wire(
        chunk([call("weather", '{"city":"Pa'), call("time", '{"zone":"U')]),
        chunk([call("time", 'TC"}', "B"), call("weather", 'ris"}', "A")]),
        chunk([call(None, {"city": "Paris"}, "A"), call(None, None, "A")], 1),
    )
    events = await request(protocol, stream)
    assert tool_calls(protocol, stream, events) == [
        {"id": "A", "name": "weather", "args": {"city": "Paris"}},
        {"id": "B", "name": "time", "args": {"zone": "UTC"}},
    ]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("finish", [None, 2, 3])
async def test_wire_delayed_or_missing_terminal_never_releases_calls(
    wire_source, protocol, stream, finish
):
    wire_source.raw = wire(
        chunk([call("weather", {"city": "Sapporo"}, "A")]),
        chunk([text("partial answer")]),
        chunk([], finish),
    )
    events = await request(protocol, stream)
    assert tool_calls(protocol, stream, events) == []
    assert "partial answer" in str(events)
    if protocol == "native":
        assert [
            c["finishReason"]
            for e in events
            for c in e["candidates"]
            if "finishReason" in c
        ] == [{None: "OTHER", 2: "MAX_TOKENS", 3: "SAFETY"}[finish]]
    elif protocol == "responses":
        assert (events[-1]["response"] if stream else events[0])[
            "status"
        ] == "incomplete"
    elif protocol == "chat":
        assert events[-1]["choices"][0]["finish_reason"] == (
            "content_filter" if finish == 3 else "length"
        )
    else:
        stop_reason = (
            next(
                e["delta"]["stop_reason"]
                for e in events
                if e["type"] == "message_delta"
            )
            if stream
            else events[0]["stop_reason"]
        )
        assert stop_reason == ("refusal" if finish == 3 else "max_tokens")


@pytest.mark.parametrize("protocol", ["chat", "claude"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool_first", [False, True])
async def test_stop_boundary_obeys_original_wire_order(
    wire_source, protocol, stream, tool_first
):
    parts = [text("before <END> after"), call("weather", {"city": "Paris"}, "A")]
    wire_source.raw = wire(chunk(list(reversed(parts)) if tool_first else parts, 1))
    events = await request(protocol, stream, stop="<END>")
    assert len(tool_calls(protocol, stream, events)) == int(tool_first)
    assert "before " in str(events)
    assert "after" not in str(events)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
async def test_wire_signature_stays_on_its_owner_part(wire_source, protocol, stream):
    sig, detached, empty = (
        "c2lnLWZpcnN0",
        "bGl0ZXJhbC1jYXJyaWVy",
        "ZW1wdHktY2Fycmllcg==",
    )
    wire_source.raw = wire(
        chunk(
            [
                text("visible"),
                call("weather", {"city": "Paris"}, "A", signature=sig),
                call("time", {"zone": "UTC"}, "B"),
                text(None, signature=detached),
                text("", signature=empty),
            ],
            1,
        )
    )
    events = await request(protocol, stream)
    assert [tool["id"] for tool in tool_calls(protocol, stream, events)] == ["A", "B"]
    if protocol == "native":
        assert native_parts(events) == [
            {"text": "visible"},
            {
                "functionCall": {
                    "id": "A",
                    "name": "weather",
                    "args": {"city": "Paris"},
                },
                "thoughtSignature": sig,
            },
            {"functionCall": {"id": "B", "name": "time", "args": {"zone": "UTC"}}},
            {"thoughtSignature": detached},
            {"text": "", "thoughtSignature": empty},
        ]
    else:
        assert detached not in str(events) and empty not in str(events)
        # Responses repeats the signed item across SSE lifecycle events; no
        # other call's added/done/snapshot item may inherit the same value.
        encoded_siblings = [
            e for e in events if e.get("item", {}).get("call_id") == "B"
        ]
        for event in encoded_siblings:
            assert "extra_content" not in event["item"]
        if protocol != "responses" or not stream:
            assert str(events).count(sig) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_responses_keeps_distinct_ordered_runs(wire_source, stream):
    wire_source.raw = wire(
        chunk(
            [
                text("first", thought=True, signature="c2lnMQ=="),
                text("before"),
                call("weather", {"city": "Paris"}, "A"),
                text("second", thought=True, signature="c2lnMg=="),
                text("after"),
            ],
            1,
        )
    )
    events = await request("responses", stream)
    items = (events[-1]["response"] if stream else events[0])["output"]
    assert [i["type"] for i in items] == [
        "reasoning",
        "message",
        "function_call",
        "reasoning",
        "message",
    ]
    assert [items[i]["summary"][0]["text"] for i in (0, 3)] == ["first", "second"]
    assert [
        items[i]["extra_content"]["google"]["thought_signature"] for i in (0, 3)
    ] == ["c2lnMQ==", "c2lnMg=="]
    assert [items[i]["content"][0]["text"] for i in (1, 4)] == ["before", "after"]


@pytest.mark.parametrize("stream", [False, True])
async def test_native_preserves_code_and_function_result(wire_source, stream):
    result = [None, None, None, None, ["lookup", {"city": "Paris"}, "A"]]
    code = [None] * 8 + [[1, "print(4)"], [1, "FOUR"]]
    wire_source.raw = wire(chunk([text("before"), code, result, text("after")], 1))
    events = await request("native", stream)
    assert native_parts(events) == [
        {"text": "before"},
        {
            "executableCode": {"language": "PYTHON", "code": "print(4)"},
            "codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "FOUR"},
        },
        {
            "functionResponse": {
                "name": "lookup",
                "id": "A",
                "response": {"city": "Paris"},
            }
        },
        {"text": "after"},
    ]


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_compat_code_only_output_is_not_empty(wire_source, protocol, stream):
    wire_source.raw = wire(chunk([[None] * 9 + [[1, "FOUR"]]], 1))
    events = await request(protocol, stream)
    assert "FOUR" in str(events) and "空回诊断" not in str(events)


async def test_eof_repair_cannot_make_partially_closed_candidate_executable(
    wire_source,
):
    wire_source.raw = wire(chunk([call("weather", {"city": "Paris"}, "A")], 1))[:-4]
    events = await request("chat", True)
    assert not tool_calls("chat", True, events)
    assert events[-1]["choices"][0]["finish_reason"] == "length"


async def test_leading_text_is_live_while_calls_wait_for_terminal(
    wire_source, monkeypatch
):
    # Signal from the actual Chat adapter's serialized visible-text chunk,
    # before releasing the in-memory transport's final bytes.
    from aistudio_api.api import routes_compat

    prefix = wire(chunk([text("leading"), call("weather", {"city": "Paris"}, "A")]))[
        :-2
    ]
    terminal = wire(chunk([text("following")], 1))[2:]
    wire_source.raw = prefix + b"," + terminal
    wire_source.chunk_boundary = len(prefix) + 1
    wire_source.pause = asyncio.Event()
    received = []
    leading_visible = asyncio.Event()
    adapter = routes_compat.openai_stream_adapter

    async def observe(*args, **kwargs):
        async for frame in adapter(*args, **kwargs):
            received.extend(sse_events(frame))
            if '"leading"' in frame:
                leading_visible.set()
            yield frame

    monkeypatch.setattr(routes_compat, "openai_stream_adapter", observe)
    pending = asyncio.create_task(request("chat", True))
    try:
        await asyncio.wait_for(leading_visible.wait(), timeout=2)
        assert not tool_calls("chat", True, received)
        assert not pending.done()
        wire_source.pause.set()
        result = await asyncio.wait_for(pending, timeout=2)
    finally:
        wire_source.pause.set()
        if not pending.done():
            pending.cancel()
    assert [tool["id"] for tool in tool_calls("chat", True, result)] == ["A"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
async def test_later_stop_does_not_rehabilitate_an_interrupted_tool_set(
    wire_source, protocol, stream
):
    wire_source.raw = wire(
        chunk([call("weather", {"city": "Paris"}, "A"), text("partial")], 2),
        chunk([text("continuation")], 1),
    )
    events = await request(protocol, stream)
    assert not tool_calls(protocol, stream, events)
    assert "partial" in str(events) and "continuation" in str(events)


@pytest.mark.parametrize("stream", [False, True])
async def test_idless_complete_snapshot_can_replace_its_own_partial_shards(
    wire_source, stream
):
    wire_source.raw = wire(
        chunk([call("weather", '{"city":"Par')]),
        chunk([call("weather", '{"city":"Paris"}')]),
        chunk([call("weather", {"city": "Tokyo"})], 1),
    )
    events = await request("native", stream)
    assert tool_calls("native", stream, events) == [
        {"name": "weather", "args": {"city": "Paris"}},
        {"name": "weather", "args": {"city": "Tokyo"}},
    ]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("independent_name", ["measure", "measure_today"])
async def test_idless_unrelated_full_object_does_not_replace_incomplete_shard(
    wire_source, stream, independent_name
):
    wire_source.raw = wire(
        chunk([call("measure", '{"sample":"al')]),
        chunk([call(independent_name, {"sample": "beta"})]),
        chunk([call("measure", 'pha"}')], 1),
    )
    events = await request("native", stream)
    assert tool_calls("native", stream, events) == [
        {"name": "measure", "args": {"sample": "alpha"}},
        {"name": independent_name, "args": {"sample": "beta"}},
    ]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
async def test_independent_complete_idless_calls_remain_distinct(
    wire_source, protocol, stream
):
    wire_source.raw = wire(
        chunk([call("lookup", {"city": "Paris"})]),
        chunk([call("lookup", {"city": "Paris"})], 1),
    )
    events = await request(protocol, stream)
    calls = tool_calls(protocol, stream, events)
    assert len(calls) == 2
    assert all(tool["args"] == {"city": "Paris"} for tool in calls)
    if protocol != "native":
        assert calls[0]["id"] != calls[1]["id"]
