"""Browser telemetry keeps the initiating account and leaves generation metrics alone."""

import asyncio
import gc
import weakref
from contextvars import ContextVar
from unittest.mock import AsyncMock

import pytest

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.gateway import session as module
from aistudio_api.infrastructure.request_context import RequestContext, current_request


@pytest.mark.parametrize("explicit", [False, True])
async def test_followup_uses_original_account_without_mutating_request_context(
    monkeypatch, explicit
):
    monkeypatch.setattr(module, "_BROWSER_COUNTTOKENS_FOLLOWUP", True)
    monkeypatch.setattr(module, "_PAGE_COUNT_TOKENS_DELAY", 0)
    session = module.GatewaySession()
    original = module.SessionContext(
        auth_file="accounts/original/auth.json",
        auth_user="1",
        cookies={"SAPISID": "original"},
    )
    session.set_ready_context(original)
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        return 200, b"[]"

    session._transport.send_request = send
    observation = RequestContext(
        "generation", category="model", account_id="original", stage="finished"
    )
    token = current_request.set(observation)
    try:
        session.schedule_browser_counttokens_followup(
            original if explicit else None, "test", "hello"
        )
        session.set_ready_context(
            module.SessionContext(
                auth_file="accounts/other/auth.json", cookies={"SAPISID": "other"}
            )
        )
        await asyncio.gather(*session._background_followups)
        assert len(calls) == 1
        assert calls[0]["auth_file"] == original.auth_file
        assert calls[0]["cookies"] == original.cookies
        assert observation.account_id == "original"
        assert observation.stage == "finished"
        assert observation.attempts == []
    finally:
        current_request.reset(token)
        await session.close()


async def test_closed_session_does_not_schedule_new_followups(monkeypatch):
    monkeypatch.setattr(module, "_BROWSER_COUNTTOKENS_FOLLOWUP", True)
    session = module.GatewaySession()
    await session.close()
    session.schedule_browser_counttokens_followup(
        module.SessionContext(cookies={"SAPISID": "x"}), "test", "hello"
    )
    assert not session._background_followups
    # Clean up the unfixed implementation's delayed task as well.
    await session.close()


async def test_background_task_and_done_callback_do_not_retain_request_context(
    monkeypatch,
):
    monkeypatch.setattr(module, "_BROWSER_COUNTTOKENS_FOLLOWUP", True)
    session = module.GatewaySession()
    context = module.SessionContext(cookies={"SAPISID": "test"})
    variable: ContextVar[object | None] = ContextVar("request_payload", default=None)

    class Payload:
        pass

    payload = Payload()
    reference = weakref.ref(payload)
    token = variable.set(payload)
    try:
        session.schedule_browser_counttokens_followup(context, "test", "hello")
    finally:
        variable.reset(token)
    del payload
    try:
        gc.collect()
        assert reference() is None
    finally:
        await session.close()


async def test_session_close_releases_signer_even_if_transport_close_fails():
    session = module.GatewaySession()
    session.set_ready_context(module.SessionContext(cookies={"SAPISID": "x"}))
    session._transport.close = AsyncMock(
        side_effect=RuntimeError("transport close failed")
    )
    session._signer.close = AsyncMock()
    with pytest.raises(RuntimeError, match="transport close failed"):
        await session.close()
    session._signer.close.assert_awaited_once()
    assert not session.is_ready


async def test_cancelled_session_close_still_closes_transport_and_signer():
    session = module.GatewaySession()
    session._transport.close = AsyncMock()
    session._signer.close = AsyncMock()
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    followup = asyncio.create_task(worker())
    session._background_followups.add(followup)
    await entered.wait()
    closing = asyncio.create_task(session.close())
    await cleaning.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    session._transport.close.assert_awaited_once()
    session._signer.close.assert_awaited_once()
    assert not session._background_followups


@pytest.mark.parametrize(
    "operation",
    ["ready", "transition", "proxy", "sign", "send", "stream", "set_context"],
)
async def test_closed_session_cannot_reload_credentials_or_restart_work(operation):
    session = module.GatewaySession()
    context = module.SessionContext(cookies={"SAPISID": "test"})
    await session.close()
    with pytest.raises(TransportError, match="closed"):
        if operation == "ready":
            await session.ensure_ready()
        elif operation == "transition":
            await session.switch_auth(None)
        elif operation == "proxy":
            await session.set_default_proxy(None)
        elif operation == "sign":
            await session.generate_snapshot([], context)
        elif operation == "send":
            await session.send_request(body="[]", ctx=context)
        elif operation == "stream":
            await anext(session.send_streaming_request(body="[]", ctx=context))
        else:
            session.set_ready_context(context)
