"""The official Gemini chat SDKs can replay a completed native SSE turn."""

from __future__ import annotations

import asyncio
import json
from time import perf_counter

import httpx
import pytest
import test_ordered_tool_routes as wire_routes
import test_signed_part_roundtrip as signed_parts
from google import genai
from google.genai import types
from starlette.types import Message
from test_client_disconnect import _Client

from aistudio_api.api.app import app
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

route = signed_parts.route
wire_gateway = wire_routes.wire_source
_PYTHON_TELEMETRY = "google-genai-sdk/1.68.0 gl-python/3.12.0"
_NODE_TELEMETRY = "google-genai-sdk/3.6.1 gl-node/v24.22.3"
_CALL = "call-blue"


@pytest.mark.parametrize("shape", ["single", "ordered", "signed_call"])
@pytest.mark.parametrize("separate_terminal", [False, True])
async def test_google_chat_replays_exact_streamed_model_parts(
    route, shape, separate_terminal
):
    if shape == "single":
        expected = [AistudioPart(text="Cobalt.")]
    elif shape == "ordered":
        expected = [
            AistudioPart(text="A "),
            AistudioPart(text="cobalt square."),
            AistudioPart(thought_signature=signed_parts.signature("detached")),
        ]
    else:
        expected = [
            AistudioPart(
                thought_signature=signed_parts.signature("call"),
                function_call=("lookup", {"color": "cobalt"}, _CALL),
            )
        ]
    parts = [part.to_wire() for part in expected]
    # Exercise a terminal in the same candidate, as well as a metadata-only
    # terminal which requires the one-Part lookbehind.
    chunks = [wire_routes.chunk([part]) for part in parts[:-1]]
    chunks.append(wire_routes.chunk([parts[-1]], None if separate_terminal else 1))
    if separate_terminal:
        chunks.append(wire_routes.chunk([], 1))
    terminal_with_usage = [*chunks.pop(), None, [8, 3, 11]]
    route.wire.raw = wire_routes.wire(*chunks, terminal_with_usage)
    client = genai.Client(
        api_key="fixture",
        http_options=types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            httpx_async_client=route.raw,
            headers=signed_parts.HEADERS,
        ),
    )

    async with client.aio as sdk:
        chat = sdk.chats.create(model=signed_parts.MODEL)
        result = [
            chunk async for chunk in await chat.send_message_stream("Remember cobalt.")
        ]
        assert all(
            chunk.candidates
            and chunk.candidates[0].content
            and chunk.candidates[0].content.parts
            for chunk in result
        )
        assert [
            candidate.finish_reason
            for chunk in result
            for candidate in (chunk.candidates or [])
            if candidate.finish_reason is not None
        ] == [types.FinishReason.STOP]
        assert [
            chunk.usage_metadata.total_token_count
            for chunk in result
            if chunk.usage_metadata
        ] == [11]
        assert sum(
            len(list(signed_parts._native_parts(chunk))) for chunk in result
        ) == len(parts)
        history = chat.get_history(curated=True)
        assert history[0].role == "user"
        assert all(content.role == "model" for content in history[1:])
        reply = types.Part(text="Which color?")
        if shape == "signed_call":
            reply = types.Part(
                function_response=types.FunctionResponse(
                    name="lookup", id=_CALL, response={"color": "cobalt"}
                )
            )
        route.wire.reply(AistudioPart(text="The next answer."))
        await chat.send_message(reply)

    assert route.wire.model_wire == parts


@pytest.mark.parametrize("prefix", [False, True], ids=["single", "ordered"])
@pytest.mark.parametrize(("finish", "reason"), [(1, "STOP"), (2, "MAX_TOKENS")])
async def test_node_native_stream_keeps_signed_tail_only_on_success(
    route, prefix, finish, reason
):
    signature = signed_parts.signature("node-tail")
    expected = ([{"text": "Prefix."}] if prefix else []) + [
        {"text": "Tail.", "thoughtSignature": signature}
    ]
    parts = ([wire_routes.text("Prefix.")] if prefix else []) + [
        wire_routes.text("Tail.", signature=signature)
    ]
    route.wire.raw = wire_routes.wire(
        wire_routes.chunk(parts), [*wire_routes.chunk([], finish), None, [8, 3, 11]]
    )
    path, payload = wire_routes.input_request("native", True)
    response = await route.raw.post(
        path,
        params={"alt": "sse"},
        json=payload,
        headers={"x-goog-api-client": _NODE_TELEMETRY, "user-agent": _NODE_TELEMETRY},
    )
    assert response.status_code == 200, response.text
    events = wire_routes.sse_events(response.text)
    candidates = [event["candidates"][0] for event in events]
    assert wire_routes.native_parts(events) == expected
    assert all(candidate["content"]["parts"] for candidate in candidates[:-1])
    assert all("finishReason" not in candidate for candidate in candidates[:-1])
    assert candidates[-1]["finishReason"] == reason
    assert candidates[-1]["content"]["parts"] == (expected[-1:] if finish == 1 else [])
    assert all("usageMetadata" not in event for event in events[:-1])
    assert events[-1]["usageMetadata"]["totalTokenCount"] == 11


