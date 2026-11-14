"""Partial Responses messages must not advertise completed output items."""

import json

import pytest

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.responses_service import (
    build_responses_object,
    responses_stream_adapter,
)


@pytest.mark.parametrize("finish", ["STOP", "MAX_TOKENS", "SAFETY", "OTHER"])
def test_nonstream_message_status_matches_response(finish):
    response = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "partial"}]},
                    "finishReason": finish,
                }
            ]
        }
    )
    result = build_responses_object(response, "test")
    output = result["output"]
    assert isinstance(output, list)
    assert output[0]["status"] == result["status"]


@pytest.mark.parametrize("failure", ["upstream", "exception", "timeout_callback"])
async def test_failed_stream_closes_partial_items_as_incomplete_only_once(failure):
    handlers = []

    async def source():
        yield {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "thinking", "thought": True},
                            {"text": "partial"},
                        ]
                    }
                }
            ]
        }
        if failure == "exception":
            raise RuntimeError("lost connection")
        if failure == "upstream":
            yield {"error": {"message": "lost connection"}}
        else:
            # Pause before a terminal result so the registered timeout hook fires.
            yield {"candidates": []}

    stream = responses_stream_adapter(
        source(), requested_model="test", register_failure=handlers.append
    )
    raw = []
    if failure == "timeout_callback":
        async for chunk in stream:
            raw.append(chunk)
            if "event: response.output_text.delta\n" in chunk:
                raw.extend(handlers[0]("timeout"))
                assert handlers[0]("timeout again") == []
                break
        await stream.aclose()
    else:
        raw = [chunk async for chunk in stream]
    events = [
        json.loads(line[6:])
        for chunk in raw
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    done = [
        event["item"]
        for event in events
        if event["type"] == "response.output_item.done"
    ]
    assert len(done) == 2
    assert all(item["status"] == "incomplete" for item in done)
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["output"] == done
