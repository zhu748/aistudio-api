"""Only original positional wire strings can disambiguate nested argument shards."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from test_ordered_tool_routes import (
    PROTOCOLS,
    call,
    chunk,
    input_request,
    native_parts,
    sse_events,
    tool_calls,
    wire,
)
from test_signed_part_roundtrip import route as route, signature

from aistudio_api.api.responses import encode_response_part
from aistudio_api.application import stream_utils
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_part

OUTER, INNER, PEER = (signature(label) for label in ("outer", "inner", "peer"))


async def _request(route, protocol, stream, frames):
    route.wire.raw = wire(*frames)
    path, params = input_request(protocol, stream)
    response = await route.raw.post(path, json=params)
    assert response.status_code == 200, response.text
    return sse_events(response.text) if stream else [response.json()]


def _markers(value):
    if isinstance(value, dict):
        if "functionCall" in value:
            yield value["functionCall"]["id"], value.get("thoughtSignature")
        if value.get("type") in ("function", "function_call", "tool_use"):
            yield (
                value.get("call_id") or value.get("id"),
                value.get("extra_content", {})
                .get("google", {})
                .get("thought_signature"),
            )
        for entry in value.values():
            yield from _markers(entry)
    elif isinstance(value, list):
        for entry in value:
            yield from _markers(entry)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("prefix", "middle", "expected"),
    [
        ('{"filter":', '{"active":true}', {"filter": {"active": True}}),
        ('{"items":', '["é",2]', {"items": ["é", 2]}),
    ],
)
async def test_wire_nested_arguments_and_proof_reach_each_public_route(
    route, protocol, stream, prefix, middle, expected
):
    events = await _request(
        route,
        protocol,
        stream,
        [
            chunk(
                [
                    call("inspect", prefix, "A", signature=OUTER),
                    call("inspect", {}, "B", signature=PEER),
                ]
            ),
            chunk([call("inspect", middle, "A", signature=INNER)]),
            chunk([call("inspect", "}", "A")], 1),
        ],
    )
    assert tool_calls(protocol, stream, events) == [
        {"id": "A", "name": "inspect", "args": expected},
        {"id": "B", "name": "inspect", "args": {}},
    ]
    assert {(cid, marker) for cid, marker in _markers(events) if marker} == {
        ("A", OUTER),
        ("B", PEER),
    }
    assert INNER not in str(events)


def _assemble(parts, finish="STOP"):
    assembler = stream_utils.ToolResponseAssembler()
    for part in parts:
        assembler.feed([encode_response_part(parse_response_part(part))])
    return assembler.finish(finish)


def _part(args, marker=None):
    return {
        "functionCall": {"id": "A", "name": "inspect", "args": args},
        **({"thoughtSignature": marker} if marker else {}),
    }


@pytest.mark.parametrize(
    ("pieces", "args", "marker"),
    [
        (['{"filter":', '{"x":1}'], {"x": 1}, INNER),  # genuine signed replacement
        (['{"filter":', {"x": 1}], {"x": 1}, INNER),  # typed snapshot is final
        (['{"n":', '{"n":1', "}"], {"n": 1}, INNER),  # genuine cumulative partial
        (['{"n":', '{"n":1}'], {"n": 1}, INNER),
        (['{"n":', '{"n":', '{"n":1}}}'], {"n": {"n": {"n": 1}}}, OUTER),
        (['{"literal":"', "  { } ", '"}'], {"literal": "  { } "}, OUTER),
        (['{"a":', "{}", ',"b":', '{"x":1}', "}"], {"a": {}, "b": {"x": 1}}, OUTER),
        (
            ['{"a":[', '{"x":1}', ",", '{"x":2}', "]}"],
            {"a": [{"x": 1}, {"x": 2}]},
            OUTER,
        ),
    ],
)
def test_shards_compete_with_real_replacement_and_cumulative_updates(
    pieces, args, marker
):
    assert _assemble(
        [
            call(
                "inspect",
                value,
                "A",
                signature=OUTER if i == 0 else INNER if i == 1 else None,
            )
            for i, value in enumerate(pieces)
        ]
    ) == ([_part(args, marker)], "STOP")


def test_inner_only_marker_never_signs_the_outer_arguments():
    assert _assemble(
        [
            call("inspect", '{"f":', "A"),
            call("inspect", "{}", "A", signature=INNER),
            call("inspect", "}", "A"),
        ]
    ) == ([_part({"f": {}})], "STOP")


@pytest.mark.parametrize(
    "variant",
    [
        "typed",
        "forged",
        "sparse-name",
        "promoted-id",
        "changed-name",
        "different-id",
        "different-envelope",
        "isolated-array",
        "prior-invalid-type",
        "trailing-junk",
    ],
)
def test_unproven_shape_or_identity_never_revives_an_entire_tool_batch(variant):
    first = call("inspect", '{"f":', "A", signature=OUTER)
    middle = call("inspect", '{"ok":true}', "A", signature=INNER)
    last = call("inspect", "}", "A")
    frames = [first, middle, last]
    if variant == "typed":
        middle[3][1] = {"ok": True}
    elif variant == "forged":
        middle[3] = {
            "id": "A",
            "name": "inspect",
            "args": {"ok": True},
            "raw": ["inspect", '{"ok":true}', "A"],
        }
    elif variant == "sparse-name":
        last[3][0] = None
    elif variant == "promoted-id":
        first[3][2] = None
    elif variant == "changed-name":
        middle[3][0] = "inspect_more"
    elif variant == "different-id":
        last[3][2] = "C"
    elif variant == "different-envelope":
        middle[1] = "Changed caption."
    elif variant == "isolated-array":
        frames = [call("inspect", "[1,2]", "A")]
    elif variant == "prior-invalid-type":
        frames.insert(0, call("inspect", 3, "A"))
    else:
        last[3][1] = "}!"
    parts, finish = _assemble([*frames, call("peer", {}, "B", signature=PEER)])
    assert finish == "MALFORMED_FUNCTION_CALL"
    assert not any(p.get("functionCall") for p in parts)
    assert all(marker not in str(parts) for marker in (OUTER, INNER, PEER))


@pytest.mark.parametrize("finish", ["OTHER", "MAX_TOKENS", "SAFETY"])
def test_only_confirmed_stop_can_publish_even_a_proven_literal(finish):
    assert _assemble(
        [
            call("inspect", piece, "A", signature=OUTER)
            for piece in ('{"f":', "{}", "}")
        ],
        finish,
    ) == ([], finish)


@pytest.mark.parametrize(
    "tail",
    [
        ',"v":NaN}',
        ',"v":Infinity}',
        ',"v":1e400}',
        ',"f":0}',
        r',"s":"\ud800"}',
    ],
)
def test_recovered_object_is_strict_json_with_unambiguous_keys(tail):
    assert _assemble(
        [call("inspect", piece, "A") for piece in ('{"f":', "{}", tail)]
    ) == ([], "MALFORMED_FUNCTION_CALL")


async def test_generic_sse_and_function_response_do_not_acquire_wire_provenance():
    # Even real positional data loses the non-JSON provenance when serialized.
    async def source() -> AsyncIterator[dict[str, object]]:
        for piece in ('{"f":', "{}", "}"):
            part = encode_response_part(
                parse_response_part(call("inspect", piece, "A"))
            )
            yield {
                "candidates": [{"content": {"parts": [json.loads(json.dumps(part))]}}]
            }
        yield {
            "candidates": [
                {"content": {"parts": [{"text": "kept"}]}, "finishReason": "STOP"}
            ]
        }

    events = [event async for event in stream_utils.coalesce_tool_frames(source())]
    assert native_parts(events) == [{"text": "kept"}]
    candidates = events[-1]["candidates"]
    assert isinstance(candidates, list)
    assert candidates[0]["finishReason"] == "MALFORMED_FUNCTION_CALL"
    response = [None, None, None, None, ["inspect", "[1,2]", "A"]]
    assert encode_response_part(parse_response_part(response)) == {
        "functionResponse": {"name": "inspect", "id": "A", "response": [1, 2]}
    }


@pytest.mark.parametrize(
    ("limit", "value"),
    [
        ("_MAX_TOOL_ARGUMENT_FRAGMENTS", 2),
        ("_MAX_TOOL_ARGUMENT_BYTES", 10),
    ],
)
def test_optional_tape_respects_limits_without_displacing_a_valid_snapshot(
    monkeypatch, limit, value
):
    monkeypatch.setattr(stream_utils, limit, value)
    prefix = [call("inspect", '{"field":', "A"), call("inspect", "{}", "A")]
    assert _assemble(prefix) == ([_part({})], "STOP")
    assert _assemble([*prefix, call("inspect", "}", "A")]) == (
        [],
        "MALFORMED_FUNCTION_CALL",
    )


def test_ordinary_deferred_content_can_reclaim_optional_response_budget(monkeypatch):
    monkeypatch.setattr(stream_utils, "_MAX_TOOL_RESPONSE_BYTES", 110)
    prefix = '{"' + "p" * 60 + '":'
    text = "x" * 50
    assert _assemble(
        [call("inspect", prefix, "A"), call("inspect", "{}", "A"), [None, text]]
    ) == ([_part({}), {"text": text}], "STOP")


def test_many_inner_snapshots_parse_the_literal_only_once(monkeypatch):
    decodes = []
    decode = stream_utils._LITERAL_DECODER.decode

    def observed(s):
        decodes.append(len(s))
        return decode(s)

    monkeypatch.setattr(stream_utils._LITERAL_DECODER, "decode", observed)
    assembler = stream_utils.ToolResponseAssembler()
    pieces = ['{"items":[', "{}", *([",", "{}"] * 200), "]}"]
    for piece in pieces:
        assembler.feed(
            [encode_response_part(parse_response_part(call("inspect", piece, "A")))]
        )
    assert decodes == []
    assert assembler.finish("STOP") == ([_part({"items": [{}] * 201})], "STOP")
    assert decodes == [sum(map(len, pieces))]
