"""A terminal Responses item must have the same lifecycle in JSON and SSE."""

import json

import httpx2
import openai
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from aistudio_api.api import routes_compat
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.compat_shared import decode_signature_envelope

_MODEL = "gemini-3.8-flash"
_DRAFT = {"text": "Draft A."}
_REASON = {"text": "Compare B.", "thought": True, "thoughtSignature": "c2lnLUI="}
_NEXT_REASON = {"text": "Compare C.", "thought": True, "thoughtSignature": "c2lnLUM="}
_CALL = {"functionCall": {"name": "lookup", "args": {"city": "Oslo"}, "id": "A"}}


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param([_DRAFT, _REASON, {"text": "Partial C"}], id="mixed-runs"),
        pytest.param([_REASON], id="sole-reasoning"),
        pytest.param([_REASON, _NEXT_REASON], id="distinct-signed-owners"),
        pytest.param([_DRAFT, _CALL], id="tool-gate"),
    ],
)
@pytest.mark.parametrize(
    "finish,status,reason",
    [("STOP", "completed", None), ("MAX_TOKENS", "incomplete", "max_output_tokens")],
)
async def test_responses_item_status_through_sdk(
    monkeypatch, parts, finish, status, reason
):
    # The gateway only exposes function calls on an accepted STOP stream.
    visible = [p for p in parts if finish == "STOP" or "functionCall" not in p]

    async def generate(*args, stream, **kwargs):
        frame = {
            "candidates": [
                {
                    "content": {"parts": visible if stream else parts},
                    "finishReason": finish,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 4,
                "thoughtsTokenCount": 2,
                "totalTokenCount": 13,
            },
        }
        if stream:
            return StreamingResponse(
                iter([f"data: {json.dumps(frame)}\n\n"]), media_type="text/event-stream"
            )
        return GeminiGenerateContentResponse.model_validate(frame)

    app = FastAPI()
    app.include_router(routes_compat.router)
    app.dependency_overrides[get_client] = lambda: object()
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", generate)
    async with openai.AsyncOpenAI(
        api_key="fixture",
        base_url="http://fixture.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app)),
        _strict_response_validation=True,
        max_retries=0,
    ) as client:
        buffered = await client.responses.create(
            model=_MODEL,
            input="Compare cities.",
            include=["reasoning.encrypted_content"],
        )
        async with client.responses.stream(
            model=_MODEL,
            input="Compare cities.",
            include=["reasoning.encrypted_content"],
        ) as stream:
            events = [event async for event in stream]

    last = events[-1]
    assert last.type == "response.incomplete" or last.type == "response.completed"
    assert last.type == f"response.{status}"
    streamed = last.response
    assert [
        e.item.model_dump(exclude_none=True)
        for e in events
        if e.type == "response.output_item.done"
    ] == [i.model_dump(exclude_none=True) for i in streamed.output]
    assert [
        i.model_dump(exclude={"id"}, exclude_none=True) for i in buffered.output
    ] == [i.model_dump(exclude={"id"}, exclude_none=True) for i in streamed.output]
    for response in (buffered, streamed):
        assert response.status == status and response.model == _MODEL
        assert (
            response.incomplete_details.reason if response.incomplete_details else None
        ) == reason
        assert response.usage and response.usage.input_tokens == 7
        assert response.usage.output_tokens == 6 and response.usage.total_tokens == 13
        assert response.usage.output_tokens_details.reasoning_tokens == 2

    # These fixtures intentionally contain one Part per output item, except
    # that a non-STOP finish must suppress the (non-executable) tool call.
    for index, (item, part) in enumerate(zip(buffered.output, visible, strict=True)):
        data = item.model_dump(exclude_none=True)
        assert data["status"] == (status if index == len(visible) - 1 else "completed")
        if part.get("thought"):
            assert data["type"] == "reasoning"
            assert data["summary"] == [{"type": "summary_text", "text": part["text"]}]
            assert (
                data["extra_content"]["google"]["thought_signature"]
                == part["thoughtSignature"]
            )
            assert (
                decode_signature_envelope(data["encrypted_content"])
                == part["thoughtSignature"]
            )
        elif "functionCall" in part:
            assert data["type"] == "function_call" and data["call_id"] == "A"
            assert data["name"] == "lookup" and json.loads(data["arguments"]) == {
                "city": "Oslo"
            }
        else:
            assert (
                data["type"] == "message" and data["content"][0]["text"] == part["text"]
            )
            assert "extra_content" not in data and "encrypted_content" not in data
