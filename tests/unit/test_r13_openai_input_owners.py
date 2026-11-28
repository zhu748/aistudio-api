"""Compatibility fallbacks and scalar display copies are not signed Part owners."""

from copy import deepcopy

import pytest
from test_replay_signature_provenance import (
    _outputs,
    _request,
    _strip_signatures,
    _wire_tools,
    route as route,
)
from test_signed_part_roundtrip import signature

from aistudio_api.api.schemas import GeminiFunctionCall
from aistudio_api.application.compat_shared import encode_signature_envelope
from aistudio_api.domain import tool_signatures
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_MISSING = object()
_PARTIAL = '{"n":'
_A, _B, _C = (signature(c) for c in "abc")
PROMPT: dict[str, object] = {"role": "user", "content": "Start here."}
FOLLOWUP: dict[str, object] = {"role": "user", "content": "Continue."}


def _extra(sig):
    return {"extra_content": {"google": {"thought_signature": sig}}}


@pytest.mark.parametrize("surface", ["chat", "responses"])
async def test_call_arguments_keep_existing_payload_but_only_exact_owners(
    route, surface
):
    client, gateway = route
    is_chat = surface == "chat"
    cases = [
        ({}, {}, True),
        (" {} ", {}, True),
        ({"n": 1}, {"n": 1}, True),
        ('{ "n": 1 }', {"n": 1}, True),
        ({"raw_arguments": _PARTIAL}, {"raw_arguments": _PARTIAL}, True),
        ([1, 2], [1, 2] if is_chat else {}, is_chat),
        ("[1,2]", [1, 2] if is_chat else {}, is_chat),
        (_PARTIAL, {} if is_chat else {"raw_arguments": _PARTIAL}, False),
        *(
            (value, {}, False)
            for value in ("true", True, '"x"', "null", "", " ", None, _MISSING)
        ),
    ]
    calls, expected = [], []
    for i, (raw, args, exact) in enumerate(cases):
        call_id, sig = str(i), signature(str(i))
        args_field = {} if raw is _MISSING else {"arguments": raw}
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "lookup", **args_field},
                **_extra(sig),
            }
            if is_chat
            else {
                "call_id": call_id,
                "type": "function_call",
                "name": "lookup",
                **args_field,
                **_extra(sig),
            }
        )
        expected.append(
            AistudioPart(
                function_call=("lookup", args, call_id),
                thought_signature=sig if exact else None,
            )
        )
    history = [{"role": "assistant", "tool_calls": calls}] if is_chat else calls
    outputs = _outputs(surface, [(str(i), "OK") for i in range(len(cases))])
    gateway.reply(AistudioPart(text="Done."))
    await _request(client, surface, [PROMPT, *history, *outputs])
    assert gateway.wire[1][0] == [p.to_wire() for p in expected]


@pytest.mark.parametrize(
    "surface,raw,original",
    [
        ("chat", "true", {}),
        ("responses", "null", {}),
        ("responses", _PARTIAL, {"raw_arguments": _PARTIAL}),
    ],
)
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "disk-restart"])
@pytest.mark.parametrize("keep_direct", [False, True], ids=["no-direct", "direct"])
async def test_rewritten_argument_collision_cannot_restore_or_unlock_a_later_turn(
    route, monkeypatch, tmp_path, surface, raw, original, cold, keep_direct
):
    client, gateway = route
    gateway.reply(
        AistudioPart(function_call=("lookup", original, "A"), thought_signature=_A),
        AistudioPart(function_call=("lookup", {"n": 1}, "B"), thought_signature=_B),
    )
    batch = await _request(client, surface, [PROMPT])
    results = _outputs(surface, [("A", "OK"), ("B", "OK")])
    gateway.reply(AistudioPart(function_call=("lookup", {}, "C"), thought_signature=_C))
    later = _strip_signatures(
        await _request(client, surface, [PROMPT, *batch, *results])
    )
    rest = [*results, *later, *_outputs(surface, [("C", "OK")])]
    if cold:
        gateway.history.clear()
        tool_signatures.get_signature_cache().flush()
        monkeypatch.setattr(
            tool_signatures,
            "_cache_instance",
            tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json"),
        )

    rewritten = deepcopy(batch)
    if surface == "chat":
        items = rewritten[0]["tool_calls"]
        assert isinstance(items, list) and isinstance(items[0], dict)
        item = items[0]
        item["function"]["arguments"] = raw
    else:
        item = rewritten[0]
        item["arguments"] = raw
    if not keep_direct:
        item.pop("extra_content")
    # Neither the conflicting turn nor its descendants can supply a cache
    # context for new captures, even if a signed response follows.
    follow_sig = signature("never-observed-under-the-clean-prefix")
    gateway.reply(
        AistudioPart(function_call=("lookup", {}, "D"), thought_signature=follow_sig)
    )
    follow = _strip_signatures(
        await _request(client, surface, [PROMPT, *rewritten, *rest])
    )
    assert _wire_tools(gateway, 1, "function_call") == [
        ("A", "lookup", original, ""),
        ("B", "lookup", {"n": 1}, _B),
    ]
    assert _wire_tools(gateway, 3, "function_call") == [("C", "lookup", {}, "")]

    # The independent exact transcript still recovers the original batch and
    # its legitimate child. The capture after the taint was not stored here.
    gateway.reply(AistudioPart(text="Done."))
    await _request(
        client,
        surface,
        [
            PROMPT,
            *_strip_signatures(batch),
            *rest,
            *follow,
            *_outputs(surface, [("D", "OK")]),
        ],
    )
    assert _wire_tools(gateway, 1, "function_call") == [
        ("A", "lookup", original, _A),
        ("B", "lookup", {"n": 1}, _B),
    ]
    assert _wire_tools(gateway, 3, "function_call") == [("C", "lookup", {}, _C)]
    assert _wire_tools(gateway, 5, "function_call")[0][-1] != follow_sig


