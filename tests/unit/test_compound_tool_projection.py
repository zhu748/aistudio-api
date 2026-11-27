"""Compat SDKs see identical intra-Part payload order in JSON and SSE."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import openai
import pytest
import test_signed_part_roundtrip as rig

from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

route = rig.route
_STOP = "[STOP]"
_PREFIX = "Caption.Code reached "
_CODE = f"Code reached {_STOP} and is done."


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("surface", ["chat", "claude", "responses"])
async def test_code_result_precedes_call_from_same_part(route, surface, stream):
    route.wire.reply(
        AistudioPart(
            text="Caption.",
            code_execution_result=("OUTCOME_OK", _CODE),
            function_call=("lookup", {"color": "blue"}, "A"),
        ),
    )
    params = rig.turn_params(surface)
    options: dict[str, Any] = {
        "api_key": "fixture",
        "http_client": httpx2.AsyncClient(transport=httpx2.ASGITransport(route.app)),
        "default_headers": rig.HEADERS,
        "_strict_response_validation": True,
        "max_retries": 0,
    }
    if surface == "claude":
        params["stop_sequences"] = [_STOP]
        async with anthropic.AsyncAnthropic(
            base_url="http://fixture.invalid", **options
        ) as client:
            message = await rig._claude_sdk_turn(client, params, stream)
        assert message.stop_reason == "stop_sequence"
        assert message.stop_sequence == _STOP
        assert all(block.type == "text" for block in message.content)
        assert "".join(block.text for block in message.content) == _PREFIX
        return

    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1", **options
    ) as client:
        if surface == "chat":
            params["stop"] = [_STOP]
            if stream:
                async with client.chat.completions.stream(**params) as response:
                    chat = await response.get_final_completion()
            else:
                chat = await client.chat.completions.create(**params)
            choice = chat.choices[0]
            assert choice.finish_reason == "stop"
            assert choice.message.content == _PREFIX
            assert not choice.message.tool_calls
        else:
            if stream:
                async with client.responses.stream(**params) as response:
                    result = await response.get_final_response()
            else:
                result = await client.responses.create(**params)
            message, call = result.output
            assert message.type == "message" and call.type == "function_call"
            assert result.output_text == "Caption." + _CODE
            assert call.call_id == "A" and call.name == "lookup"
