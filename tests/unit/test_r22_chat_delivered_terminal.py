"""Chat's accepted choice, optional usage and sentinel have distinct boundaries."""

import asyncio
import json
from contextlib import nullcontext

import httpx2
import openai
import pytest
import test_ordered_tool_routes as wire
from starlette.types import Message, Receive, Scope, Send
from test_client_disconnect import _Client
from test_responses_failure_sdk import _arm_wire_deadline

from aistudio_api.api.app import app
from aistudio_api.config import settings
from aistudio_api.infrastructure.request_history import request_history

chat_wire = wire.wire_source
_TEXT = 'An example: {"finish_reason":"tool_calls"}'
_ARGS = {"label": "blue", "finish_reason": "stop"}
_SIGNATURE = {"google": {"thought_signature": "b3BhcXVl"}}
_DEADLINE = "Request exceeded total timeout"


def _frame(body: bytes) -> tuple[str, dict] | None:
    if body == b"data: [DONE]\n\n":
        return "done", {}
    if not body.startswith(b"data: "):
        return None
    event = json.loads(body[6:])
    if "error" in event:
        return "error", event
    if not event["choices"]:
        return "usage", event
    choice = event["choices"][0]
    if choice["finish_reason"] is not None:
        return "finish", event
    if "role" in choice["delta"]:
        return "role", event
    if "reasoning_content" in choice["delta"]:
        return "thought", event
    return "payload", event


