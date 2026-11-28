"""The Messages SDK must see a protocol error, never a fabricated block close."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import nullcontext

import anthropic
import httpx2
import pytest
from starlette.types import Message
from test_ordered_tool_routes import MODEL
from test_responses_failure_sdk import _arm_wire_deadline, _ASGIObserver

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application.api_service_gemini import GeminiStreamResponse
from aistudio_api.config import settings

_DEADLINE = "Request exceeded total timeout"
_UPSTREAM = "fixture station unavailable"


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("upstream_error", [False, True])
async def test_total_timeout_at_each_accepted_messages_event(
    monkeypatch: pytest.MonkeyPatch, deferred: bool, upstream_error: bool
) -> None:
    expire = _arm_wire_deadline(monkeypatch)
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(
        settings, "stream_heartbeat_interval", 0.001 if deferred else 60
    )
    original_prepare = routes_compat.claude_to_gemini_request
    closed = 0

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        nonlocal closed
        try:
            for part in (
                {"text": "Morning. "},
                {"text": "Forecast", "thought": True, "thoughtSignature": "fixture"},
            ):
                yield {"candidates": [{"content": {"parts": [part]}}]}
            if upstream_error:
                yield {"error": {"code": 503, "message": _UPSTREAM}}
            else:
                yield {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "Clear."}]},
                            "finishReason": "STOP",
                        }
                    ]
                }
        finally:
            closed += 1

    async def generate(*_args, **_kwargs) -> GeminiStreamResponse:
        return GeminiStreamResponse(frames())

    monkeypatch.setitem(app.dependency_overrides, get_client, object)
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", generate)

    async def run(cut: int | None, expected_error: str | None) -> list[dict]:
        wire: list[dict] = []
        before = closed

        def after_send(message: Message) -> None:
            body = message.get("body", b"")
            if body.startswith(b"event: "):
                name, data = body.split(b"\ndata: ", 1)
                event = json.loads(data)
                assert name == f"event: {event['type']}".encode()
                if event["type"] == "message_start":
                    # The only non-deterministic field across independent requests.
                    assert event["message"].pop("id").startswith("msg_")
                wire.append(event)
                if len(wire) == cut:
                    expire()

        observer = _ASGIObserver(after_send)

        async def prepare(request: ClaudeMessagesRequest):
            if deferred:
                await observer.heartbeat.wait()
            return await original_prepare(request)

        monkeypatch.setattr(routes_compat, "claude_to_gemini_request", prepare)
        async with (
            asyncio.timeout(3),
            anthropic.AsyncAnthropic(
                api_key="fixture",
                base_url="http://fixture.invalid",
                http_client=httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(observer)
                ),
                max_retries=0,
                _strict_response_validation=True,
            ) as client,
        ):
            with (
                pytest.raises(anthropic.APIStatusError, match=expected_error)
                if expected_error
                else nullcontext()
            ):
                async with client.messages.stream(
                    model=MODEL,
                    max_tokens=128,
                    messages=[{"role": "user", "content": "Weather?"}],
                ) as stream:
                    message = await stream.get_final_message()
                    assert message.stop_reason == "end_turn"
                    assert [b.type for b in message.content] == [
                        "text",
                        "thinking",
                        "text",
                    ]
        assert observer.statuses == [200]
        assert closed == before + 1
        return wire

    original_error = _UPSTREAM if upstream_error else None
    control = await run(None, original_error)
    assert [
        e["delta"]["type"] if e["type"] == "content_block_delta" else e["type"]
        for e in control
    ] == [
        "message_start",
        "ping",
        "content_block_start",
        "text_delta",
        "content_block_stop",
        "content_block_start",
        "thinking_delta",
        "signature_delta",
        "content_block_stop",
        *(
            ["error"]
            if upstream_error
            else [
                "content_block_start",
                "text_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ]
        ),
    ]
    if upstream_error:
        assert control[-1]["error"] == {
            "type": "overloaded_error",
            "message": _UPSTREAM,
        }

    timeout = {"type": "error", "error": {"type": "api_error", "message": _DEADLINE}}
    for cut in range(1, len(control)):
        assert await run(cut, _DEADLINE) == [*control[:cut], timeout]
    # Once either terminal reached the transport, a deadline only finishes HTTP.
    assert await run(len(control), original_error) == control
