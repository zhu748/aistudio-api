"""A signed JSON shard may use different string escaping than its final object."""

import pytest
from test_ordered_tool_routes import (
    call,
    chunk,
    input_request,
    native_parts,
    sse_events,
    wire,
)
from test_signed_part_roundtrip import route as route, signature

from aistudio_api.application.stream_utils import ToolResponseAssembler

SIG = signature("original shard")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("partial", ['{"city":"Mün', r'{"city":"M\u00fcn'])
async def test_native_snapshot_preserves_only_the_original_call_marker(
    route, stream, partial
):
    args = {"city": "München"}
    route.wire.raw = wire(
        chunk([call("weather", partial, "A", signature=SIG)]),
        chunk([call("weather", args, "B"), call("weather", args, "A")], 1),
    )
    path, request = input_request("native", stream)
    response = await route.raw.post(path, json=request)
    assert response.status_code == 200, response.text
    events = sse_events(response.text) if stream else [response.json()]
    assert native_parts(events) == [
        {
            "functionCall": {"name": "weather", "args": args, "id": "A"},
            "thoughtSignature": SIG,
        },
        {"functionCall": {"name": "weather", "args": args, "id": "B"}},
    ]


def _assemble(shards, snapshot, *, first_id="A", last_id="A", last_name="weather"):
    assembler = ToolResponseAssembler()
    for i, args in enumerate(shards):
        assembler.feed(
            [
                {
                    "functionCall": {"name": "weather", "args": args, "id": first_id},
                    **({"thoughtSignature": SIG} if i == 0 else {}),
                }
            ]
        )
    assembler.feed(
        [{"functionCall": {"name": last_name, "args": snapshot, "id": last_id}}]
    )
    return assembler.finish("STOP")


@pytest.mark.parametrize(
    ("shards", "snapshot"),
    [
        ([r'{ "\u0063ity" : "北\u4E', "AC"], {"city": "北京!"}),
        ([r'{"v":"\uD83D', r"\uDE80"], {"v": "🚀!"}),
        ([r'{"v":"a\/b\u000Ac\u0022d\u005Ce'], {"v": 'a/b\nc"d\\end'}),
        (
            [r'{"nested":[true,12,{"\u0076":"M\u00FCn"}],'],
            {"nested": [True, 12, {"v": "Mün"}], "z": 0},
        ),
    ],
)
def test_stable_shard_matches_complete_scalars_across_fragments(shards, snapshot):
    parts, reason = _assemble(shards, snapshot)
    assert reason == "STOP"
    assert parts == [
        {
            "functionCall": {"name": "weather", "id": "A", "args": snapshot},
            "thoughtSignature": SIG,
        }
    ]


@pytest.mark.parametrize(
    ("partial", "snapshot"),
    [
        (r'{"city":"M\u00fcn', {"city": "Mülheim"}),
        (r'{"city":"e\u0301', {"city": "é"}),
        (r'{"v":"\u0031', {"v": 1}),
        (r'{"v":1, "\u0078"', {"v": True, "x": 0}),
        (r'{"v":t rue', {"v": True}),
        (r'{"v":1 2', {"v": 12}),
        (r'{"v":"M\u00fc', {"v": "München", "invalid": float("inf")}),
        (r'{"city":"\u00G0', {"city": "München"}),
        (r'{ "\u0063ity" : "北\u4E', {"city": "北京!"}),
        (r'{"city":"\x4D', {"city": "München"}),
        ('{"city":"line\\', {"city": "line\n"}),
        (r'{"v":"\uD83D', {"v": "🚀"}),
        (r'{"v":"\uDE80', {"v": "🚀"}),
        (r'{"v":"\u0022,', {"v": "", "n": 0}),
    ],
)
def test_changed_or_ambiguous_prefix_cannot_transfer_marker(partial, snapshot):
    parts, reason = _assemble([partial], snapshot)
    assert reason == "STOP"
    assert parts == [{"functionCall": {"name": "weather", "id": "A", "args": snapshot}}]


@pytest.mark.parametrize(
    ("first_id", "last_id", "last_name", "reason"),
    [
        (None, "A", "weather", "STOP"),  # newly promoted, not a signed ID
        ("A", None, "weather", "MALFORMED_FUNCTION_CALL"),
        (None, None, "weather", "MALFORMED_FUNCTION_CALL"),
        ("A", "B", "weather", "MALFORMED_FUNCTION_CALL"),
        ("A", "A", "weather_now", "STOP"),
    ],
)
def test_equivalence_does_not_establish_call_identity(
    first_id, last_id, last_name, reason
):
    parts, actual_reason = _assemble(
        [r'{"city":"M\u00fcn'],
        {"city": "München"},
        first_id=first_id,
        last_id=last_id,
        last_name=last_name,
    )
    assert actual_reason == reason
    assert all("thoughtSignature" not in part for part in parts)


def test_late_name_and_id_cannot_retroactively_identify_earlier_shards():
    assembler = ToolResponseAssembler()
    assembler.feed([{"functionCall": {"name": "weather", "args": r'{"city":"M\u00fc'}}])
    call = {"name": "weather", "id": "A"}
    assembler.feed([{"functionCall": {**call, "args": "n"}, "thoughtSignature": SIG}])
    assembler.feed([{"functionCall": {**call, "args": {"city": "München"}}}])
    assert assembler.finish("STOP") == (
        [{"functionCall": {**call, "args": {"city": "München"}}}],
        "STOP",
    )
