"""An accepted stop at offset zero should be readable as empty text in the SDK."""

from typing import Any

import anthropic
import httpx2
import pytest
import test_ordered_tool_routes as routes

wire_source = routes.wire_source


@pytest.mark.parametrize(
    ("parts", "finish", "reason", "block_kind", "expected_text"),
    [
        ([routes.text("END hidden")], 1, "stop_sequence", "text", ""),
        (
            [routes.text(s) for s in ("E", "N", "D hidden")],
            1,
            "stop_sequence",
            "text",
            "",
        ),
        ([routes.text("lead END hidden")], 1, "stop_sequence", "text", "lead "),
        ([routes.text("EN")], 1, "end_turn", "text", "EN"),
        (
            [routes.text("why", thought=True), routes.text("END")],
            1,
            "stop_sequence",
            "thinking",
            None,
        ),
        (
            [routes.call("lookup", {}, "c"), routes.text("END")],
            1,
            "stop_sequence",
            "tool_use",
            None,
        ),
        ([routes.text("END")], 2, "max_tokens", None, None),
        ([routes.text("END")], 3, "refusal", None, None),
    ],
    ids=[
        "whole",
        "split",
        "visible-prefix",
        "partial-match",
        "thinking",
        "tool",
        "truncated",
        "refused",
    ],
)
async def test_sdk_stop_sequence_content(
    wire_source, parts, finish, reason, block_kind, expected_text
):
    wire_source.raw = routes.wire(
        *(
            routes.chunk([part], finish if i == len(parts) - 1 else None)
            for i, part in enumerate(parts)
        )
    )
    params: dict[str, Any] = {
        "model": routes.MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "Stop at END."}],
        "stop_sequences": ["END"],
        "extra_headers": {"X-Anti-Truncation": "0"},
    }
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid",
        api_key="fixture",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(routes.app)),
        _strict_response_validation=True,
        max_retries=0,
    ) as sdk:
        async with sdk.messages.stream(**params) as stream:
            events = [event.type async for event in stream]
            final = await stream.get_final_message()
            assert (final.stop_reason, final.stop_sequence) == (
                reason,
                "END" if reason == "stop_sequence" else None,
            )
            assert [b.type for b in final.content] == (
                [block_kind] if block_kind else []
            )
            if block_kind == "text":
                assert await stream.get_final_text() == expected_text
        if finish == 1:
            unary = await sdk.messages.create(**params)
            assert [b.model_dump(exclude_none=True) for b in final.content] == [
                b.model_dump(exclude_none=True) for b in unary.content
            ]
            assert (final.stop_reason, final.stop_sequence) == (
                unary.stop_reason,
                unary.stop_sequence,
            )
    if expected_text == "":
        assert events == [
            "message_start",
            "content_block_start",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
