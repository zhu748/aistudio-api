"""Final bookkeeping failures and cancellation must still release request state."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import (
    RequestContext,
    active_requests,
    current_request,
    unregister_request,
)


async def receive():
    return {"type": "http.request", "body": b""}


async def send(message):
    pass


SCOPE = {"type": "http", "method": "POST", "path": "/v1/messages", "headers": []}


@pytest.mark.parametrize("failure", ["account_lookup", "persistence"])
async def test_teardown_failure_restores_parent_context_and_unregisters(
    monkeypatch, failure
):
    monkeypatch.setattr(settings, "request_history", False)
    request_id = ""

    async def app(scope, receive, send):
        nonlocal request_id
        context = current_request.get()
        assert context is not None
        context.account_id = "a"
        request_id = context.request_id
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def persist(*args):
        if failure == "persistence":
            raise RuntimeError("teardown failed")

    service = SimpleNamespace(
        get_account=Mock(side_effect=RuntimeError("teardown failed"))
    )
    monkeypatch.setattr(
        runtime_state,
        "account_service",
        service if failure == "account_lookup" else None,
    )
    middleware = RequestMiddleware(app)
    monkeypatch.setattr(middleware, "_persist", persist)
    parent = RequestContext("parent")
    token = current_request.set(parent)
    try:
        with pytest.raises(RuntimeError, match="teardown failed"):
            await middleware(cast(Any, dict(SCOPE)), receive, send)
        assert current_request.get() is parent
        assert request_id not in {item["request_id"] for item in active_requests()}
        assert middleware.active == 0
    finally:
        unregister_request(request_id)
        current_request.reset(token)


async def test_cancellation_during_final_persist_is_propagated_after_cleanup(
    monkeypatch,
):
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setattr(runtime_state, "account_service", None)
    entered, release, persisted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def persist(*args):
        entered.set()
        await release.wait()
        persisted.set()

    middleware = RequestMiddleware(app)
    monkeypatch.setattr(middleware, "_persist", persist)

    async def run():
        try:
            await middleware(cast(Any, dict(SCOPE)), receive, send)
        finally:
            assert current_request.get() is None

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert persisted.is_set() and middleware.active == 0
    assert not active_requests()
