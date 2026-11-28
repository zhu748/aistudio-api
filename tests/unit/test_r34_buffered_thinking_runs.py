"""Fragmented Claude thinking stays linear without merging distinct signed owners."""

import anthropic
from anthropic.types import UserMessageParam
from test_compat_media_routes import _client_options, _serialized_contents
from test_signed_part_roundtrip import MODEL, route as route, signature, thought

from aistudio_api.application import compat_claude
from aistudio_api.application.compat_shared import decode_signature_envelope
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


async def test_buffered_fragments_then_tool_result_replay(route, monkeypatch):
    pieces = [f"piece/{i}:雪🙂 " for i in range(128)]
    first = "".join(pieces)
    tool = AistudioPart(
        function_call=("lookup", {"city": "京都"}, "A"),
        thought_signature=signature("tool"),
    )
    route.wire.reply(
        *(AistudioPart(text=value, thought=True) for value in pieces),
        thought("", "empty"),
        thought("signed boundary", "other"),
        AistudioPart(text="second", thought=True),
        AistudioPart(text=" run", thought=True),
        AistudioPart(text="visible"),
        tool,
    )
    sent = _serialized_contents(route)
    # The former merger reread every growing prefix with this coercion.
    # Use a work counter, not a timing assertion.
    copied_prefix_chars = 0
    read_string = compat_claude._as_str

    def track(value):
        nonlocal copied_prefix_chars
        if isinstance(value, str) and value.startswith("piece/"):
            copied_prefix_chars += len(value)
        return read_string(value)

    monkeypatch.setattr(compat_claude, "_as_str", track)
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as client:
        prompt: UserMessageParam = {"role": "user", "content": "Look up a city."}
        message = await client.messages.create(
            model=MODEL, max_tokens=4096, messages=[prompt]
        )
        blocks = [block.model_dump(exclude_none=True) for block in message.content]
        assert message.stop_reason == "tool_use"
        assert [block["type"] for block in blocks] == [
            "thinking",
            "thinking",
            "thinking",
            "thinking",
            "text",
            "tool_use",
        ]
        assert [
            (block["thinking"], decode_signature_envelope(block["signature"]))
            for block in blocks[:4]
        ] == [
            (first, None),
            ("", signature("empty")),
            ("signed boundary", signature("other")),
            ("second run", None),
        ]
        assert (blocks[4]["text"], blocks[5]["id"]) == ("visible", "A")
        assert copied_prefix_chars <= 2 * len(first)
        route.wire.reply(AistudioPart(text="Done."))
        await client.messages.create(
            messages=[
                prompt,
                message.to_param(),
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "A", "content": "local"}
                    ],
                },
            ],
            model=MODEL,
            max_tokens=4096,
        )
    assert sent[-1][1:] == [
        [
            [
                AistudioPart(text=first, thought=True).to_wire(),
                thought("", "empty").to_wire(),
                thought("signed boundary", "other").to_wire(),
                AistudioPart(text="second run", thought=True).to_wire(),
                AistudioPart(text="visible").to_wire(),
                tool.to_wire(),
            ],
            "model",
        ],
        [
            [
                AistudioPart(
                    function_response=("lookup", {"result": "local"}, "A")
                ).to_wire()
            ],
            "user",
        ],
    ]
