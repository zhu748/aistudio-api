"""Responses terminal errors round-trip through the official SDK over local ASGI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2 as httpx
import openai
import pytest
from openai.types.responses import ResponseFailedEvent
from starlette.types import Message, Receive, Scope, Send

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.schemas_compat import OpenAIResponsesRequest
from aistudio_api.application import api_service_gemini, responses_service
from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError

_MODEL = "gemini-3.8-flash"


class _ASGIObserver:
    def __init__(self, after_send: Callable[[Message], None] | None = None) -> None:
        self.statuses: list[int] = []
        self.heartbeat = asyncio.Event()
        self.after_send = after_send

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observe(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.start":
                self.statuses.append(message["status"])
            if message.get("body") == b": keepalive\n\n":
                self.heartbeat.set()
            if self.after_send is not None:
                self.after_send(message)

        await app(scope, receive, observe)

    def client(self) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key="fixture",
            base_url="http://fixture.invalid/v1",
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self)),
            max_retries=0,
            _strict_response_validation=True,
        )


@pytest.fixture
def isolated_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())


@pytest.mark.parametrize(
    ("upstream_code", "response_code"),
    [
        (400, "invalid_prompt"),
        (429, "rate_limit_exceeded"),
        (500, "server_error"),
        (502, "server_error"),
        (503, "server_error"),
        (504, "server_error"),
        (None, "server_error"),
    ],
)
async def test_responses_failed_event_uses_sdk_codes(
    monkeypatch: pytest.MonkeyPatch,
    isolated_stream: None,
    upstream_code: int | None,
    response_code: str,
) -> None:
    calls = closed = 0
    observer = _ASGIObserver()

    async def source(**_kwargs):
        nonlocal calls, closed
        calls += 1
        try:
            yield "body", "Partial forecast."
            if upstream_code is None:
                await asyncio.Event().wait()
            else:
                raise RequestError(upstream_code, "fixture station unavailable")
        finally:
            closed += 1

    monkeypatch.setattr(
        settings, "request_timeout", 0.35 if upstream_code is None else 2
    )
    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(stream_generate_content=source),
    )
    async with (
        asyncio.timeout(3),
        observer.client() as client,
        client.responses.stream(model=_MODEL, input="Weather?") as stream,
    ):
        events = [event async for event in stream]

    assert calls == closed == 1
    assert observer.statuses == [200]
    assert [event.sequence_number for event in events] == list(range(len(events)))
    opened, failed = events[0], events[-1]
    assert opened.type == "response.created"
    assert isinstance(failed, ResponseFailedEvent)
    assert failed.response.id == opened.response.id
    assert failed.response.error is not None
    assert failed.response.error.code == response_code
    assert (
        "Request exceeded total timeout"
        if upstream_code is None
        else "fixture station unavailable"
    ) in failed.response.error.message


@pytest.mark.parametrize(
    "outcome", ["early_error", "late_error", "deadline", "success"]
)
async def test_responses_preparation_respects_http_and_sse_lifecycle(
    monkeypatch: pytest.MonkeyPatch, isolated_stream: None, outcome: str
) -> None:
    observer = _ASGIObserver()
    prepare_closed = asyncio.Event()
    source_calls = 0
    original_prepare = routes_compat.responses_to_gemini_request

    async def prepare(request: OpenAIResponsesRequest):
        try:
            if outcome != "early_error":
                await observer.heartbeat.wait()
            if outcome == "deadline":
                await asyncio.Event().wait()
            if outcome != "success":
                raise ValueError("fixture input could not be converted")
            return await original_prepare(request)
        finally:
            prepare_closed.set()

    async def source(**_kwargs):
        nonlocal source_calls
        source_calls += 1
        yield "body", "Clear."
        yield "finish_reason", 1

    monkeypatch.setattr(routes_compat, "responses_to_gemini_request", prepare)
    monkeypatch.setattr(
        settings, "request_timeout", 0.35 if outcome == "deadline" else 2
    )
    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(stream_generate_content=source),
    )
    async with asyncio.timeout(3), observer.client() as client:
        if outcome == "early_error":
            with pytest.raises(openai.BadRequestError, match="fixture input"):
                async with client.responses.stream(model=_MODEL, input="Weather?"):
                    pytest.fail("pre-commit failure must remain a plain HTTP 400")
            assert observer.statuses == [400]
            assert not observer.heartbeat.is_set()
            assert prepare_closed.is_set() and source_calls == 0
            return
        async with client.responses.stream(model=_MODEL, input="Weather?") as stream:
            events = [event async for event in stream]

    assert observer.statuses == [200] and observer.heartbeat.is_set()
    assert prepare_closed.is_set()
    assert [event.sequence_number for event in events] == list(range(len(events)))
    assert sum(event.type == "response.created" for event in events) == 1
    opened, terminal = events[0], events[-1]
    assert opened.type == "response.created"
    assert opened.response.model == _MODEL
    if outcome == "success":
        assert source_calls == 1 and terminal.type == "response.completed"
        assert terminal.response.id == opened.response.id
        assert terminal.response.output_text == "Clear."
    else:
        assert source_calls == 0 and isinstance(terminal, ResponseFailedEvent)
        assert terminal.response.id == opened.response.id
        assert not terminal.response.output
        error = terminal.response.error
        assert error is not None
        if outcome == "deadline":
            assert error.code == "server_error" and "total timeout" in error.message
        else:
            assert error.code == "invalid_prompt" and "fixture input" in error.message


@pytest.mark.parametrize(
    "boundary",
    ["created_serialized", "created_accepted", "in_progress_accepted", "prep_accepted"],
)
async def test_responses_deadline_uses_only_accepted_lifecycle_events(
    monkeypatch: pytest.MonkeyPatch, isolated_stream: None, boundary: str
) -> None:
    # Move only the middleware's wall timer, at a deliberate wire boundary.
    # No races against an arbitrary sleep or the test client's own timeout.
    total_budget = 22.5
    original_timeout = asyncio.timeout
    captured: list[asyncio.Timeout] = []
    expired = False
    source_calls = source_closed = 0

    def capture_timeout(delay: float | None) -> asyncio.Timeout:
        timer = original_timeout(delay)
        if delay == total_budget:
            captured.append(timer)
        return timer

    def expire_deadline() -> None:
        nonlocal expired
        if not expired:
            assert len(captured) == 1
            expired = True
            captured[0].reschedule(asyncio.get_running_loop().time())

    original_created = responses_service._ResponsesStreamEmitter.created

    def created(emitter: responses_service._ResponsesStreamEmitter) -> str:
        chunk = original_created(emitter)
        if boundary == "created_serialized":
            expire_deadline()
        return chunk

    def after_send(message: Message) -> None:
        body = message.get("body", b"")
        if (
            boundary in ("created_accepted", "prep_accepted")
            and body.startswith(b"event: response.created\n")
        ) or (
            boundary == "in_progress_accepted"
            and body.startswith(b"event: response.in_progress\n")
        ):
            expire_deadline()

    observer = _ASGIObserver(after_send=after_send)
    original_prepare = routes_compat.responses_to_gemini_request

    async def prepare(request: OpenAIResponsesRequest):
        await observer.heartbeat.wait()
        if boundary == "prep_accepted":
            raise ValueError("fixture input could not be converted")
        return await original_prepare(request)

    async def source(**_kwargs):
        nonlocal source_calls, source_closed
        source_calls += 1
        try:
            await asyncio.Event().wait()
            yield "finish_reason", 1
        finally:
            source_closed += 1

    monkeypatch.setattr(settings, "request_timeout", total_budget)
    monkeypatch.setattr(asyncio, "timeout", capture_timeout)
    monkeypatch.setattr(responses_service._ResponsesStreamEmitter, "created", created)
    monkeypatch.setattr(routes_compat, "responses_to_gemini_request", prepare)
    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(stream_generate_content=source),
    )
    async with (
        asyncio.timeout(3),
        observer.client() as client,
        client.responses.stream(model=_MODEL, input="Weather?") as stream,
    ):
        events = [event async for event in stream]

    assert expired and observer.statuses == [200] and observer.heartbeat.is_set()
    assert source_calls == source_closed
    expected = ["response.created", "response.failed"]
    if boundary == "in_progress_accepted":
        expected.insert(1, "response.in_progress")
    assert [event.type for event in events] == expected
    assert [event.sequence_number for event in events] == list(range(len(expected)))
    opened, failed = events[0], events[-1]
    assert opened.type == "response.created" and isinstance(failed, ResponseFailedEvent)
    assert opened.response.id == failed.response.id
    assert opened.response.created_at == failed.response.created_at
    assert not failed.response.output and failed.response.error is not None
    assert failed.response.error.code == "server_error"
    assert "Request exceeded total timeout" in failed.response.error.message