@pytest.mark.parametrize("terminal", [2, 3, 5])
async def test_google_chat_does_not_curate_nonstop_stream(route, terminal):
    route.wire.raw = wire_routes.wire(
        wire_routes.chunk(
            [wire_routes.text("Partial."), wire_routes.call("lookup", {}, _CALL)]
        ),
        wire_routes.chunk([], terminal),
    )
    client = genai.Client(
        api_key="fixture",
        http_options=types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            httpx_async_client=route.raw,
            headers=signed_parts.HEADERS,
        ),
    )
    async with client.aio as sdk:
        chat = sdk.chats.create(model=signed_parts.MODEL)
        result = [
            chunk async for chunk in await chat.send_message_stream("Remember cobalt.")
        ]
        parts = [part for chunk in result for part in signed_parts._native_parts(chunk)]
        assert [part.text for part in parts] == ["Partial."]
        assert all(part.function_call is None for part in parts)
        assert chat.get_history(curated=True) == []
        assert result[-1].candidates
        assert (
            result[-1].candidates[0].finish_reason
            == {
                2: types.FinishReason.MAX_TOKENS,
                3: types.FinishReason.SAFETY,
                5: types.FinishReason.OTHER,
            }[terminal]
        )


@pytest.mark.parametrize(
    ("headers", "sdk", "multi_part"),
    [
        pytest.param({}, False, False, id="raw"),
        pytest.param(
            {"x-goog-api-client": _PYTHON_TELEMETRY}, True, False, id="python"
        ),
        pytest.param({"user-agent": _PYTHON_TELEMETRY}, True, False, id="python-ua"),
        pytest.param(
            {"x-goog-api-client": _PYTHON_TELEMETRY}, True, True, id="python-batch"
        ),
        pytest.param({"x-goog-api-client": _NODE_TELEMETRY}, True, False, id="node"),
        pytest.param({"user-agent": _NODE_TELEMETRY}, True, False, id="node-ua"),
        pytest.param(
            {"x-goog-api-client": _NODE_TELEMETRY}, True, True, id="node-batch"
        ),
        pytest.param({"user-agent": "node"}, False, False, id="generic-node"),
        pytest.param(
            {"x-goog-api-client": "gl-node/v24.22.3"}, False, False, id="runtime-only"
        ),
        pytest.param(
            {"x-goog-api-client": "google-genai-sdk/3.6.1"}, False, False, id="sdk-only"
        ),
        pytest.param(
            {
                "x-goog-api-client": "google-genai-sdk/3.6.1 gl-node/web",
                "user-agent": "Mozilla/5.0",
            },
            False,
            False,
            id="browser-sdk",
        ),
        pytest.param(
            {
                "x-goog-api-client": "google-genai-sdk/3.6.1",
                "user-agent": "gl-node/v24.22.3",
            },
            False,
            False,
            id="split-labels",
        ),
    ],
)
async def test_native_first_part_latency_is_at_most_one_part_lookbehind(
    wire_gateway, monkeypatch, headers, sdk, multi_part
):
    first_read, second_read, allow_second, allow_finish, first_emit = [
        asyncio.Event() for _ in range(5)
    ]
    stamps: dict[str, float] = {}
    outputs: list[dict] = []
    first_group = (
        ["Leading.", "In the same upstream chunk."] if multi_part else ["Leading."]
    )
    first = wire_routes.wire(
        wire_routes.chunk([wire_routes.text(text) for text in first_group])
    )[:-2]
    second = wire_routes.wire(wire_routes.chunk([wire_routes.text("Tail.")]))[2:-2]
    last = wire_routes.wire(wire_routes.chunk([], 1))[2:]

    async def answer(**_kwargs):
        yield "status", 200
        stamps["first_upstream"] = perf_counter()
        yield "chunk", first + b","
        first_read.set()
        await allow_second.wait()
        stamps["second_upstream"] = perf_counter()
        yield "chunk", second + b","
        second_read.set()
        await allow_finish.wait()
        yield "chunk", last

    async def observe(scope, receive, send):
        async def record(message: Message) -> None:
            await send(message)
            body = message.get("body")
            if isinstance(body, bytes) and body.startswith(b"data: "):
                outputs.append(json.loads(body[6:]))
                if b"Leading." in body:
                    stamps["first_client"] = perf_counter()
                    first_emit.set()

        await app(scope, receive, record)

    monkeypatch.setattr(wire_gateway, "send_streaming_request", answer)
    path, payload = wire_routes.input_request("native", True)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(observe),
            base_url="http://fixture.invalid",
            headers={**signed_parts.HEADERS, **headers},
        ) as client,
        asyncio.timeout(3),
        asyncio.TaskGroup() as group,
    ):
        task = group.create_task(client.post(path, json=payload))
        await first_read.wait()
        early = not sdk or multi_part
        if early:
            await first_emit.wait()
        await asyncio.sleep(0.04)  # A deliberate gap between upstream Parts.
        assert first_emit.is_set() is early
        allow_second.set()
        await second_read.wait()
        await first_emit.wait()
        assert not task.done()
        assert outputs[0]["candidates"][0]["content"]["parts"] == [{"text": "Leading."}]
        assert "finishReason" not in outputs[0]["candidates"][0]
        allow_finish.set()

    assert task.result().status_code == 200
    assert wire_routes.native_parts(outputs) == [
        {"text": text} for text in [*first_group, "Tail."]
    ]
    assert outputs[-1]["candidates"][0]["finishReason"] == "STOP"
    end = outputs[-1]["candidates"][0]["content"]["parts"]
    assert end == ([{"text": "Tail."}] if sdk else [])
    assert (stamps["first_client"] < stamps["second_upstream"]) is early
    print(
        f"{headers or 'raw'} ({len(first_group)} initial Parts) first useful (ms): "
        f"{(stamps['first_client'] - stamps['first_upstream']) * 1000:.2f}"
    )