def _detail(kind, text, sig=None, fmt="google-gemini-v1"):
    return {
        "type": f"reasoning.{kind}",
        "format": fmt,
        **({kind: text} if text is not _MISSING else {}),
        **({"signature": sig} if sig is not None else {}),
    }


@pytest.mark.parametrize("alias", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize(
    "scalar_extra", [{}, _extra("invalid:scalar")], ids=["absent", "invalid"]
)
async def test_unsigned_reasoning_display_mirrors_only_original_typed_owners(
    route, alias, scalar_extra
):
    client, gateway = route
    # A self-identifying envelope and the explicit Google format are both
    # scoped; an unrelated encrypted entry or a missing text is not an owner.
    details = [
        _detail("text", "A", _A),
        {
            "type": "reasoning.encrypted",
            "id": "orphan",
            "format": "google-gemini-v1",
            "data": _C,
        },
        _detail("text", _MISSING, _C),
        _detail("summary", "", _B),
        _detail("text", " 🎈 "),
        _detail("summary", "B", encode_signature_envelope(_C), "other"),
    ]
    gateway.reply(AistudioPart(text="Done."))
    await _request(
        client,
        "chat",
        [
            PROMPT,
            {
                "role": "assistant",
                "content": "Answer",
                alias: "A 🎈 B",
                **scalar_extra,
                "reasoning_details": details,
            },
            FOLLOWUP,
        ],
    )
    assert gateway.wire[1][0] == [
        AistudioPart(thought=True, text=t, thought_signature=s).to_wire()
        for t, s in [("A", _A), ("", _B), (" 🎈 ", None), ("B", _C)]
    ] + [AistudioPart(text="Answer").to_wire()]


@pytest.mark.parametrize(
    "message,details,expected",
    [
        (
            {"reasoning": "A B"},
            [_detail("text", "A", _A), _detail("text", "B", _B)],
            [("A B", None)],
        ),
        (
            {"reasoning": "A B", **_extra("invalid:scalar")},
            [_detail("text", "A", _A), _detail("text", "B", _B)],
            [("A B", None)],
        ),
        (
            {"reasoning": "AB", **_extra(_C)},
            [_detail("text", "A", _A), _detail("text", "B", _B)],
            [("AB", _C)],
        ),
        (
            {"reasoning_content": "", "reasoning": "AB", **_extra(_C)},
            [_detail("text", "AB", _A)],
            [("", _C)],
        ),
        (
            {"reasoning": "AB"},
            [_detail("text", "A"), _detail("text", "B", _B, "anthropic-v1")],
            [("AB", None)],
        ),
        (
            {"reasoning": "AB"},
            [_detail("text", "A", _A, ""), _detail("text", "B")],
            [("AB", None)],
        ),
        (
            {},
            [
                _detail("text", _MISSING, _A),
                _detail("summary", None, _A),
                _detail("text", 3, _A),
            ],
            [],
        ),
        ({}, [_detail("summary", "", _B)], [("", _B)]),
    ],
    ids=[
        "mismatch",
        "invalid-scalar-mismatch",
        "signed-scalar",
        "signed-empty-scalar",
        "foreign",
        "unscoped",
        "absent-not-empty",
        "signed-empty-detail",
    ],
)
async def test_reasoning_non_mirrors_preserve_existing_scalar_or_explicit_text(
    route, message, details, expected
):
    client, gateway = route
    gateway.reply(AistudioPart(text="Done."))
    await _request(
        client,
        "chat",
        [
            PROMPT,
            {
                "role": "assistant",
                "content": "Answer",
                **message,
                "reasoning_details": details,
            },
            FOLLOWUP,
        ],
    )
    assert gateway.wire[1][0] == [
        AistudioPart(thought=True, text=t, thought_signature=s).to_wire()
        for t, s in expected
    ] + [AistudioPart(text="Answer").to_wire()]


def test_rewrite_provenance_is_not_a_client_field_or_a_wire_value():
    data = {"name": "lookup", "args": {}, "id": "A"}
    private = "_compat_args_rewritten"
    call = GeminiFunctionCall.model_validate({**data, private: True})
    assert not call._compat_args_rewritten
    call._compat_args_rewritten = True
    assert call.model_copy(deep=True)._compat_args_rewritten
    assert call.model_dump() == data
    assert call.model_fields_set == set(data)
    assert private not in GeminiFunctionCall.model_json_schema()["properties"]
    ordinary = AistudioPart(function_call=("lookup", {}, "A"))
    rewritten = AistudioPart(
        function_call=("lookup", {}, "A"), _compat_args_rewritten=True
    )
    assert rewritten == ordinary and rewritten.to_wire() == ordinary.to_wire()
