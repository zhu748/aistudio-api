"""Signer hot changes must preserve independent settings and cached proof identity."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart
from aistudio_api.infrastructure.waa import signer as module

CONTENTS = [AistudioContent(role="user", parts=[AistudioPart(text="hi")])]
COOKIES = {"SAPISID": "same-prefix-12345-A"}


async def test_concurrent_node_and_proxy_changes_preserve_both_updates():
    signer = module.WaaSigner(
        proof_server_url="https://old.example", proxy_url="http://old:1"
    )
    async with signer._get_reconfigure_lock():
        first = asyncio.create_task(
            signer.reconfigure(server_url="https://new.example")
        )
        second = asyncio.create_task(signer.reconfigure(proxy_url="http://new:2"))
        await asyncio.sleep(0)
    try:
        await asyncio.gather(first, second)
        assert signer.server_url == "https://new.example"
        assert signer._remote_signer.proxy_url == "http://new:2"
    finally:
        await signer.close()


async def test_remote_client_creation_cannot_complete_after_close(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(proxy):
        entered.set()
        await release.wait()
        return

    monkeypatch.setattr(module, "resolve_proxy_url_async", resolve)
    remote = module.RemoteProofSigner(server_url="https://signer.example")
    task = asyncio.create_task(remote._get_client())
    await entered.wait()
    await remote.close()
    release.set()
    try:
        with pytest.raises(TransportError, match="closed"):
            await task
    finally:
        await remote.close()


async def test_closed_facade_cannot_reconfigure_or_mint(monkeypatch):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner()
    signer._remote_signer.mint_proof = AsyncMock(return_value="!cached")
    await signer.generate_snapshot(CONTENTS, COOKIES)
    await signer.close()
    with pytest.raises(TransportError, match="closed"):
        await signer.generate_snapshot(CONTENTS, COOKIES)
    with pytest.raises(TransportError, match="closed"):
        await signer.reconfigure(server_url="https://other.example")


@pytest.mark.parametrize("changed", ["cookie", "route"])
async def test_proof_cache_isolated_by_full_cookie_and_upstream_route(
    monkeypatch, changed
):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner()
    mint = AsyncMock(side_effect=["!first", "!second"])
    signer._remote_signer.mint_proof = mint
    try:
        assert (
            await signer.generate_snapshot(
                CONTENTS, COOKIES, upstream_proxy="http://one:1"
            )
            == "!first"
        )
        cookies = {"SAPISID": "same-prefix-12345-B"} if changed == "cookie" else COOKIES
        route = "http://two:2" if changed == "route" else "http://one:1"
        assert (
            await signer.generate_snapshot(CONTENTS, cookies, upstream_proxy=route)
            == "!second"
        )
        assert mint.await_count == 2
    finally:
        await signer.close()


async def test_empty_cached_headers_clear_previous_signature_metadata(monkeypatch):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner()

    async def mint(**kwargs):
        signer._remote_signer.last_headers = {}
        return "!empty-headers"

    signer._remote_signer.mint_proof = mint
    try:
        await signer.generate_snapshot(CONTENTS, COOKIES)
        signer._remote_signer.last_headers = {"Cookie": "different-account"}
        assert await signer.generate_snapshot(CONTENTS, COOKIES) == "!empty-headers"
        assert signer.last_headers == {}
    finally:
        await signer.close()


async def test_old_inflight_proof_does_not_refill_cache_after_node_change(monkeypatch):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner(proof_server_url="https://old.example")
    entered, release = asyncio.Event(), asyncio.Event()

    async def old_mint(**kwargs):
        entered.set()
        await release.wait()
        return "!old"

    signer._remote_signer.mint_proof = old_mint
    pending = asyncio.create_task(signer.generate_snapshot(CONTENTS, COOKIES))
    await entered.wait()
    try:
        await signer.reconfigure(server_url="https://new.example")
        signer._remote_signer.mint_proof = AsyncMock(return_value="!new")
        release.set()
        assert await pending == "!old"
        assert await signer.generate_snapshot(CONTENTS, COOKIES) == "!new"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await signer.close()


async def test_node_change_invalidates_already_cached_proofs(monkeypatch):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner(proof_server_url="https://old.example")
    signer._remote_signer.mint_proof = AsyncMock(return_value="!old")
    try:
        assert await signer.generate_snapshot(CONTENTS, COOKIES) == "!old"
        await signer.reconfigure(server_url="https://new.example")
        signer._remote_signer.mint_proof = AsyncMock(return_value="!new")
        assert await signer.generate_snapshot(CONTENTS, COOKIES) == "!new"
    finally:
        await signer.close()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_retired_signer_waits_for_all_requests_and_releases_failed_leases(
    outcome,
):
    signer = module.WaaSigner(proof_server_url="https://old.example")
    old = signer._remote_signer
    closed = AsyncMock()
    old.close = closed
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    started = 0

    async def mint(**kwargs):
        nonlocal started
        index = started
        started += 1
        entered[index].set()
        await release[index].wait()
        if outcome == "error":
            raise TransportError("mint failed")
        return "!proof"

    old.mint_proof = mint
    tasks = [
        asyncio.create_task(signer.generate_snapshot(CONTENTS, COOKIES))
        for _ in range(2)
    ]
    await asyncio.gather(*(event.wait() for event in entered))
    try:
        await signer.reconfigure(server_url="https://new.example")
        if outcome == "cancel":
            tasks[0].cancel()
        else:
            release[0].set()
        await asyncio.gather(tasks[0], return_exceptions=True)
        closed.assert_not_awaited()
        if outcome == "cancel":
            tasks[1].cancel()
        else:
            release[1].set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if outcome == "success":
            assert results == ["!proof", "!proof"]
        else:
            expected = asyncio.CancelledError if outcome == "cancel" else TransportError
            assert all(isinstance(result, expected) for result in results)
        await asyncio.wait_for(asyncio.gather(*signer._delayed_close_tasks), 1)
        closed.assert_awaited_once()
        assert not signer._active_signers
        assert not signer._signer_idle
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await signer.close()


async def test_retired_signer_is_closed_when_its_last_request_finishes():
    signer = module.WaaSigner(proof_server_url="https://old.example")
    old = signer._remote_signer
    closed = AsyncMock()
    old.close = closed
    entered, release = asyncio.Event(), asyncio.Event()

    async def mint(**kwargs):
        entered.set()
        await release.wait()
        return "!proof"

    old.mint_proof = mint
    task = asyncio.create_task(signer.generate_snapshot(CONTENTS, COOKIES))
    await entered.wait()
    try:
        await signer.reconfigure(server_url="https://new.example")
        await asyncio.sleep(0)
        closed.assert_not_awaited()
        release.set()
        assert await task == "!proof"
        # Closure follows actual leases, without a fixed 35 second timer.
        await asyncio.wait_for(asyncio.gather(*signer._delayed_close_tasks), 0.2)
        closed.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await signer.close()