@pytest.mark.parametrize("ending", ["disconnect", "timeout", "wire_error"])
@pytest.mark.parametrize(
    "telemetry", [_PYTHON_TELEMETRY, _NODE_TELEMETRY], ids=["python", "node"]
)
async def test_google_sdk_lone_pending_part_cleans_up_on_aborted_stream(
    wire_gateway, monkeypatch, ending, telemetry
):
    stalled = asyncio.Event()
    closed = 0
    calls = 0
    monkeypatch.setattr(
        settings, "request_timeout", 0.05 if ending == "timeout" else 15
    )
    monkeypatch.setattr(settings, "request_history", False)

    async def answer(**_kwargs):
        nonlocal closed, calls
        calls += 1
        try:
            yield "status", 200
            # A parsed STOP on its own cannot establish that the transport
            # completed; a late failure must not masquerade as success.
            yield (
                "chunk",
                wire_routes.wire(wire_routes.chunk([wire_routes.text("Partial.")], 1)),
            )
            stalled.set()
            if ending == "wire_error":
                yield "error", "fixture ended"
            else:
                await asyncio.Event().wait()
        finally:
            closed += 1

    monkeypatch.setattr(wire_gateway, "send_streaming_request", answer)
    client = _Client("gemini", stream=True, spec_version="2.4")
    client.scope["headers"] = [
        *client.scope["headers"],
        (b"x-goog-api-client", telemetry.encode()),
        (b"x-anti-truncation", b"false"),
    ]
    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        group.create_task(app(client.scope, client.receive, client.send))
        await stalled.wait()
        if ending == "disconnect":
            client.disconnect.set()

    assert calls == closed == 1
    events = [
        json.loads(body[6:])
        for message in client.messages
        if isinstance(body := message.get("body"), bytes) and body.startswith(b"data: ")
    ]
    assert all(
        "finishReason" not in part
        for event in events
        for part in event.get("candidates", [])
    )
    if ending == "disconnect":
        assert events == []
    elif ending == "timeout":
        assert len(events) == 1 and events[0]["error"]["code"] == 504
    else:
        assert len(events) == 2
        assert wire_routes.native_parts(events) == [{"text": "Partial."}]
        assert events[-1]["error"]["code"] == 502


async def test_google_sdk_no_output_keeps_diagnostic_without_curating_turn(route):
    route.wire.raw = wire_routes.wire(wire_routes.chunk([], 1))
    client = genai.Client(
        api_key="fixture",
        http_options=types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            httpx_async_client=route.raw,
            headers=signed_parts.HEADERS,
        ),
    )
    async with client.aio as sdk:
        chat = sdk.chats.create(model=signed_parts.MODEL)
        result = [chunk async for chunk in await chat.send_message_stream("Hello.")]
        assert len(result) == 2
        assert "空回诊断" in (result[0].text or "")
        assert result[-1].candidates
        assert result[-1].candidates[0].finish_reason == types.FinishReason.STOP
        assert chat.get_history(curated=True) == []
