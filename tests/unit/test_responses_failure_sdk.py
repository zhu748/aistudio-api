"""Responses terminal errors round-trip through the official SDK over local ASGI."""

from __future__ import annotations

import asyncio
import json
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


def _arm_wire_deadline(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Move only the middleware wall timer, at a deliberate ASGI boundary."""
    total_budget = 22.5
    original_timeout = asyncio.timeout
    timer: asyncio.Timeout | None = None

    def capture_timeout(delay: float | None) -> asyncio.Timeout:
        nonlocal timer
        result = original_timeout(delay)
        if delay == total_budget:
            timer = result
        return result

    def expire() -> None:
        assert timer is not None
        timer.reschedule(asyncio.get_running_loop().time())

    monkeypatch.setattr(settings, "request_timeout", total_budget)
    monkeypatch.setattr(asyncio, "timeout", capture_timeout)
    return expire


@pytest.mark.parametrize(
    "boundary",
    ["created_serialized", "created_accepted", "in_progress_accepted", "prep_accepted"],
)
async def test_responses_deadline_uses_only_accepted_lifecycle_events(
    monkeypatch: pytest.MonkeyPatch, isolated_stream: None, boundary: str
) -> None:
    expire = _arm_wire_deadline(monkeypatch)
    expired = False
    source_calls = source_closed = 0

    def expire_deadline() -> None:
        nonlocal expired
        if not expired:
            expired = True
            expire()

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


def _assert_delivered_items(prefix: list[dict], wire: list[dict]) -> None:
    """Failed snapshot contains exactly the items/parts/deltas accepted at cutoff."""
    added = [e["item"] for e in prefix if e["type"] == "response.output_item.added"]
    done = [e["item"] for e in wire if e["type"] == "response.output_item.done"]
    assert [item["id"] for item in added] == [item["id"] for item in done]
    assert done == wire[-1]["response"]["output"]
    for index, (initial, final) in enumerate(zip(added, done, strict=True)):
        accepted = [e for e in prefix if e.get("output_index") == index]
        previously_closed = [
            e["item"] for e in accepted if e["type"] == "response.output_item.done"
        ]
        if previously_closed:
            assert previously_closed == [final]
            continue
        assert final["status"] == "incomplete"
        assert initial.get("extra_content") == final.get("extra_content")
        delta = "".join(e["delta"] for e in accepted if e["type"].endswith(".delta"))
        if initial["type"] == "function_call":
            assert final == {**initial, "status": "incomplete", "arguments": delta}
            assert all(
                e["type"] != "response.function_call_arguments.done"
                for e in wire[len(prefix) :]
            )
            continue
        reasoning = initial["type"] == "reasoning"
        part_type = (
            "response.reasoning_summary_part" if reasoning else "response.content_part"
        )
        text_type = (
            "response.reasoning_summary_text" if reasoning else "response.output_text"
        )
        part_added = any(e["type"] == part_type + ".added" for e in accepted)
        parts = final["summary" if reasoning else "content"]
        assert [part["text"] for part in parts] == ([delta] if part_added else [])
        for suffix in (part_type + ".done", text_type + ".done"):
            assert sum(
                e["type"] == suffix and e.get("output_index") == index for e in wire
            ) == int(part_added)


@pytest.mark.parametrize("wait_for_heartbeat", [False, True])
@pytest.mark.parametrize("upstream_error", [False, True])
async def test_deadline_at_each_accepted_responses_frame(
    monkeypatch: pytest.MonkeyPatch,
    isolated_stream: None,
    wait_for_heartbeat: bool,
    upstream_error: bool,
) -> None:
    expire = _arm_wire_deadline(monkeypatch)
    source_calls = source_closed = 0
    original_prepare = routes_compat.responses_to_gemini_request

    async def source(**_kwargs):
        nonlocal source_calls, source_closed
        source_calls += 1
        try:
            yield "thinking", "Rain? ☂"
            yield "thought_signature", "c2ln"
            yield "body", 'Day "one". '
            yield "body", "🌧 Tomorrow."
            if upstream_error:
                raise RequestError(429, "fixture forecast unavailable")
            yield (
                "tool_calls",
                [
                    {
                        "name": "weather",
                        "args": {"city": "東京"},
                        "call_id": "fixture_call",
                    }
                ],
            )
            yield "finish_reason", 1
        finally:
            source_closed += 1

    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(stream_generate_content=source),
    )

    async def run(cutoff: int | None) -> list[dict]:
        wire: list[dict] = []

        def after_send(message: Message) -> None:
            body = message.get("body", b"")
            if body.startswith(b"event: response."):
                wire.append(json.loads(body.split(b"\ndata: ", 1)[1]))
                if len(wire) - 1 == cutoff:
                    expire()

        observer = _ASGIObserver(after_send=after_send)

        async def prepare(request: OpenAIResponsesRequest):
            if wait_for_heartbeat:
                await observer.heartbeat.wait()
            return await original_prepare(request)

        monkeypatch.setattr(routes_compat, "responses_to_gemini_request", prepare)
        async with (
            asyncio.timeout(3),
            observer.client() as client,
            client.responses.stream(model=_MODEL, input="Weather?") as stream,
        ):
            sdk_types = [e.type async for e in stream]
        assert source_calls == source_closed
        assert observer.statuses == [200]
        assert sdk_types == [e["type"] for e in wire]
        assert [e["sequence_number"] for e in wire] == list(range(len(wire)))
        assert (
            sdk_types[0] == "response.created"
            and sdk_types.count("response.created") == 1
        )
        assert (
            sum(
                t in ("response.failed", "response.completed", "response.incomplete")
                for t in sdk_types
            )
            == 1
        )
        assert (
            len(
                {
                    (e["response"]["id"], e["response"]["created_at"])
                    for e in wire
                    if "response" in e
                }
            )
            == 1
        )
        return wire

    control = await run(None)
    kinds = [e["type"] for e in control]
    expected_terminal = "response.failed" if upstream_error else "response.completed"
    assert kinds[-1] == expected_terminal
    assert kinds.count("response.output_text.delta") == 2
    assert ("response.function_call_arguments.delta" in kinds) == (not upstream_error)
    if upstream_error:
        assert control[-1]["response"]["error"]["code"] == "rate_limit_exceeded"
    # Every accepted cut includes partially opened items, unaccepted close
    # events, and a generated upstream error racing the outer deadline.
    for cutoff in range(len(control)):
        wire = await run(cutoff)
        prefix = wire[: cutoff + 1]
        assert [e["type"] for e in prefix] == kinds[: cutoff + 1]
        if cutoff == len(control) - 1:
            assert len(wire) == len(control) and wire[-1]["type"] == expected_terminal
            continue
        assert wire[-1]["type"] == "response.failed"
        assert wire[-1]["response"]["error"]["code"] == "server_error"
        assert "total timeout" in wire[-1]["response"]["error"]["message"]
        assert all(
            not e["type"].endswith((".added", ".delta")) for e in wire[cutoff + 1 :]
        )
        _assert_delivered_items(prefix, wire)
