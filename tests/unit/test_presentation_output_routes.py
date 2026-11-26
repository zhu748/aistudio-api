"""Stable-ID presentation fragments restore text without disturbing ordinary tools."""

import json

import httpx
import pytest
import test_anti_truncation_variants as variants
import test_ordered_tool_routes as wire_routes

from aistudio_api.api.app import app

wire_gateway = wire_routes.wire_source
selected_variant = variants.variant_config
_MODES = [
    ("-at", "generateContent"),
    ("-at", "streamGenerateContent"),
    ("-at-stream", "streamGenerateContent"),
]


async def invoke(variant, action):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1beta/models/gemini-3.7-flash{variant}:{action}",
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Report the weather."}]}
                ],
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "weather",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                },
                            }
                        ]
                    }
                ],
            },
        )
    assert response.status_code == 200
    frames = (
        [response.json()]
        if action == "generateContent"
        else [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
    )
    return wire_routes.native_parts(frames), [
        c["finishReason"]
        for f in frames
        for c in f["candidates"]
        if c.get("finishReason")
    ]


@pytest.mark.parametrize(("variant", "action"), _MODES)
@pytest.mark.parametrize(
    "shape", ["fragments", "sparse", "snapshots", "ordered", "interrupted"]
)
async def test_presentation_keeps_text_identity_and_wire_position(
    wire_gateway, selected_variant, variant, action, shape
):
    call, chunk = wire_routes.call, wire_routes.chunk
    if shape == "ordered":
        parts = [
            call("router_emit", {"content": "before"}, "A"),
            call("weather", {"city": "Paris"}, "W"),
            call("router_emit", {"content": "after"}, "B"),
        ]
    elif shape == "snapshots":
        parts = [
            call("router_emit", {"content": "hello "}, "A"),
            call("router_emit", {"content": "hello world"}, "A"),
        ]
    else:
        parts = [
            call("router_emit", '{"content":"hello ', "A"),
            call(None if shape == "sparse" else "router_emit", 'world"}', "A"),
        ]
        if shape == "interrupted":
            parts.append(call("weather", {"city": "Paris"}, "W"))
    finish = 2 if shape == "interrupted" else 1
    wire_gateway.raw = wire_routes.wire(
        *(
            chunk([part], finish if i == len(parts) - 1 else None)
            for i, part in enumerate(parts)
        )
    )
    actual, terminals = await invoke(variant, action)
    assert terminals == ["MAX_TOKENS" if shape == "interrupted" else "STOP"]
    if shape == "ordered":
        assert actual == [
            {"text": "before"},
            {"functionCall": {"id": "W", "name": "weather", "args": {"city": "Paris"}}},
            {"text": "after"},
        ]
    else:
        assert all(p.keys() == {"text"} for p in actual)
        assert "".join(p["text"] for p in actual) == "hello world"


@pytest.mark.parametrize(("variant", "action"), _MODES)
@pytest.mark.parametrize("sparse", [False, True])
@pytest.mark.parametrize("same_frame", [False, True])
async def test_interleaved_presentation_ids_leave_real_call_in_its_first_slot(
    wire_gateway, selected_variant, variant, action, sparse, same_frame
):
    call, chunk = wire_routes.call, wire_routes.chunk
    ordinary_signature = "b3JkaW5hcnk="
    detached_signature = "ZGV0YWNoZWQ="
    continuation = None if sparse else "router_emit"
    parts = [
        call("router_emit", '{"content":"a', "A", signature="ZGlzcGxheQ=="),
        call("weather", '{"city":"Ba', "W", signature=ordinary_signature),
        call("router_emit", '{"content":"b', "B"),
        wire_routes.text(None, signature=detached_signature),
        call(continuation, 'c"}', "A"),
        call(None, 'th"}', "W"),
        call(continuation, 'd"}', "B"),
    ]
    wire_gateway.raw = wire_routes.wire(
        *(
            [chunk(parts, 1)]
            if same_frame
            else [
                chunk([part], 1 if i == len(parts) - 1 else None)
                for i, part in enumerate(parts)
            ]
        )
    )
    assert await invoke(variant, action) == (
        [
            {"text": "a"},
            {
                "functionCall": {
                    "id": "W",
                    "name": "weather",
                    "args": {"city": "Bath"},
                },
                "thoughtSignature": ordinary_signature,
            },
            {"text": "b"},
            {"thoughtSignature": detached_signature},
            {"text": "c"},
            {"text": "d"},
        ],
        ["STOP"],
    )


@pytest.mark.parametrize(("variant", "action"), _MODES)
@pytest.mark.parametrize(
    "arguments",
    [
        '{"content":"tide',
        '{"content":"tide"',
        '{"content":"tide"} x',
        {"content": 42},
        {},
    ],
)
async def test_stop_does_not_certify_invalid_selected_presentation(
    wire_gateway, selected_variant, variant, action, arguments
):
    wire_gateway.raw = wire_routes.wire(
        wire_routes.chunk([wire_routes.call("router_emit", arguments, "A")], 1)
    )
    parts, reasons = await invoke(variant, action)
    assert reasons == ["MALFORMED_FUNCTION_CALL"]
    assert all(set(part) == {"text"} for part in parts)


@pytest.mark.parametrize(("variant", "action"), _MODES)
@pytest.mark.parametrize("tool_first", [False, True])
async def test_all_selected_presentation_calls_must_finish_before_releasing_a_tool(
    wire_gateway, selected_variant, variant, action, tool_first
):
    call, chunk = wire_routes.call, wire_routes.chunk
    first = call("router_emit", {"content": "before"}, "A")
    ordinary = call("weather", {"city": "Bath"}, "W")
    partial = call("router_emit", '{"content":"unfinished', "B")
    parts = [ordinary, first, partial] if tool_first else [first, partial, ordinary]
    wire_gateway.raw = wire_routes.wire(
        *(chunk([part], 1 if i == 2 else None) for i, part in enumerate(parts))
    )
    actual, reasons = await invoke(variant, action)
    assert reasons == ["MALFORMED_FUNCTION_CALL"]
    assert actual == (
        [{"text": "before"}, {"text": "unfinished"}]
        if variant == "-at-stream"
        else [{"text": "before"}]
    )


@pytest.mark.parametrize(("variant", "action"), _MODES)
@pytest.mark.parametrize(
    ("finish", "reason"),
    [
        (2, "MAX_TOKENS"),
        (3, "SAFETY"),
        (10, "MALFORMED_FUNCTION_CALL"),
        (12, "UNEXPECTED_TOOL_CALL"),
        (13, "TOO_MANY_TOOL_CALLS"),
    ],
)
async def test_complete_presentation_keeps_provider_nonstop(
    wire_gateway, selected_variant, variant, action, finish, reason
):
    wire_gateway.raw = wire_routes.wire(
        wire_routes.chunk(
            [wire_routes.call("router_emit", {"content": "tide"}, "A")], finish
        )
    )
    assert await invoke(variant, action) == ([{"text": "tide"}], [reason])


@pytest.mark.parametrize(("variant", "action"), _MODES)
async def test_invalid_display_does_not_downgrade_winning_ordinary_text(
    wire_gateway, selected_variant, variant, action
):
    wire_gateway.raw = wire_routes.wire(
        wire_routes.chunk([wire_routes.text("ordinary")]),
        wire_routes.chunk(
            [wire_routes.call("router_emit", '{"content":"tide', "A")], 1
        ),
    )
    assert await invoke(variant, action) == ([{"text": "ordinary"}], ["STOP"])


@pytest.mark.parametrize(("variant", "action"), _MODES)
async def test_sparse_presentation_needs_exact_preexisting_id(
    wire_gateway, selected_variant, variant, action
):
    call, chunk = wire_routes.call, wire_routes.chunk
    wire_gateway.raw = wire_routes.wire(
        chunk([call("router_emit", '{"content":"a', "A")]),
        chunk([call(None, 'wrong"}', "other-A")]),
        chunk([call(None, 'c"}', "A")], 1),
    )
    parts, reasons = await invoke(variant, action)
    assert parts == [{"text": "a"}, {"text": "c"}]
    assert reasons == ["MALFORMED_FUNCTION_CALL"]


@pytest.mark.parametrize(("variant", "action"), _MODES)
@pytest.mark.parametrize(
    ("limit", "value"),
    [
        ("_MAX_PRESENTATION_CALLS", 1),
        ("_MAX_PRESENTATION_IDS", 1),
        ("_MAX_PRESENTATION_ID_CHARS", 1),
        ("_MAX_PRESENTATION_DELTAS", 1),
        ("_MAX_PRESENTATION_TOTAL_BYTES", 5),
        ("_MAX_PRESENTATION_CALL_BYTES", 5),
    ],
)
async def test_presentation_limits_never_report_confirmed_stop(
    wire_gateway, selected_variant, monkeypatch, variant, action, limit, value
):
    from aistudio_api.application import anti_truncation

    monkeypatch.setattr(anti_truncation, limit, value, raising=False)
    call, chunk = wire_routes.call, wire_routes.chunk
    wire_gateway.raw = wire_routes.wire(
        chunk([call("router_emit", {"content": "a"}, "A")]),
        chunk([call("router_emit", {"content": "échoé"}, "BB")], 1),
    )
    parts, reasons = await invoke(variant, action)
    assert parts == [{"text": "a"}]
    assert reasons == ["MALFORMED_FUNCTION_CALL"]


@pytest.mark.parametrize(("variant", "action"), _MODES)
async def test_known_ordinary_id_cannot_be_borrowed_by_presentation(
    wire_gateway, selected_variant, variant, action
):
    call, chunk = wire_routes.call, wire_routes.chunk
    wire_gateway.raw = wire_routes.wire(
        chunk([wire_routes.text("ordinary"), call("weather", '{"city":"Ba', "W")]),
        chunk([call("router_emit", {"content": "not the answer"}, "W")]),
        chunk([call(None, 'th"}', "W")], 1),
    )
    assert await invoke(variant, action) == (
        [
            {"text": "ordinary"},
            {"functionCall": {"id": "W", "name": "weather", "args": {"city": "Bath"}}},
        ],
        ["STOP"],
    )


@pytest.mark.parametrize(("variant", "action"), _MODES)
async def test_parser_structural_budget_is_shared_before_any_display_text(
    wire_gateway, selected_variant, monkeypatch, variant, action
):
    from aistudio_api.application import anti_truncation

    monkeypatch.setattr(anti_truncation, "_MAX_PRESENTATION_PARSER_FRAMES", 4)
    call, chunk = wire_routes.call, wire_routes.chunk
    wire_gateway.raw = wire_routes.wire(
        chunk([call("router_emit", '{"extra":[[', "A")]),
        chunk([call("router_emit", '{"extra":[[', "B")]),
        chunk([call(None, ']],"content":"a"}', "A")]),
        chunk([call(None, ']],"content":"b"}', "B")], 1),
    )
    assert await invoke(variant, action) == (
        [{"text": "a"}],
        ["MALFORMED_FUNCTION_CALL"],
    )


@pytest.mark.parametrize(
    ("finish", "reason"), [(1, "MALFORMED_FUNCTION_CALL"), (2, "MAX_TOKENS")]
)
async def test_indeterminate_name_metadata_is_bounded_before_display(
    wire_gateway, selected_variant, monkeypatch, finish, reason
):
    from aistudio_api.application import anti_truncation

    monkeypatch.setattr(anti_truncation, "_MAX_PRESENTATION_PREFIX_BYTES", 1)
    wire_gateway.raw = wire_routes.wire(
        wire_routes.chunk([wire_routes.text("ordinary")]),
        wire_routes.chunk([wire_routes.call("router_", None, "P")], finish),
    )
    assert await invoke("-at-stream", "streamGenerateContent") == (
        [{"text": "ordinary"}],
        [reason],
    )
