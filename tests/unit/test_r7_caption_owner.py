"""Removing a caption must not transfer the compound Part's proof to a tool."""

from __future__ import annotations

import pytest
import test_anti_truncation_variants as variants
import test_signed_part_roundtrip as rig
from test_ordered_tool_routes import native_parts, sse_events

from aistudio_api.application.anti_truncation import _without_plain_text
from aistudio_api.domain.models import GeneratedImage, ResponsePart
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

route = rig.route
variant_config = variants.variant_config


@pytest.mark.usefixtures("variant_config")
@pytest.mark.parametrize(
    ("variant", "action"),
    [
        ("-at", "generateContent"),
        ("-at", "streamGenerateContent"),
        ("-at-stream", "streamGenerateContent"),
    ],
)
@pytest.mark.parametrize("display", [True, False], ids=["caption-stripped", "intact"])
async def test_caption_signature_stays_with_its_exact_part(
    route, variant, action, display
):
    caption_sig = rig.signature("call with caption")
    bare_sig = rig.signature("bare call")
    city = {"city": "Bath"}
    other = {"city": "Leeds"}
    caption = "This is a tool caption."
    inputs = [
        AistudioPart(
            text=caption,
            function_call=("weather", city, "W"),
            thought_signature=caption_sig,
        ),
        AistudioPart(function_call=("weather", other, "X"), thought_signature=bare_sig),
    ]
    if display:
        inputs.insert(
            0,
            AistudioPart(
                function_call=("router_emit", {"content": "Display text."}, "A")
            ),
        )
    route.wire.reply(*inputs)
    response = await route.raw.post(
        f"/v1beta/models/gemini-3.7-flash{variant}:{action}",
        json={
            "contents": [{"role": "user", "parts": [{"text": "Report the weather."}]}],
            "tools": [{"functionDeclarations": [{"name": "weather"}]}],
        },
        headers={"X-Anti-Truncation": "true"},
    )
    assert response.status_code == 200, response.text
    events = (
        [response.json()] if action == "generateContent" else sse_events(response.text)
    )
    first = {"functionCall": {"name": "weather", "args": city, "id": "W"}}
    expected = (
        [{"text": "Display text."}, first]
        if display
        else [
            {**first, "text": caption, "thoughtSignature": caption_sig},
        ]
    )
    assert native_parts(events) == [
        *expected,
        {
            "functionCall": {"name": "weather", "args": other, "id": "X"},
            "thoughtSignature": bare_sig,
        },
    ]
    # The compound proof never belongs to the cache's tool-only key.
    assert route.cache.lookup("W", "weather", city) is None
    assert route.cache.lookup("X", "weather", other) == bare_sig


@pytest.mark.parametrize("caption", ["", "Caption."])
def test_projection_clears_decoded_mirrors_without_mutating_the_source(caption):
    sig = rig.signature("compound carrier")
    call: dict[str, object] = {"name": "weather", "thought_signature": sig}
    image = GeneratedImage("image/png", "aGVsbG8=", thought_signature=sig)
    part = ResponsePart(
        text=caption,
        text_present=True,
        function_call=call,
        image=image,
        thought_signature=sig,
    )
    projected = _without_plain_text(part)
    assert not projected.text_present and not projected.thought_signature
    assert projected.function_call == {"name": "weather"}
    assert projected.image == GeneratedImage("image/png", "aGVsbG8=")
    assert part.text_present and part.text == caption and part.thought_signature == sig
    assert call["thought_signature"] == image.thought_signature == sig
