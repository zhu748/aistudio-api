"""Signed multi-step histories survive client metadata loss and concurrent rolls."""

from __future__ import annotations

import base64
import json
from typing import Any, cast
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
from aistudio_api.domain import tool_signatures
from aistudio_api.domain.errors import TransportError
from aistudio_api.domain.tool_signatures import SKIP_THOUGHT_SIGNATURE
from aistudio_api.infrastructure.gateway import model_turns as module
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_turns import ModelTurnHistory
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.gateway.wire_spec import TopLevelIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart
from aistudio_api.infrastructure.request_context import RequestContext, current_request
from aistudio_api.infrastructure.signature_trace import get_signature_trace

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


@pytest.mark.parametrize(
    "changed", ["client", "prompt", "whitespace", "arguments", "text", "order"]
)
def test_changed_conversation_does_not_restore_another_state(changed):
    history = ModelTurnHistory()
    contents = echo(remember(history))
    scope = "other" if changed == "client" else "scope"
    if changed == "prompt":
        contents[0].parts[0].text = "different initial prompt"
    elif changed == "whitespace":
        contents[0].parts[0].text = f"  {PROMPT}"
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
    # 上限改为 _turn_limits() 档位解析（内存预算自适应）：按 (cache,
    # entries, turn) 注入小档位，与旧常量 monkeypatch 语义一致。
    monkeypatch.setattr(module, "_turn_limits", lambda: (64 << 20, 1000, 50))
    history = ModelTurnHistory()
    contents = remember(history)
    assert history.prepare("scope", echo(contents))[1] is None
    monkeypatch.setattr(module, "_turn_limits", lambda: (64 << 20, 1000, 10000))
    history = ModelTurnHistory()
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


def test_echoed_reasoning_conflict_preserves_original_request():
    history = ModelTurnHistory()
    raw = [
        AistudioPart(
            text="newer thought", thought=True, thought_signature=SIGNATURE
        ).to_wire(),
        AistudioPart(text="same visible answer").to_wire(),
    ]
    user = remember(history, parts=raw)
    original = AistudioContent(
        "model",
        [
            AistudioPart(text="older thought", thought=True),
            AistudioPart(text="same visible answer"),
        ],
    )
    restored, token, context = history.prepare(
        "scope",
        [*user, original, AistudioContent("user", [AistudioPart(text="next")])],
    )
    assert restored[1] is original
    assert token is None
    assert context is None


def test_identical_captures_restore_parts_without_choosing_between_tokens():
    history = ModelTurnHistory()
    initial = remember(history)
    remember(history, token="v1_" + "b" * 24)
    restored, token, _ = history.prepare("scope", echo(initial))
    assert restored[1].parts[0].thought
    assert token is None


def test_scoped_turn_precedes_conflicting_per_call_cache(monkeypatch, tmp_path):
    history = ModelTurnHistory()
    cache = tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json")
    monkeypatch.setattr(tool_signatures, "_cache_instance", cache)
    other = base64.b64encode(b"unrelated fixture state").decode()
    for i, (name, args) in enumerate(CALLS):
        cache.remember(f"echo-{i}", name, args, other)
    raw = [
        AistudioPart(
            function_call=(name, args, f"upstream-{i}"),
            thought_signature=SIGNATURE if i == 0 else None,
        ).to_wire()
        for i, (name, args) in enumerate(CALLS)
    ]
    contents = echo(remember(history, parts=raw))
    restored, token, _ = history.prepare("scope", contents)
    assert token == TOKEN
    assert [part.thought_signature for part in restored[1].parts] == [SIGNATURE, None]
    assert [part.thought_signature for part in contents[1].parts] == [None, None]


def test_cache_capacity_and_memory_are_bounded(monkeypatch):
    monkeypatch.setattr(module, "_turn_limits", lambda: (64 << 20, 2, 4 << 20))
    history = ModelTurnHistory()
    first = remember(history, scope="a")
    remember(history, scope="b")
    assert history.prepare("a", echo(first))[1] == TOKEN
    remember(history, scope="c")
    assert history.prepare("b", echo(first))[1] is None
    assert history.prepare("a", echo(first))[1] == TOKEN
    monkeypatch.setattr(module, "_turn_limits", lambda: (1, 2, 4 << 20))
    history = ModelTurnHistory()
    remember(history, scope="a")
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