@pytest.mark.parametrize(
    ("kind", "include_usage", "cut"),
    [
        (kind, include_usage, cut)
        for kind in ("text", "tool")
        for include_usage in (False, True)
        for cut in (
            "baseline",
            "after_payload",
            "after_finish",
            "after_done",
            "during_finish",
            "during_done",
            *(("after_usage", "during_usage") if include_usage else ()),
        )
    ]
    + [
        ("length", False, "after_finish"),
        ("filter", True, "after_finish"),
        ("signed_thought", True, "after_finish"),
        ("tool", True, "reject_recovery"),
        ("tool", True, "stall_recovery"),
    ],
)
async def test_chat_deadline_at_outer_send(
    monkeypatch, chat_wire: wire.WireSource, kind: str, include_usage: bool, cut: str
) -> None:
    expire = _arm_wire_deadline(monkeypatch)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 60)
    monkeypatch.setattr(settings, "request_history", True)
    if cut == "stall_recovery":
        monkeypatch.setattr(settings, "stream_send_timeout", 0.2)
    recovery_failure = cut.endswith("_recovery")
    parts = (
        [wire.call("paint", _ARGS, "A", signature="b3BhcXVl")]
        if kind == "tool"
        else [wire.text(_TEXT)]
    )
    if kind == "signed_thought":
        parts.insert(0, wire.text("Look first.", thought=True, signature="b3BhcXVl"))
    finish = {"length": 2, "filter": 3}.get(kind, 1)
    chat_wire.raw = wire.wire([*wire.chunk(parts, finish), None, [8, 3, 11]])
    attempted: list[str] = []
    accepted: list[str] = []
    frames: dict[str, dict] = {}
    request_id = ""

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        async def observed(message: Message) -> None:
            nonlocal request_id
            if message["type"] == "http.response.start":
                assert message["status"] == 200 and not request_id
                request_id = dict(message["headers"])[b"x-request-id"].decode()
            frame = _frame(message.get("body", b""))
            if frame:
                name, _ = frame
                attempted.append(name)
                if cut == "during_" + name:
                    expire()
                    await asyncio.Event().wait()  # Partial write, never acknowledged.
                if cut == "reject_recovery" and name == "done":
                    raise OSError("fixture transport closed")
                if cut == "stall_recovery" and name == "done":
                    await asyncio.Event().wait()  # Only the send timer remains.
            await send(message)
            if frame:
                name, event = frame
                frames[name] = event
                accepted.append(name)
                if cut == "after_" + name or (recovery_failure and name == "finish"):
                    expire()

        await app(scope, receive, observed)

    reason = {"tool": "tool_calls", "length": "length", "filter": "content_filter"}.get(
        kind, "stop"
    )
    interrupted = cut.startswith("during_") or recovery_failure
    early = cut == "after_payload"
    sdk_events = []
    async with (
        asyncio.timeout(4),
        openai.AsyncOpenAI(
            api_key="fixture",
            base_url="http://fixture.invalid/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(asgi)),
            max_retries=0,
            _strict_response_validation=True,
        ) as client,
    ):
        with (
            pytest.raises(OSError)
            if interrupted
            else pytest.raises(openai.APIError, match=_DEADLINE)
            if early
            else nullcontext()
        ):
            async with client.chat.completions.stream(
                model=wire.MODEL,
                messages=[{"role": "user", "content": "Describe a sample."}],
                stream_options={"include_usage": include_usage},
                extra_headers={"x-anti-truncation": "0"},
            ) as stream:
                async for event in stream:
                    if event.type == "chunk" and event.chunk.choices:
                        if end := event.chunk.choices[0].finish_reason:
                            sdk_events.append(("finish", end))
                    elif event.type == "content.done":
                        sdk_events.append((event.type, event.content))
                    elif event.type == "tool_calls.function.arguments.done":
                        sdk_events.append(
                            (event.type, event.name, json.loads(event.arguments))
                        )
                if reason in ("length", "content_filter"):
                    # The stock parser raises its original finish-specific error.
                    with pytest.raises(
                        openai.LengthFinishReasonError
                        if reason == "length"
                        else openai.ContentFilterFinishReasonError
                    ):
                        await stream.get_final_completion()
                    final = stream.current_completion_snapshot
                else:
                    final = await stream.get_final_completion()
                assert final.choices[0].finish_reason == reason
                if "usage" in accepted:
                    assert final.usage and final.usage.total_tokens == 11
                else:
                    assert final.usage is None

    prefix = ["role", *(["thought"] if kind == "signed_thought" else []), "payload"]
    record = request_history().get(request_id)
    assert record is not None
    if early:
        assert attempted == accepted == [*prefix, "error", "done"]
        assert frames["error"]["error"]["code"] == 504 and not sdk_events
        assert (record["result"], record["error"]) == ("timeout", _DEADLINE)
    elif interrupted:
        pending = "done" if recovery_failure else cut.removeprefix("during_")
        if pending != "finish":
            prefix.append("finish")
        if pending == "done" and include_usage and not recovery_failure:
            prefix.append("usage")
        assert accepted == prefix and attempted == [*prefix, pending]
        assert record["result"] == "cancelled" and not record["finish_reason"]
        if cut == "reject_recovery":
            assert "Downstream disconnected" in str(record["error"])
        elif cut == "stall_recovery":
            assert record["error"] == "Downstream response send timed out"
        else:
            assert record["error"] == "Request timeout interrupted downstream send"
    else:
        metered = include_usage and cut != "after_finish"
        assert (
            attempted
            == accepted
            == [*prefix, "finish", *(["usage"] if metered else []), "done"]
        )
        assert frames["finish"]["choices"][0]["finish_reason"] == reason
        assert sdk_events == [
            ("finish", reason),
            ("tool_calls.function.arguments.done", "paint", _ARGS)
            if kind == "tool"
            else ("content.done", _TEXT),
        ]
        raw_reason = {"length": "MAX_TOKENS", "filter": "SAFETY"}.get(kind, "STOP")
        assert (record["result"], record["error"], record["finish_reason"]) == (
            "success",
            "",
            raw_reason,
        )
    if kind == "tool":
        tool = frames["payload"]["choices"][0]["delta"]["tool_calls"][0]
        assert tool["id"] == "A" and tool["extra_content"] == _SIGNATURE
    if kind == "signed_thought":
        assert frames["finish"]["choices"][0]["delta"]["extra_content"] == _SIGNATURE


async def test_disconnect_after_chat_finish_does_not_recover_a_sentinel(
    chat_wire: wire.WireSource, monkeypatch, tmp_path
) -> None:
    chat_wire.raw = wire.wire(wire.chunk([wire.text(_TEXT)], 1))
    monkeypatch.setattr(settings, "request_history", True)
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    client = _Client("chat", stream=True, spec_version="2.4")
    sent = []

    async def send(message: Message) -> None:
        await client.send(message)
        if frame := _frame(message.get("body", b"")):
            sent.append(frame[0])
            if frame[0] == "finish":
                client.disconnect.set()

    async with asyncio.timeout(4):
        await app(client.scope, client.receive, send)
    assert sent == ["role", "payload", "finish"]
    record = client.stored_result()
    assert record["result"] == "cancelled" and not record["finish_reason"]
    assert "Downstream disconnected" in record["error"]
