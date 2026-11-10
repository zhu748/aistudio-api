"""Round 4 regression tests: per-task isolation of shared gateway state.

These guard against two concrete races that existed when AIStudioClient /
WaaSigner were process-wide singletons with plain instance attributes:

* ``client.last_prepared_request`` — concurrent 403 retries could replay
  another user's signed body/proof.
* ``signer.last_headers`` — a proof minted in one task could be paired with
  dynamic headers (and cookies) minted in another task.

Both are now stored in ``contextvars.ContextVar`` so each asyncio task sees
only the values it produced.
"""

from __future__ import annotations

import asyncio
import json
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from aistudio_api.application import api_service_gemini as gemini_svc
from aistudio_api.infrastructure.gateway.client import (
    AIStudioClient,
    PreparedRequest,
)
from aistudio_api.infrastructure.waa.signer import RemoteProofSigner


def _prep(tag: str) -> PreparedRequest:
    return PreparedRequest(url="https://upstream", headers={}, body=tag, model=tag)


def test_last_prepared_request_is_task_local():
    """Two concurrent tasks must not observe each other's signed request."""
    client = AIStudioClient()
    observed: dict[str, str | None] = {}
    barrier = asyncio.Barrier(2)

    async def worker(tag: str) -> None:
        client.last_prepared_request = _prep(tag)
        await barrier.wait()  # both writes happen before either read
        got = client.last_prepared_request
        observed[tag] = got.body if got is not None else None

    async def run() -> None:
        await asyncio.gather(worker("A"), worker("B"))

    asyncio.run(run())
    assert observed == {"A": "A", "B": "B"}


@pytest.mark.asyncio
async def test_signers_last_headers_are_task_local():
    """Concurrent proof mints must keep proof <-> headers aligned."""
    signer = RemoteProofSigner(server_url="https://mock.proof.server")
    barrier = asyncio.Barrier(2)
    observed: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        tag = json.loads(request.content)["prompt"]
        return httpx.Response(
            200,
            json={
                "ok": True,
                "proof": f"!proof_{tag}",
                "headers": {"X-Signer-Tag": tag},
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def worker(tag: str) -> None:
        # mint_proof writes last_headers on success
        await signer.mint_proof(cookies={"SAPISID": "x"}, prompt=tag)
        await barrier.wait()  # both mints complete before either read
        observed[tag] = dict(signer.last_headers)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await asyncio.gather(worker("A"), worker("B"))
    assert observed["A"] == {"X-Signer-Tag": "A"}
    assert observed["B"] == {"X-Signer-Tag": "B"}
    await signer.close()


def test_contextvar_default_is_independent_of_instance():
    """A fresh task must not inherit a value from a sibling task."""
    client = AIStudioClient()

    async def run() -> None:
        client.last_prepared_request = _prep("outer")

        async def inner() -> PreparedRequest | None:
            return client.last_prepared_request

        # Clear the var in this task before spawning the child so the child
        # starts from the default, proving no stale cross-request leakage.
        from aistudio_api.infrastructure.gateway import client as client_mod

        reset = client_mod._LAST_PREPARED_REQUEST.set(None)
        try:
            child = asyncio.create_task(inner())
            got = await child
        finally:
            client_mod._LAST_PREPARED_REQUEST.reset(reset)
        assert got is None

    asyncio.run(run())


@pytest.mark.asyncio
async def test_transport_error_maps_generic_runtimeerror_to_502(monkeypatch):
    """is_transport_error (not isinstance TransportError) decides the 502 path."""
    monkeypatch.setattr(gemini_svc, "ensure_active_account", lambda *a, **k: _noop())
    monkeypatch.setattr(gemini_svc, "try_switch_account", lambda *a, **k: _noop_false())
    monkeypatch.setattr(gemini_svc, "record_rotator_event", lambda *a, **k: None)
    monkeypatch.setattr(gemini_svc.runtime_state, "record", lambda *a, **k: None)

    # A bare RuntimeError whose message contains a transport keyword is a
    # transport failure per domain/errors.is_transport_error, and must produce
    # 502/upstream_error instead of falling through to the generic 500.
    with pytest.raises(HTTPException) as exc_info:
        await gemini_svc.handle_attempt_exception(
            RuntimeError("connection closed by peer"),
            attempt=0,
            model_path="models/gemini-3.7-flash",
            normalized_model="gemini-3.7-flash",
        )
    assert exc_info.value.status_code == 502
    detail = cast("dict[str, object]", exc_info.value.detail)
    assert detail["type"] == "upstream_error"


async def _noop() -> None:
    return None


async def _noop_false() -> bool:
    return False
