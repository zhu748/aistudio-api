"""Signed multi-step histories survive client metadata loss and concurrent rolls."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import pytest

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.schemas_compat import (
    ClaudeMessagesRequest,
    OpenAIChatCompletionRequest,
    OpenAIResponsesRequest,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.compat_service import (
    claude_to_gemini_request,
    openai_to_gemini_request,
)
from aistudio_api.application.responses_service import responses_to_gemini_request
from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.gateway import model_turns as module
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_turns import ModelTurnHistory
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.gateway.wire_spec import TopLevelIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart
from aistudio_api.infrastructure.request_context import RequestContext, current_request

SIGNATURE = base64.b64encode(b"upstream-state-for-this-turn").decode()
TOKEN = "v1_" + "a" * 24
MODEL = "gemini-4-hydrogen"
PROMPT = "Read the context and reply."
CALLS = [("context", {"query": "sync"}), ("reply", {"message": "answer"})]


def upstream_parts():
    return [
        AistudioPart(text="private reasoning", thought=True).to_wire(),
        *[
            AistudioPart(
                function_call=(name, args, f"server-{i}"), thought_signature=SIGNATURE
            ).to_wire()
            for i, (name, args) in enumerate(CALLS)
        ],
        AistudioPart(text="", thought_signature=SIGNATURE).to_wire(),
    ]


def wire_response(parts=None, token=TOKEN, finish=1):
    chunk = [
        [[[parts if parts is not None else upstream_parts(), "model"], finish]],
        None,
        [1, 2, 3],
        None,
        None,
        None,
        None,
        token,
    ]
    return json.dumps([[chunk]], ensure_ascii=False).encode()


@pytest.fixture
def client():
    client = AIStudioClient()
    ctx = SessionContext(
        auth_file="/data/account/auth.json", cookies={"SAPISID": "fixture"}
    )
    client.session.ensure_ready = AsyncMock(return_value=ctx)
    client.session.generate_snapshot = AsyncMock(return_value="!proof")
    client.session.send_request = AsyncMock(return_value=(200, wire_response()))
    return client


async def echoed_request(protocol):
    if protocol == "gemini":
        request = GeminiGenerateContentRequest.model_validate(
            {
                "tools": [],
                "contents": [
                    {"role": "user", "parts": [{"text": PROMPT}]},
                    {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": name,
                                    "args": args,
                                    "id": f"client-{i}",
                                }
                            }
                            for i, (name, args) in enumerate(CALLS)
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "id": f"client-{i}",
                                    "name": name,
                                    "response": {"result": "tool result"},
                                }
                            }
                            for i, (name, _) in enumerate(CALLS)
                        ],
                    },
                ],
            }
        )
    elif protocol == "claude":
        request, _ = await claude_to_gemini_request(
            ClaudeMessagesRequest.model_validate(
                {
                    "model": MODEL,
                    "tools": [],
                    "messages": [
                        {"role": "user", "content": PROMPT},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"client-{i}",
                                    "name": name,
                                    "input": args,
                                }
                                for i, (name, args) in enumerate(CALLS)
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": f"client-{i}",
                                    "content": "tool result",
                                }
                                for i in range(2)
                            ],
                        },
                    ],
                }
            )
        )
    elif protocol == "responses":
        request, _ = await responses_to_gemini_request(
            OpenAIResponsesRequest.model_validate(
                {
                    "model": MODEL,
                    "tools": [],
                    "input": [
                        {"role": "user", "content": PROMPT},
                        *[
                            {
                                "type": "function_call",
                                "call_id": f"client-{i}",
                                "name": name,
                                "arguments": json.dumps(args),
                            }
                            for i, (name, args) in enumerate(CALLS)
                        ],
                        *[
                            {
                                "type": "function_call_output",
                                "call_id": f"client-{i}",
                                "output": "tool result",
                            }
                            for i in range(2)
                        ],
                    ],
                }
            )
        )
    else:
        request, _ = await openai_to_gemini_request(
            OpenAIChatCompletionRequest.model_validate(
                {
                    "model": MODEL,
                    "tools": [],
                    "messages": [
                        {"role": "user", "content": PROMPT},
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": f"client-{i}",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(args),
                                    },
                                }
                                for i, (name, args) in enumerate(CALLS)
                            ],
                        },
                        *[
                            {
                                "role": "tool",
                                "tool_call_id": f"client-{i}",
                                "content": "tool result",
                            }
                            for i in range(2)
                        ],
                    ],
                }
            )
        )
    return normalize_gemini_request(request, MODEL)


@pytest.mark.parametrize("protocol", ["openai", "claude", "responses", "gemini"])
@pytest.mark.parametrize("stream", [False, True])
async def test_second_step_restores_original_parts_before_signing(
    client, protocol, stream
):
    request = await echoed_request(protocol)
    if stream:

        async def upstream(**kwargs):
            yield "status", 200
            raw = wire_response()
            for start in range(0, len(raw), 17):
                yield "chunk", raw[start : start + 17]

        client.session.send_streaming_request = upstream
        assert [
            event
            async for event in client.stream_generate_content(
                model=MODEL, contents=request.contents[:1]
            )
        ]
    else:
        await client.generate_content(model=MODEL, contents=request.contents[:1])

    prepared = await client.prepare_request(model=MODEL, contents=request.contents)
    body = json.loads(prepared.body)
    restored = body[TopLevelIndex.CONTENTS][1][0]
    expected = upstream_parts()
    for i, part in enumerate(expected[1:3]):
        payload = part[10]
        assert isinstance(payload, list)
        payload[2] = f"client-{i}"
    assert restored == expected
    assert body[TopLevelIndex.SESSION_TOKEN] == TOKEN
    assert client.session.generate_snapshot.await_args is not None
    signed = client.session.generate_snapshot.await_args.args[0]
    assert signed[1].to_wire()[0] == restored
    assert not any(part.thought for part in request.contents[1].parts)
    call = request.contents[1].parts[0].function_call
    assert call is not None
    assert call[-1] == "client-0"


def remember(history, scope="scope", prompt=PROMPT, parts=None, token=TOKEN, finish=1):
    contents = [AistudioContent("user", [AistudioPart(text=prompt)])]
    _, _, context = history.prepare(scope, contents)
    assert context is not None
    capture = context.capture()
    capture.add(parts if parts is not None else upstream_parts(), token)
    capture.finish(finish)
    return contents


def echo(contents):
    return [
        *contents,
        AistudioContent(
            "model",
            [
                AistudioPart(function_call=(name, args, f"echo-{i}"))
                for i, (name, args) in enumerate(CALLS)
            ],
        ),
        AistudioContent("user", [AistudioPart(text="next")]),
    ]


def test_rerolls_keep_distinct_turns_and_never_reuse_a_later_unknown_turn_token():
    history = ModelTurnHistory()
    contents = remember(history)
    other = [AistudioPart(text="other roll", thought_signature=SIGNATURE).to_wire()]
    remember(history, parts=other, token="v1_" + "b" * 24)
    restored, token, _ = history.prepare("scope", echo(contents))
    assert token == TOKEN
    assert restored[1].parts[0].thought
    unknown = [
        *echo(contents),
        AistudioContent("model", [AistudioPart(text="not issued here")]),
        AistudioContent("user", [AistudioPart(text="continue")]),
    ]
    assert history.prepare("scope", unknown)[1] is None


@pytest.mark.parametrize("changed", ["client", "prompt", "arguments", "text", "order"])
def test_changed_conversation_does_not_restore_another_state(changed):
    history = ModelTurnHistory()
    contents = echo(remember(history))
    scope = "other" if changed == "client" else "scope"
    if changed == "prompt":
        contents[0].parts[0].text = "different initial prompt"
    elif changed == "arguments":
        contents[1].parts[0].function_call = (
            "context",
            {"query": "different"},
            "echo-0",
        )
    elif changed == "text":
        contents[1].parts.insert(0, AistudioPart(text="edited response"))
    elif changed == "order":
        contents[1].parts.reverse()
    restored, token, _ = history.prepare(scope, contents)
    assert token is None
    assert not any(part.thought for part in restored[1].parts)


@pytest.mark.parametrize(
    "change", ["client", "model", "account", "auth_user", "system", "tools"]
)
async def test_prepare_scopes_model_history(client, change):
    request = await echoed_request("openai")
    context = current_request.set(RequestContext("first", key_id="key-a"))
    try:
        await client.generate_content(model=MODEL, contents=request.contents[:1])
        kwargs = {"model": MODEL, "contents": request.contents}
        if change == "client":
            current_request.set(RequestContext("second", key_id="key-b"))
        elif change == "model":
            kwargs["model"] = "gemini-3.8-flash"
        elif change == "account":
            client.session.ensure_ready.return_value = SessionContext(
                auth_file="/data/other/auth.json"
            )
        elif change == "auth_user":
            client.session.ensure_ready.return_value = SessionContext(
                auth_file="/data/account/auth.json", auth_user="1"
            )
        elif change == "system":
            kwargs["system_instruction"] = "different instructions"
        else:
            kwargs["tools"] = [[None, [["different_tool"]]]]
        prepared = await client.prepare_request(**kwargs)
        body = json.loads(prepared.body)
        assert (
            len(body) <= TopLevelIndex.SESSION_TOKEN
            or body[TopLevelIndex.SESSION_TOKEN] is None
        )
        assert not any(
            part.thought
            for part in client.session.generate_snapshot.await_args.args[0][1].parts
        )
    finally:
        current_request.reset(context)


@pytest.mark.parametrize("finish", [None, 2, 3, 5, 22])
def test_incomplete_or_rejected_turn_is_not_cached(finish):
    history = ModelTurnHistory()
    contents = remember(history, finish=finish)
    assert history.prepare("scope", echo(contents))[1] is None


@pytest.mark.parametrize("token", [TOKEN + "\n", "v1_" + "a" * 5000])
def test_invalid_token_is_not_replayed(token):
    history = ModelTurnHistory()
    contents = remember(history, token=token)
    restored, restored_token, _ = history.prepare("scope", echo(contents))
    assert restored_token is None
    assert restored[1].parts[0].thought


def test_large_or_media_response_is_not_retained(monkeypatch):
    history = ModelTurnHistory()
    monkeypatch.setattr(module, "_MAX_TURN_BYTES", 50)
    contents = remember(history)
    assert history.prepare("scope", echo(contents))[1] is None
    monkeypatch.setattr(module, "_MAX_TURN_BYTES", 10000)
    media = AistudioPart(inline_data=("image/png", "data"), thought_signature=SIGNATURE)
    contents = remember(history, parts=[media.to_wire()])
    assert (
        history.prepare(
            "scope",
            [
                *contents,
                AistudioContent("model", [media]),
                AistudioContent("user", [AistudioPart(text="next")]),
            ],
        )[1]
        is None
    )


def test_explicit_different_signature_is_preserved():
    history = ModelTurnHistory()
    contents = echo(remember(history))
    contents[1].parts[0].thought_signature = base64.b64encode(
        b"another upstream state"
    ).decode()
    restored, token, _ = history.prepare("scope", contents)
    assert token is None
    assert (
        restored[1].parts[0].thought_signature == contents[1].parts[0].thought_signature
    )
    assert not restored[1].parts[0].thought


def test_cache_capacity_and_memory_are_bounded(monkeypatch):
    history = ModelTurnHistory()
    monkeypatch.setattr(module, "_MAX_ENTRIES", 2)
    first = remember(history, scope="a")
    remember(history, scope="b")
    assert history.prepare("a", echo(first))[1] == TOKEN
    remember(history, scope="c")
    assert history.prepare("b", echo(first))[1] is None
    assert history.prepare("a", echo(first))[1] == TOKEN
    monkeypatch.setattr(module, "_MAX_CACHE_BYTES", 1)
    remember(history, scope="d")
    assert history.prepare("a", echo(first))[1] is None


async def test_failed_stream_does_not_cache_even_if_an_earlier_frame_said_stop(client):
    request = await echoed_request("openai")

    async def upstream(**kwargs):
        yield "status", 200
        yield "chunk", wire_response()
        yield "error", "connection reset"

    client.session.send_streaming_request = upstream
    with pytest.raises(TransportError, match="connection reset"):
        async for _ in client.stream_generate_content(
            model=MODEL, contents=request.contents[:1]
        ):
            pass
    prepared = await client.prepare_request(model=MODEL, contents=request.contents)
    body = json.loads(prepared.body)
    assert (
        len(body) <= TopLevelIndex.SESSION_TOKEN
        or body[TopLevelIndex.SESSION_TOKEN] is None
    )


async def test_cancelled_stream_does_not_commit_the_partially_consumed_turn(client):
    request = await echoed_request("openai")

    async def upstream(**kwargs):
        yield "status", 200
        yield "chunk", wire_response()

    client.session.send_streaming_request = upstream
    stream = client.stream_generate_content(model=MODEL, contents=request.contents[:1])
    await anext(stream)
    await stream.aclose()
    prepared = await client.prepare_request(model=MODEL, contents=request.contents)
    body = json.loads(prepared.body)
    assert (
        len(body) <= TopLevelIndex.SESSION_TOKEN
        or body[TopLevelIndex.SESSION_TOKEN] is None
    )


async def test_nonstream_continuation_cannot_inherit_an_earlier_stop(client):
    first = json.loads(wire_response())[0][0]
    later = [[[[[AistudioPart(text="unfinished").to_wire()], "model"]]]]
    client.session.send_request.return_value = (
        200,
        json.dumps([[first, later]]).encode(),
    )
    output = await client.generate_content(
        model=MODEL, contents=[AistudioContent("user", [AistudioPart(text=PROMPT)])]
    )
    assert output.candidates[0].finish_reason is None


async def test_buffered_replay_limit_preserves_reply_without_caching_large_parts(
    client, monkeypatch
):
    from aistudio_api.infrastructure.gateway import wire_parser

    monkeypatch.setattr(wire_parser, "_MAX_REPLAY_BYTES", 100)
    text = "large reply" * 1000
    client.session.send_request.return_value = (
        200,
        wire_response([AistudioPart(text=text, thought_signature=SIGNATURE).to_wire()]),
    )
    initial = [AistudioContent("user", [AistudioPart(text=PROMPT)])]
    output = await client.generate_content(model=MODEL, contents=initial)
    assert output.text == text
    assert not output.candidates[0].raw_parts
    followup = [
        *initial,
        AistudioContent("model", [AistudioPart(text=text)]),
        AistudioContent("user", [AistudioPart(text="next")]),
    ]
    prepared = await client.prepare_request(model=MODEL, contents=followup)
    body = json.loads(prepared.body)
    assert (
        len(body) <= TopLevelIndex.SESSION_TOKEN
        or body[TopLevelIndex.SESSION_TOKEN] is None
    )


async def test_replayed_request_gets_a_fresh_capture_after_stream_failure(client):
    request = await echoed_request("openai")
    prepared = await client.prepare_request(model=MODEL, contents=request.contents[:1])
    attempts = 0

    async def upstream(**kwargs):
        nonlocal attempts
        attempts += 1
        yield "status", 200
        if attempts == 1:
            yield (
                "chunk",
                wire_response(
                    [AistudioPart(text="failed attempt", thought=True).to_wire()]
                ),
            )
            yield "error", "reset"
        else:
            yield "chunk", wire_response()

    client.session.send_streaming_request = upstream
    with pytest.raises(TransportError):
        async for _ in client.stream_generate_content(model=MODEL, prepared=prepared):
            pass
    assert [
        event
        async for event in client.stream_generate_content(
            model=MODEL, prepared=prepared
        )
    ]
    followup = await client.prepare_request(model=MODEL, contents=request.contents)
    parts = json.loads(followup.body)[1][1][0]
    assert len(parts) == len(upstream_parts())
    assert "failed attempt" not in json.dumps(parts)
    assert parts[0][1] == "private reasoning"


def test_split_model_messages_restore_without_mutating_the_cached_turn():
    history = ModelTurnHistory()
    raw = [
        AistudioPart(text="private", thought=True).to_wire(),
        AistudioPart(text="Hello ").to_wire(),
        AistudioPart(text="world", thought_signature=SIGNATURE).to_wire(),
    ]
    initial = remember(history, parts=raw)
    contents = [
        *initial,
        AistudioContent("model", [AistudioPart(text="Hello")]),
        AistudioContent("model", [AistudioPart(text=" world")]),
        AistudioContent("user", [AistudioPart(text="next")]),
    ]
    restored, token, _ = history.prepare("scope", contents)
    assert token == TOKEN
    assert len(restored) == 3
    assert restored[1].to_wire()[0] == raw
    restored[1].parts.clear()
    assert history.prepare("scope", contents)[0][1].to_wire()[0] == raw
