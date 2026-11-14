"""Combined Gemini part fields survive every compatibility protocol."""

import json

import pytest

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.compat_service import (
    build_claude_message,
    build_openai_completion,
    claude_stream_adapter,
    openai_stream_adapter,
)
from aistudio_api.application.responses_service import (
    build_responses_object,
    responses_stream_adapter,
)


def frame(kind, finish="STOP"):
    part: dict[str, object] = {"text": "caption"}
    if kind == "image":
        part["inlineData"] = {"mimeType": "image/png", "data": "aGVsbG8="}
    else:
        part["functionCall"] = {"name": "lookup", "id": "call-1", "args": {"q": 1}}
    return {"candidates": [{"content": {"parts": [part]}, "finishReason": finish}]}


def adapter(protocol, source, stops=None):
    if protocol == "chat":
        return openai_stream_adapter(
            source, requested_model="test", include_usage=False
        )
    if protocol == "claude":
        return claude_stream_adapter(
            source, requested_model="test", stop_sequences=stops
        )
    return responses_stream_adapter(source, requested_model="test")


def events(chunks):
    return [
        json.loads(line[6:])
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


@pytest.mark.parametrize("kind", ["image", "tool"])
@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
def test_nonstream_combined_parts_keep_caption_and_payload(protocol, kind):
    response = GeminiGenerateContentResponse.model_validate(frame(kind))
    if protocol == "chat":
        result = build_openai_completion(response, "test")
    elif protocol == "claude":
        result = build_claude_message(response, "test", None)
    else:
        result = build_responses_object(response, "test")
    encoded = json.dumps(result)
    assert "caption" in encoded
    assert ("aGVsbG8=" if kind == "image" else "lookup") in encoded


@pytest.mark.parametrize("kind", ["image", "tool"])
@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_stream_combined_parts_keep_caption_and_payload(protocol, kind):
    async def source():
        yield frame(kind)

    chunks = [chunk async for chunk in adapter(protocol, source())]
    encoded = json.dumps(events(chunks))
    assert "caption" in encoded
    assert ("aGVsbG8=" if kind == "image" else "lookup") in encoded


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_blocked_combined_call_preserves_caption_and_suppresses_tool(
    protocol, stream
):
    if stream:

        async def source():
            yield frame("tool", "SAFETY")

        result = events([chunk async for chunk in adapter(protocol, source())])
    else:
        response = GeminiGenerateContentResponse.model_validate(frame("tool", "SAFETY"))
        if protocol == "chat":
            result = build_openai_completion(response, "test")
        elif protocol == "claude":
            result = build_claude_message(response, "test", None)
        else:
            result = build_responses_object(response, "test")
    encoded = json.dumps(result)
    assert "caption" in encoded
    assert "lookup" not in encoded


async def test_claude_stop_inside_combined_part_suppresses_its_call():
    async def source():
        yield frame("tool")

    result = events([chunk async for chunk in adapter("claude", source(), ["tion"])])
    encoded = json.dumps(result)
    assert '"text": "cap"' in encoded
    assert "lookup" not in encoded


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_caption_with_final_argument_shard_does_not_force_incomplete_call(
    protocol,
):
    async def source():
        yield {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "fragmented",
                                    "name": "lookup",
                                    "args": '{"q":',
                                }
                            }
                        ]
                    }
                }
            ]
        }
        yield {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "caption",
                                "functionCall": {
                                    "id": "fragmented",
                                    "name": "lookup",
                                    "args": "1}",
                                },
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }

    result = events([chunk async for chunk in adapter(protocol, source())])
    encoded = json.dumps(result)
    assert "caption" in encoded
    assert "lookup" in encoded
    assert "Incomplete tool arguments" not in encoded
    assert '"error"' not in encoded
