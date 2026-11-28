"""An ID-less Responses function output can name an already pending call."""

import openai
import pytest
from openai.types.responses.response_input_param import FunctionCallOutput
from test_compat_media_routes import _PNG, _client_options, _serialized_contents
from test_r15_result_owners import _assert_replay, _call
from test_r23_responses_state_refs import _turn
from test_signed_part_roundtrip import route as route, signature

from aistudio_api.domain.tool_signatures import tool_history_text
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_NAME = "read_note"
_RAW = ' {"sample": "藍", "call_id": "C"}\n'
_PAYLOAD = {"result": _RAW}


@pytest.mark.parametrize("stream", [False, True])
async def test_named_results_match_the_all_id_replay_after_exact_reservations(
    route, stream
):
    wire = _serialized_contents(route)
    labels = {"A": _NAME, "B": _NAME, "C": "unknown"}
    calls = [
        AistudioPart(
            function_call=(name, {"sample": key}, key), thought_signature=signature(key)
        )
        for key, name in labels.items()
    ]
    prompt = {"role": "user", "content": "Compare the three notes."}
    followup = {"role": "user", "content": "Which note came from B?"}
    tools = [
        {"type": "function", "name": name, "parameters": {"type": "object"}}
        for name in (_NAME, "unknown")
    ]
    expected_results = [
        AistudioPart(function_response=(_NAME, {"result": "Note B"}, "B")),
        AistudioPart(inline_data=("image/png", _PNG["B"])),
        AistudioPart(inline_data=("text/plain", "Yg=="), inline_filename="B.txt"),
        AistudioPart(function_response=("unknown", {"result": "Note C"}, "C")),
        AistudioPart(function_response=(_NAME, _PAYLOAD, "A")),
        AistudioPart(text=followup["content"]),
    ]
    route.wire.reply(*calls)
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **_client_options(route)
    ) as sdk:
        turn = await _turn(sdk, stream, input=[prompt], tools=tools)
        assert [(item.call_id, item.name) for item in turn.output] == list(
            labels.items()
        )
        for all_ids in (True, False):
            b = FunctionCallOutput(
                type="function_call_output",
                name=_NAME,
                output=[
                    {"type": "input_text", "text": "Note B"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + _PNG["B"],
                    },
                    {
                        "type": "input_file",
                        "filename": "B.txt",
                        "file_data": "data:text/plain;base64,Yg==",
                    },
                ],
            )
            c = FunctionCallOutput(
                type="function_call_output", name="unknown", output="Note C"
            )
            if all_ids:
                b["call_id"], c["call_id"] = "B", "C"
            # A arrives last and contradicts its display name. Reserve it by ID
            # before deciding whether the earlier result can belong to B.
            a = FunctionCallOutput(
                type="function_call_output", call_id="A", name="unknown", output=_RAW
            )
            route.wire.reply(AistudioPart(text="Recorded."))
            reply = await _turn(
                sdk,
                stream,
                input=[prompt, *turn.output, b, c, a, followup],
                tools=tools,
            )
            assert reply.output_text == "Recorded."
            assert wire[-1] == [
                [[AistudioPart(text=prompt["content"]).to_wire()], "user"],
                [[part.to_wire() for part in calls], "model"],
                [[part.to_wire() for part in expected_results], "user"],
            ]
    assert len(wire) == 3


def _orphan(name="unknown", key=None):
    return AistudioPart(text=tool_history_text("tool_result", name, key, _PAYLOAD))


@pytest.mark.parametrize("stream", [False, True])
async def test_idless_hint_preserves_existing_ambiguity_and_fallbacks(
    monkeypatch, stream
):
    one = [(_NAME, "A")]
    idless = [(_NAME, None)]
    owned = AistudioPart(function_response=(_NAME, _PAYLOAD, "A"))
    cases = [
        # A supplied ID, even an unusable one, is not a request to match by name.
        (idless, {"call_id": "missing", "name": _NAME}, _orphan(key="missing")),
        (idless, {"call_id": " ", "name": _NAME}, _orphan(key=" ")),
        (idless, {"call_id": False, "name": _NAME}, _orphan()),
        (
            [(_NAME, "dup"), ("unknown", "dup")],
            {"call_id": "dup", "name": _NAME},
            _orphan(key="dup"),
        ),
        # Only an unqualified result can supply a hint for a plain function.
        (one, {"namespace": "notes", "name": _NAME}, _orphan()),
        (one, {"namespace": [], "name": _NAME}, _orphan()),
        ([("lookup", "Q", "archive")], {"name": "lookup"}, _orphan()),
        ([("tag", None, [])], {"name": "tag"}, _orphan()),
        (
            [("lookup", "Q", "archive")],
            {"call_id": "Q", "namespace": "archive", "name": _NAME},
            AistudioPart(function_response=("lookup", _PAYLOAD, "Q")),
        ),
        (one, {"call_id": "A", "namespace": [], "name": {}}, owned),
        (one, {"call_id": "", "namespace": None, "name": _NAME}, owned),
        (one, {"call_id": None, "namespace": "", "name": _NAME}, owned),
        (one, {"name": " Read_Note "}, _orphan(" Read_Note ")),
        (one, {"name": ["read_note"]}, _orphan()),
        (one, {}, _orphan()),
        (
            [("unknown", "C")],
            {},
            AistudioPart(function_response=("unknown", _PAYLOAD, "C")),
        ),
        # With no exact reservation the existing matcher never guesses A or B.
        (
            [*one, (_NAME, "B")],
            {"name": _NAME},
            AistudioPart(function_response=(_NAME, _PAYLOAD)),
        ),
    ]
    draft, _ = _call("responses", _NAME, -1, "unfinished")
    steps = [({**draft, "namespace": "archive", "status": "in_progress"}, [])]
    for row, (calls, fields, expected) in enumerate(cases):
        for name, key, *namespace in calls:
            call, parts = _call("responses", name, row, key)
            if namespace:
                call["namespace"] = namespace[0]
            steps.append((call, parts))
        result = {"type": "function_call_output", "output": _RAW, **fields}
        steps.append((result, [expected]))
    await _assert_replay(monkeypatch, "responses", stream, steps)
