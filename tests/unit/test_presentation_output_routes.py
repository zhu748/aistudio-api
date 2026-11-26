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