def test_explicit_sig_unobserved_after_restart_keeps_session_cacheable(
    monkeypatch, tmp_path
):
    """Claude Code after a restart (warm history gone, cold cache wiped): the
    client echoes the lead FC signature via extra_content. That turn is forwarded
    untouched (siblings stay bare), the rest of the session is cacheable again
    (no chain_broken), and the exact echoed signature is bound into the prefix so
    a different signature under the same visible text cannot unlock turn 2."""
    monkeypatch.setattr(
        tool_signatures,
        "_cache_instance",
        tool_signatures.ThoughtSignatureCache(tmp_path / "before.json"),
    )
    contents = remember(ModelTurnHistory())
    # Restart: fresh in-memory history and fresh on-disk cache (ephemeral data dir).
    restarted = ModelTurnHistory()
    monkeypatch.setattr(
        tool_signatures,
        "_cache_instance",
        tool_signatures.ThoughtSignatureCache(tmp_path / "after.json"),
    )
    second_sig = base64.b64encode(b"upstream-state-for-turn-two").decode()
    request_token = current_request.set(
        RequestContext("req_restart", category="model", protocol="claude")
    )
    try:
        trace = get_signature_trace(create=True)
        assert trace is not None
        echoed = echo(contents)
        echoed[1].parts[0].thought_signature = SIGNATURE
        restored, token, context = restarted.prepare("scope", echoed)
        assert token is None
        assert context is not None
        # Client signature untouched; the unsigned sibling keeps the bare shape.
        assert restored[1].parts[0].thought_signature == SIGNATURE
        assert restored[1].parts[1].thought_signature is None
        replay = cast(dict[str, Any], trace.to_dict()["replay"])
        assert "explicit_sig_unobserved" in replay["miss_reasons"]
        assert "chain_broken" not in replay["miss_reasons"]
        assert replay["skip_fills"] == 0

        # Turn 2 is generated on the restarted process and stored under the bound prefix.
        capture = context.capture()
        capture.add(
            [
                AistudioPart(text="private reasoning 2", thought=True).to_wire(),
                AistudioPart(
                    function_call=("reply", {"message": "answer"}, "server-2"),
                    thought_signature=second_sig,
                ).to_wire(),
            ],
            TOKEN,
        )
        capture.finish(1)

        # Next request: the client echoes turn 2 with the signature stripped; the
        # warm cache restores the original roll (thought + real signature).
        turn2_echo = AistudioContent(
            "model",
            [AistudioPart(function_call=("reply", {"message": "answer"}, "echo-2"))],
        )
        follow_up = AistudioContent("user", [AistudioPart(text="next")])
        restored2, token2, _ = restarted.prepare(
            "scope", [*echoed, turn2_echo, follow_up]
        )
        assert token2 == TOKEN
        assert restored2[3].parts[0].thought
        assert restored2[3].parts[1].thought_signature == second_sig

        # A different echoed signature on turn 1 is another prefix: turn 2 stays locked.
        other = echo(contents)
        other[1].parts[0].thought_signature = base64.b64encode(
            b"another upstream state"
        ).decode()
        restored3, token3, _ = restarted.prepare(
            "scope", [*other, turn2_echo, follow_up]
        )
        assert token3 is None
        assert restored3[3].parts[0].thought_signature == SKIP_THOUGHT_SIGNATURE
    finally:
        current_request.reset(request_token)


def test_stripped_tool_signature_with_explicit_thought_signature_gets_lead_skip(
    monkeypatch, tmp_path
):
    """A client that echoes a real thought signature but strips the tool
    signature must not send a bare lead functionCall: only the lead call gets the
    SKIP sentinel, the thought signature stays on the thought part."""
    monkeypatch.setattr(
        tool_signatures,
        "_cache_instance",
        tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json"),
    )
    history = ModelTurnHistory()
    thought_sig = base64.b64encode(b"thought-state").decode()
    contents = [
        AistudioContent("user", [AistudioPart(text=PROMPT)]),
        AistudioContent(
            "model",
            [
                AistudioPart(
                    text="private reasoning",
                    thought=True,
                    thought_signature=thought_sig,
                ),
                AistudioPart(function_call=("context", {"query": "sync"}, "call-0")),
                AistudioPart(function_call=("reply", {"message": "answer"}, "call-1")),
            ],
        ),
        AistudioContent("user", [AistudioPart(text="next")]),
    ]
    restored, token, context = history.prepare("scope", contents)
    assert token is None
    assert context is not None
    parts = restored[1].parts
    assert parts[0].thought and parts[0].thought_signature == thought_sig
    assert parts[1].thought_signature == SKIP_THOUGHT_SIGNATURE
    assert parts[2].thought_signature is None
    assert contents[1].parts[1].thought_signature is None
