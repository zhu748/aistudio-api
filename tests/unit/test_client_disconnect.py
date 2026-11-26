"""The public ASGI routes cancel work and report what reached the client."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.types import Message, Scope
from test_keepalive import request_for

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.state import RuntimeState, runtime_state
from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.request_context import active_requests
from aistudio_api.infrastructure.request_history import request_history

_ANSWER = "Here is the answer."


@pytest.fixture
def isolated_history(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "request_history", True)
    monkeypatch.setattr(settings, "persist_stats", False)
    monkeypatch.setattr(settings, "request_timeout", 30.0)
    monkeypatch.setattr(runtime_state, "account_service", None)
    monkeypatch.setattr(runtime_state, "rotator", None)
    monkeypatch.setattr(runtime_state, "model_stats", RuntimeState().model_stats)

    async def ensure_active(*_args, **_kwargs):
        pass

    monkeypatch.setattr(api_service_gemini, "ensure_active_account", ensure_active)


class _Client:
    def __init__(self, protocol: str, *, stream: bool, spec_version: str):
        path, payload = request_for(protocol, stream)
        body = json.dumps(payload).encode()
        self.scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "method": "POST",
            "headers": [(b"content-type", b"application/json")],
        }
        self.messages: list[Message] = []
        self.disconnect = asyncio.Event()
        self.heartbeat = asyncio.Event()
        # Exercise handoff after multi-part body parsing. Only one reader may
        # pull from the server at a time, including the standard SSE listener.
        halfway = len(body) // 2
        self._body_parts = [body[:halfway], body[halfway:]]
        self.receiving = False

    async def receive(self) -> Message:
        assert not self.receiving, "concurrent reads from the ASGI server"
        self.receiving = True
        try:
            await asyncio.sleep(0)
            if self._body_parts:
                body = self._body_parts.pop(0)
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": bool(self._body_parts),
                }
            await self.disconnect.wait()
            return {"type": "http.disconnect"}
        finally:
            self.receiving = False

    async def send(self, message: Message) -> None:
        self.messages.append(message)
        if message.get("body") == b" \n":
            self.heartbeat.set()

    def stored_result(self):
        report = request_history().query()
        assert report["total"] == 1
        items = report["items"]
        assert isinstance(items, list)
        return items[0]


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize(
    ("keepalive", "after_heartbeat"), [(False, False), (True, False), (True, True)]
)
async def test_buffered_generation_cancels_independent_of_heartbeat(
    isolated_history, monkeypatch, spec_version, keepalive, after_heartbeat
):
    monkeypatch.setattr(settings, "nonstream_keepalive", keepalive)
    monkeypatch.setattr(
        settings, "stream_heartbeat_interval", 0.01 if after_heartbeat else 10.0
    )
    upstream_started, upstream_closed = asyncio.Event(), asyncio.Event()

    async def generate_content(**_kwargs):
        upstream_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            upstream_closed.set()

    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(generate_content=generate_content),
    )
    client = _Client("chat", stream=False, spec_version=spec_version)
    # The overall request timeout and (unless explicitly awaited) heartbeat
    # are far outside this test; finishing promptly requires a disconnect read.
    async with asyncio.timeout(3):
        async with asyncio.TaskGroup() as group:
            group.create_task(app(client.scope, client.receive, client.send))
            await upstream_started.wait()
            if after_heartbeat:
                await client.heartbeat.wait()
            client.disconnect.set()

    assert upstream_closed.is_set() and not client.receiving
    if after_heartbeat:
        assert client.messages[0]["status"] == 200
    else:
        assert not client.messages
    outcome = client.stored_result()
    assert outcome["result"] == "cancelled"
    assert "Downstream disconnected" in outcome["error"]
    assert not active_requests()


def _stream_finished(protocol: str, body: bytes) -> bool:
    if protocol == "chat":
        return body == b"data: [DONE]\n\n"
    if protocol == "claude":
        return body.startswith(b"event: message_stop\n")
    if protocol == "responses":
        return body.startswith(b"event: response.completed\n")
    return b'"finishReason":"STOP"' in body


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("protocol", ["chat", "claude", "responses", "gemini"])
@pytest.mark.parametrize("client_received_terminal", [False, True])
async def test_stream_history_distinguishes_disconnect_from_a_delivered_terminal(
    isolated_history, monkeypatch, spec_version, protocol, client_received_terminal
):
    upstream_closed = asyncio.Event()

    async def stream_generate_content(**_kwargs):
        try:
            yield "body", _ANSWER
            if client_received_terminal:
                yield "finish_reason", 1
            else:
                await asyncio.Event().wait()
        finally:
            upstream_closed.set()

    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(stream_generate_content=stream_generate_content),
    )
    client = _Client(protocol, stream=True, spec_version=spec_version)

    async def send(message: Message) -> None:
        await client.send(message)
        if message["type"] != "http.response.body":
            return
        body = message.get("body", b"")
        if client_received_terminal:
            if _stream_finished(protocol, body):
                client.disconnect.set()
            # Ensure the disconnect is observed before the HTTP closing frame.
            if not message.get("more_body", False):
                await asyncio.Event().wait()
        elif _ANSWER.encode() in body:
            client.disconnect.set()

    async with asyncio.timeout(3):
        await app(client.scope, client.receive, send)

    assert client.disconnect.is_set()
    assert upstream_closed.is_set() and not client.receiving
    outcome = client.stored_result()
    stages = [event["stage"] for event in outcome["events"]]
    if client_received_terminal:
        assert outcome["result"] == "success"
        assert outcome["finish_reason"] == "STOP"
        assert "disconnected" not in stages
    else:
        assert outcome["result"] == "cancelled"
        assert "Downstream disconnected" in outcome["error"]
        assert not outcome["finish_reason"]
        assert stages[-2:] == ["disconnected", "finished"]
    assert not active_requests()


@pytest.mark.parametrize("keepalive", [False, True])
@pytest.mark.parametrize("close_after_json", [False, True])
async def test_delivered_buffered_request_remains_successful(
    isolated_history, monkeypatch, keepalive, close_after_json
):
    monkeypatch.setattr(settings, "nonstream_keepalive", keepalive)
    monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
    client = _Client("chat", stream=False, spec_version="2.4")

    async def generate_content(**_kwargs):
        if keepalive:
            await client.heartbeat.wait()
        return ModelOutput(candidates=[Candidate(text=_ANSWER, finish_reason=1)])

    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(generate_content=generate_content),
    )

    async def send(message: Message) -> None:
        await client.send(message)
        if close_after_json and message["type"] == "http.response.body":
            if _ANSWER.encode() in message.get("body", b""):
                client.disconnect.set()
            if not message.get("more_body", False):
                await asyncio.Event().wait()

    async with asyncio.timeout(3):
        await app(client.scope, client.receive, send)

    assert client.disconnect.is_set() == close_after_json
    assert not client.receiving
    assert client.messages[0]["status"] == 200
    body = b"".join(message.get("body", b"") for message in client.messages)
    assert json.loads(body)["choices"][0]["message"]["content"] == _ANSWER
    outcome = client.stored_result()
    assert outcome["result"] == "success" and outcome["finish_reason"] == "STOP"
