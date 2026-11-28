"""Encrypted Chat details are evidence for one call in one assistant message."""

import base64
import json
from typing import cast

import httpx2
import openai
import pytest
import test_replay_signature_provenance as proof
from openai.types.chat import ChatCompletionMessageParam
from test_signed_part_roundtrip import (
    FOLLOWUP,
    HEADERS,
    MODEL,
    PROMPT,
    route as route,
    signature,
)

from aistudio_api.application.compat_shared import encode_signature_envelope
from aistudio_api.domain import tool_signatures
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

proof_route = proof.route
_A, _B, _C = (signature(label) for label in "ABC")
_MISSING = object()


def _detail(value, owner=_MISSING, fmt="google-gemini-v1"):
    return {
        "type": "reasoning.encrypted",
        "data": value,
        "format": fmt,
        **({} if owner is _MISSING else {"id": owner}),
    }


def _call(owner, n, **extra):
    return {
        "id": owner,
        "type": "function",
        "function": {"name": "lookup", "arguments": json.dumps({"n": n})},
        **extra,
    }


def _part(owner, n, value=None):
    return AistudioPart(
        function_call=("lookup", {"n": n}, owner), thought_signature=value
    )


def _sdk(app):
    return openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1",
        api_key="fixture",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app)),
        default_headers=HEADERS,
        _strict_response_validation=True,
        max_retries=0,
    )


async def _complete(sdk: openai.AsyncOpenAI, history: list, stream=False):
    # The provider-specific reasoning_details field is not in the SDK TypedDict.
    messages = cast(list[ChatCompletionMessageParam], history)
    if stream:
        async with sdk.chat.completions.stream(
            model=MODEL, messages=messages
        ) as response:
            return await response.get_final_completion()
    return await sdk.chat.completions.create(model=MODEL, messages=messages)


async def _assert_replay(route, stream, history, expected):
    route.wire.reply(AistudioPart(text="Done."))
    async with _sdk(route.app) as sdk:
        result = await _complete(sdk, [PROMPT, *history, FOLLOWUP], stream)
    assert result.choices[0].message.content == "Done."
    assert route.wire.model_wire == [p.to_wire() for p in expected]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_id_and_opaque_value_must_both_be_unique(route, stream, reverse):
    calls = [
        _call("duplicate", 1, thoughtSignature=_C),
        _call("duplicate", 2),
        _call("identical", 3),
        _call("identical", 3),
        _call("conflict", 4),
        _call("direct", 4, extra_content={"google": {"thought_signature": _C}}),
        _call("unique", 4),
    ]
    details = [
        _detail(_A, "duplicate"),
        _detail(_A, "identical"),
        *[_detail(value, "conflict") for value in (_A, _B, _A)],
        _detail(encode_signature_envelope(_B), "direct", "other"),
        _detail(_A, "direct"),
        _detail(_C, "unique"),
        _detail(_C, "unique"),
        _detail(_B, "absent"),
    ]
    history = [
        {
            "role": "assistant",
            "tool_calls": calls,
            "reasoning_details": details[::-1] if reverse else details,
        }
    ]
    await _assert_replay(
        route,
        stream,
        history,
        [
            _part("duplicate", 1, _C),
            _part("duplicate", 2),
            _part("identical", 3),
            _part("identical", 3),
            _part("conflict", 4),
            _part("direct", 4, _C),
            _part("unique", 4, _C),
        ],
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_only_equivalent_valid_values_agree(route, stream, reverse):
    standard = base64.b64encode(b"\xfb\xfe\xfb\xff").decode()
    urlsafe = base64.urlsafe_b64encode(b"\xfb\xfe\xfb\xff").decode().rstrip("=")
    details = [
        _detail(standard, "agree"),
        _detail(standard.rstrip("="), "agree"),
        _detail(urlsafe, "agree"),
        _detail(encode_signature_envelope(urlsafe), "agree", "other"),
        _detail(_B, "agree", "foreign"),
        _detail(f" {_B}", "agree"),
        _detail(encode_signature_envelope(f" {_B}"), "agree", "other"),
        _detail(encode_signature_envelope("not:base64"), "agree"),
        _detail(
            base64.b64encode(b'{"aistudio_thought_signature":null}').decode(), "agree"
        ),
        _detail(standard, "disagree"),
        _detail(encode_signature_envelope(_B), "disagree", "other"),
        _detail(_B, "invalid-direct"),
    ]
    history = [
        {
            "role": "assistant",
            "tool_calls": [
                _call("agree", 1),
                _call("disagree", 2),
                _call("invalid-direct", 3, thoughtSignature="invalid:direct"),
            ],
            "reasoning_details": details[::-1] if reverse else details,
        }
    ]
    await _assert_replay(
        route,
        stream,
        history,
        [_part("agree", 1, standard), _part("disagree", 2), _part("invalid-direct", 3)],
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("scalar", [None, "", "Think."])
async def test_only_genuinely_idless_encrypted_details_can_be_empty_thoughts(
    route, stream, scalar
):
    details = [
        _detail(_B, "absent"),
        _detail(_A),
        _detail(_B, ""),
        _detail(_C, None),
        _detail(_C, 123),
        {"type": "reasoning.text", "text": "", "signature": _A},
        {"type": "reasoning.summary", "summary": "", "signature": _C},
    ]
    message = {
        "role": "assistant",
        "content": "Visible.",
        "reasoning_details": details,
        **({"reasoning": scalar} if scalar is not None else {}),
    }
    thoughts = (
        [AistudioPart(thought=True, text=scalar)]
        if scalar
        else [
            AistudioPart(thought=True, text="", thought_signature=s)
            for s in (_A, _B, _C, _A, _C)
        ]
    )
    await _assert_replay(
        route, stream, [message], [*thoughts, AistudioPart(text="Visible.")]
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_reused_id_is_scoped_to_its_assistant_turn(route, stream):
    history = [
        {
            "role": "assistant",
            "tool_calls": [_call("again", 1)],
            "reasoning_details": [_detail(_A, "again"), _detail(_C, "later")],
        },
        {"role": "tool", "tool_call_id": "again", "content": "One."},
        {
            "role": "assistant",
            "tool_calls": [_call("again", 2), _call("later", 3)],
            "reasoning_details": [_detail(_B, "again")],
        },
    ]
    await _assert_replay(
        route,
        stream,
        history,
        [_part("again", 1, _A), _part("again", 2, _B), _part("later", 3)],
    )


@pytest.mark.parametrize("cold", [False, True], ids=["warm", "disk-restart"])
async def test_conflicting_details_do_not_override_independent_exact_proof(
    proof_route, monkeypatch, tmp_path, cold
):
    _, gateway = proof_route
    original = _part("observed", 1, _A)
    async with _sdk(gateway.app) as sdk:
        gateway.reply(original)
        result = await _complete(sdk, [PROMPT])
        echo = result.choices[0].message.model_dump(exclude_none=True)
        assert (
            echo["tool_calls"][0].pop("extra_content")["google"]["thought_signature"]
            == _A
        )
        echo["reasoning_details"] = [_detail(_B, "observed"), _detail(_C, "observed")]
        if cold:
            gateway.history.clear()
            tool_signatures.get_signature_cache().flush()
            monkeypatch.setattr(
                tool_signatures,
                "_cache_instance",
                tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json"),
            )
        gateway.reply(AistudioPart(text="Done."))
        reply = await _complete(sdk, [PROMPT, echo, FOLLOWUP])
        assert reply.choices[0].message.content == "Done."
    assert gateway.wire[1][0] == [original.to_wire()]
