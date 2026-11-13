"""Exercise real SDK parsers and stream accumulators against the ASGI routes."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import anthropic
import httpx2 as httpx
import openai
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput


@pytest.fixture
def sdk_gateway(monkeypatch):
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 14,
        "total_tokens": 26,
        "cached_tokens": 2,
        "completion_tokens_details": {"reasoning_tokens": 4, "visible_tokens": 10},
    }
    calls = [
        {
            "name": "weather",
            "args": {"city": "Paris"},
            "call_id": "call_weather",
            "thought_signature": "tool-signature",
        }
    ]

    async def stream_generate_content(**kwargs):
        yield "thinking", "reasoning"
        yield "thought_signature", "thinking-signature"
        yield "body", "answer"
        yield "tool_calls", calls
        yield "finish_reason", client.finish_reason
        yield "usage", usage

    client: SimpleNamespace = SimpleNamespace(
        finish_reason=1,
        stream_generate_content=stream_generate_content,
        generate_content=AsyncMock(
            return_value=ModelOutput(
                candidates=[
                    Candidate(
                        text="answer",
                        thinking="reasoning",
                        function_calls=calls,
                        finish_reason=1,
                    )
                ],
                usage=usage,
            )
        ),
    )
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_client] = lambda: client
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def test_openai_sdk_chat_accumulates_tools_and_usage(sdk_gateway):
    async with openai.AsyncOpenAI(
        api_key="test",
        base_url="http://test/v1",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app)),
        _strict_response_validation=True,
    ) as client:
        async with client.chat.completions.stream(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hi"}],
            stream_options={"include_usage": True},
        ) as stream:
            result = await stream.get_final_completion()
        assert result.choices[0].message.content == "answer"
        calls = result.choices[0].message.tool_calls
        assert calls and len(calls) == 1
        assert cast(Any, calls[0]).function.arguments == '{"city": "Paris"}'
        assert result.usage and result.usage.completion_tokens == 14
        assert result.usage.total_tokens == 26


async def test_openai_sdk_responses_accumulates_output_items(sdk_gateway):
    async with openai.AsyncOpenAI(
        api_key="test",
        base_url="http://test/v1",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app)),
        _strict_response_validation=True,
    ) as client:
        async with client.responses.stream(
            model="gemini-3.8-flash", input="hi"
        ) as stream:
            result = await stream.get_final_response()
        assert result.status == "completed" and result.output_text == "answer"
        calls = [item for item in result.output if item.type == "function_call"]
        assert len(calls) == 1 and calls[0].arguments == '{"city": "Paris"}'
        assert result.usage and result.usage.output_tokens == 14


async def test_anthropic_sdk_accumulates_thinking_text_and_tools(sdk_gateway):
    async with anthropic.AsyncAnthropic(
        api_key="test",
        base_url="http://test",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app)),
        _strict_response_validation=True,
    ) as client:
        async with client.messages.stream(
            model="gemini-3.8-flash",
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            result = await stream.get_final_message()
        assert [block.type for block in result.content] == [
            "thinking",
            "text",
            "tool_use",
        ]
        calls = [block for block in result.content if block.type == "tool_use"]
        assert calls[0].input == {"city": "Paris"}
        assert result.usage.output_tokens == 14
        assert result.usage.input_tokens == 10
        assert result.usage.cache_read_input_tokens == 2


async def test_responses_sdk_truncation_keeps_incomplete_state_and_no_tool_execution(
    sdk_gateway,
):
    sdk_gateway.finish_reason = 2
    async with openai.AsyncOpenAI(
        api_key="test",
        base_url="http://test/v1",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app)),
        _strict_response_validation=True,
    ) as client:
        async with client.responses.stream(
            model="gemini-3.8-flash", input="hi"
        ) as stream:
            events = [event async for event in stream]
        result = [event for event in events if event.type == "response.incomplete"][
            -1
        ].response
        assert result.status == "incomplete"
        assert all(item.type != "function_call" for item in result.output)
        assert (
            result.incomplete_details
            and result.incomplete_details.reason == "max_output_tokens"
        )


@pytest.mark.parametrize("protocol", ["chat", "responses", "claude"])
async def test_sdk_non_streaming_strict_validation(sdk_gateway, protocol):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app))
    if protocol == "claude":
        async with anthropic.AsyncAnthropic(
            api_key="test",
            base_url="http://test",
            http_client=http,
            _strict_response_validation=True,
        ) as client:
            result = await client.messages.create(
                model="gemini-3.8-flash",
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
            )
            assert result.usage.output_tokens == 14
    else:
        async with openai.AsyncOpenAI(
            api_key="test",
            base_url="http://test/v1",
            http_client=http,
            _strict_response_validation=True,
        ) as client:
            if protocol == "chat":
                completion = await client.chat.completions.create(
                    model="gemini-3.8-flash",
                    messages=[{"role": "user", "content": "hi"}],
                )
                assert completion.usage and completion.usage.completion_tokens == 14
            else:
                response = await client.responses.create(
                    model="gemini-3.8-flash", input="hi"
                )
                assert response.usage and response.usage.output_tokens == 14


@pytest.mark.parametrize("protocol", ["chat", "responses", "claude"])
async def test_stream_timeout_uses_protocol_error_and_closes_upstream(
    sdk_gateway, monkeypatch, protocol
):
    monkeypatch.setattr(settings, "request_timeout", 0.05)
    monkeypatch.setattr(settings, "request_history", False)
    closed = asyncio.Event()

    async def stalled(**kwargs):
        try:
            yield "body", "partial"
            await asyncio.Event().wait()
        finally:
            closed.set()

    sdk_gateway.stream_generate_content = stalled
    path, payload = {
        "chat": (
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "hi"}]},
        ),
        "responses": ("/v1/responses", {"input": "hi"}),
        "claude": (
            "/v1/messages",
            {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
        ),
    }[protocol]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            path, json={"model": "gemini-3.8-flash", "stream": True, **payload}
        )
    assert response.status_code == 200 and closed.is_set()
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    if protocol == "responses":
        assert events[-1]["type"] == "response.failed"
        assert events[-1]["response"]["id"] == events[0]["response"]["id"]
        assert events[-1]["response"]["status"] == "failed"
    elif protocol == "claude":
        assert events[-1]["type"] == "error"
        assert "event: error" in response.text
    else:
        assert events[-1]["error"]["code"] == 504
        assert response.text.endswith("data: [DONE]\n\n")
