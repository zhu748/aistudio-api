"""Native JSON and SSE preserve ordinary Unicode through the Google SDK."""

from __future__ import annotations

import json

import pytest
import test_anti_truncation_variants as variants
import test_ordered_tool_routes as wire_routes
from google.genai import types
from test_genai_sdk_compat import _build_sdk_client

from aistudio_api.application.api_service_gemini import _encode_sse

wire_source = wire_routes.wire_source
selected_variant = variants.variant_config


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029", "，"])
@pytest.mark.parametrize(("suffix", "stream"), [("", False), ("", True), ("-at", True)])
async def test_native_sdk_roundtrips_line_separators(
    wire_source: wire_routes.WireSource,
    selected_variant,
    suffix: str,
    stream: bool,
    separator: str,
) -> None:
    answer = "晴" + separator + "雨🙂 " + r"\u0085 \u2028 \u2029"
    args = {"city": answer, "details": [answer]}
    wire_source.raw = wire_routes.wire(
        wire_routes.chunk(
            [wire_routes.text(answer), wire_routes.call("weather", args, "weather")],
            1,
        )
    )
    sdk, transport = _build_sdk_client()
    config = types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )
    try:
        async with sdk.aio as client:
            model = "gemini-3.7-flash" + suffix
            if stream:
                frames = [
                    frame
                    async for frame in await client.models.generate_content_stream(
                        model=model, contents="Weather?", config=config
                    )
                ]
            else:
                frames = [
                    await client.models.generate_content(
                        model=model, contents="Weather?", config=config
                    )
                ]
    finally:
        await transport.aclose()

    parts = [
        part
        for frame in frames
        for candidate in frame.candidates or []
        if candidate.content is not None
        for part in candidate.content.parts or []
    ]
    assert [part.text for part in parts if part.text is not None] == [answer]
    assert [
        part.function_call.args for part in parts if part.function_call is not None
    ] == [args]


def test_native_sse_does_not_ascii_escape_unrelated_unicode() -> None:
    text = "晴🙂 \u2028 " + r"\u2028"
    body = _encode_sse({"text": text})
    assert "晴🙂" in body
    assert "\u2028" not in body
    assert json.loads(body.removeprefix("data: "))["text"] == text
