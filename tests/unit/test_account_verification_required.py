"""Tests for Google 403 account-verification auto-disable, tagging, and failover."""

from __future__ import annotations

import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.routes_accounts import to_account_response
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiCountTokensRequest,
    GeminiGenerateContentRequest,
    GeminiPart,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.application import api_service_gemini as gemini_svc
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.account_service import AccountService
from aistudio_api.config import settings
from aistudio_api.domain.errors import (
    AccountVerificationRequiredError,
    AuthError,
    classify_error,
    is_account_verification_required,
)
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.waa.health import (
    get_signer_health,
    reset_signer_health_for_tests,
)

VERIFY_ERROR_MSG = (
    "Your account must be verified before it can continue using the Gemini API. "
    "Verify your account at https://aistudio.google.com/verify-account. "
    "Learn more at https://ai.google.dev/gemini-api/docs/verification#verify-account."
)


def _storage_state(tag: str) -> dict[str, object]:
    return {
        "cookies": [
            {"name": "SAPISID", "value": f"sapisid-{tag}"},
            {"name": "SID", "value": f"sid-{tag}"},
        ]
    }


def _setup_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path, names: list[str]
) -> tuple[AccountStore, AccountService, AccountRotator, AIStudioClient, list[str]]:
    store = AccountStore(tmp_path / "accounts")
    service = AccountService(store)
    ids: list[str] = []
    for idx, name in enumerate(names):
        meta = store.save_account(
            name=name,
            email=f"{name}@example.com",
            storage_state=_storage_state(f"{name}-{idx}"),
            auth_user=str(idx),
        )
        ids.append(meta.id)
    store.set_active_account(ids[0])

    monkeypatch.setattr(settings, "persist_rotator", False)
    rotator = AccountRotator(store)
    client = AIStudioClient()
    active_auth = store.get_auth_path(ids[0])
    client.session.set_ready_context(
        SessionContext(
            auth_file=str(active_auth),
            cookies={"SAPISID": f"sapisid-{names[0]}-0"},
        )
    )

    async def _fake_switch_auth(
        auth_file: str | None,
        *,
        auth_user: str = "0",
        proxy_url: str | None = None,
    ) -> SessionContext | None:
        if auth_file is None:
            client.session._active_context = None
            return None
        ctx = SessionContext(
            auth_file=auth_file,
            auth_user=auth_user,
            proxy_url=proxy_url,
            cookies={"SAPISID": "switched"},
        )
        client.session.set_ready_context(ctx)
        return ctx

    client.session.switch_auth = AsyncMock(side_effect=_fake_switch_auth)  # type: ignore[method-assign]

    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "client", client)
    return store, service, rotator, client, ids


def test_classify_error_identifies_account_verification_gate() -> None:
    jspb_body = json.dumps([7, VERIFY_ERROR_MSG])
    json_body = json.dumps({"error": {"code": 403, "message": VERIFY_ERROR_MSG}})

    err_http = classify_error(403, json_body)
    assert isinstance(err_http, AccountVerificationRequiredError)
    assert isinstance(err_http, AuthError)
    assert is_account_verification_required(err_http)

    # HTTP 200 with embedded gRPC code 7 (business error code 403)
    err_grpc = classify_error(200, jspb_body)
    assert isinstance(err_grpc, AccountVerificationRequiredError)
    assert is_account_verification_required(err_grpc)

    # Plain generic 403 permission error must NOT trigger verification auto-disable
    normal_403 = classify_error(
        403, json.dumps([7, "The caller does not have permission"])
    )
    assert isinstance(normal_403, AuthError)
    assert not isinstance(normal_403, AccountVerificationRequiredError)
    assert not is_account_verification_required(normal_403)


def test_store_persists_verification_required_and_clears_on_reenable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "persist_rotator", False)
    store = AccountStore(tmp_path / "accounts")
    acc = store.save_account(
        name="alpha",
        email="alpha@example.com",
        storage_state=_storage_state("alpha"),
    )
    store.set_active_account(acc.id)

    marked = store.mark_account_verification_required(
        acc.id, required=True, disable=True
    )
    assert marked is not None
    assert marked.enabled is False
    assert marked.verification_required is True

    # Reload from disk to verify persistence in registry.json & meta.json
    reloaded_store = AccountStore(tmp_path / "accounts")
    reloaded = reloaded_store.get_account(acc.id)
    assert reloaded is not None
    assert reloaded.enabled is False
    assert reloaded.verification_required is True

    resp = to_account_response(reloaded)
    assert resp.enabled is False
    assert resp.verification_required is True

    rotator = AccountRotator(reloaded_store)
    status = rotator.get_all_stats()
    assert status[acc.id]["enabled"] is False
    assert status[acc.id]["verification_required"] is True

    # Manually re-enabling clears verification_required flag
    reenabled = reloaded_store.toggle_account_enabled(acc.id, True)
    assert reenabled is not None
    assert reenabled.enabled is True
    assert reenabled.verification_required is False


async def test_nonstream_verification_403_disables_tags_and_switches_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store, _svc, rotator, client, ids = _setup_pool(
        monkeypatch, tmp_path, ["unverified", "healthy"]
    )
    reset_signer_health_for_tests()
    signer_health = get_signer_health()

    called_accounts: list[str] = []

    async def _fake_generate_content(**_kwargs: object) -> ModelOutput:
        assert client.session.active_context is not None
        acc_id = (
            str(client.session.active_context.auth_file)
            .replace("\\", "/")
            .split("/")[-2]
        )
        called_accounts.append(acc_id)
        if acc_id == ids[0]:
            raise classify_error(403, json.dumps([7, VERIFY_ERROR_MSG]))
        return ModelOutput(
            candidates=[Candidate(text="Switched cleanly!", finish_reason=1)],
            model="models/gemini-3.8-flash",
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        )

    client.generate_content = AsyncMock(side_effect=_fake_generate_content)  # type: ignore[method-assign]

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hi")])]
    )
    resp = await gemini_svc.handle_gemini_generate_content(
        "gemini-3.8-flash", req, client, stream=False, anti_truncation=False
    )
    assert not isinstance(resp, gemini_svc.GeminiStreamResponse)

    assert resp.candidates[0].content.parts[0].text == "Switched cleanly!"
    # Must NOT replay/re-sign on ids[0]: exactly 1 attempt on ids[0], then 1 on ids[1]
    assert called_accounts == [ids[0], ids[1]]
    # Account 0 is disabled and marked verification_required
    acc0 = store.get_account(ids[0])
    assert acc0 is not None
    assert acc0.enabled is False
    assert acc0.verification_required is True
    # Account 1 is now active and healthy
    active = store.get_active_account()
    assert active is not None and active.id == ids[1]
    # WAA signer health must NOT be polluted by account verification errors
    assert signer_health.is_degraded() is False
    assert signer_health.state_summary()["recentRejects"] == 0
    # Rotator status exposes verification_required
    assert rotator.get_all_stats()[ids[0]]["verification_required"] is True


async def test_stream_verification_403_disables_tags_and_switches_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store, _svc, _rotator, client, ids = _setup_pool(
        monkeypatch, tmp_path, ["unverified", "healthy"]
    )
    called_accounts: list[str] = []

    async def _fake_stream(**_kwargs: object):
        assert client.session.active_context is not None
        acc_id = (
            str(client.session.active_context.auth_file)
            .replace("\\", "/")
            .split("/")[-2]
        )
        called_accounts.append(acc_id)
        if acc_id == ids[0]:
            raise classify_error(403, json.dumps([7, VERIFY_ERROR_MSG]))
        yield "body", "Stream OK"
        yield "finish_reason", 1

    client.stream_generate_content = _fake_stream  # type: ignore[method-assign]

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hi")])]
    )
    stream_resp = await gemini_svc.handle_gemini_generate_content(
        "gemini-3.8-flash", req, client, stream=True, anti_truncation=False
    )
    assert isinstance(stream_resp, gemini_svc.GeminiStreamResponse)
    frames: list[dict[str, object]] = []
    async for frame in stream_resp.frames:
        frames.append(frame)

    assert called_accounts == [ids[0], ids[1]]
    assert any("Stream OK" in json.dumps(f) for f in frames)
    acc0 = store.get_account(ids[0])
    assert acc0 is not None
    assert acc0.enabled is False
    assert acc0.verification_required is True


async def test_single_account_verification_403_disables_and_does_not_retry_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store, _svc, _rotator, client, ids = _setup_pool(
        monkeypatch, tmp_path, ["only_one"]
    )
    attempts = 0

    async def _always_verify(**_kwargs: object) -> ModelOutput:
        nonlocal attempts
        attempts += 1
        raise classify_error(403, json.dumps([7, VERIFY_ERROR_MSG]))

    client.generate_content = AsyncMock(side_effect=_always_verify)  # type: ignore[method-assign]
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hi")])]
    )

    with pytest.raises(HTTPException) as exc_info:
        await gemini_svc.handle_gemini_generate_content(
            "gemini-3.8-flash", req, client, stream=False, anti_truncation=False
        )

    assert exc_info.value.status_code == 403
    detail = cast(object, exc_info.value.detail)
    assert isinstance(detail, dict)
    assert detail["type"] == "account_verification_required"
    assert attempts == 1  # Strictly no in-place retry!
    acc0 = store.get_account(ids[0])
    assert acc0 is not None
    assert acc0.enabled is False
    assert acc0.verification_required is True


async def test_auth403_manager_short_circuits_verification_without_signer_penalty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store, _svc, _rotator, client, ids = _setup_pool(
        monkeypatch, tmp_path, ["unverified", "backup"]
    )
    reset_signer_health_for_tests()
    signer_health = get_signer_health()

    manager = gemini_svc.Auth403RetryManager()
    manager.serving_account_id = ids[0]
    manager.current_prepared = object()  # type: ignore[assignment]

    exc = AuthError(f"权限被拒绝 (HTTP 403): {VERIFY_ERROR_MSG}")
    retry = await manager.handle_403(exc, model="gemini-3.8-flash", client=client)

    assert retry is True
    assert manager.count == 0
    assert manager.current_prepared is None
    assert signer_health.state_summary()["recentRejects"] == 0
    acc0 = store.get_account(ids[0])
    assert acc0 is not None
    assert acc0.enabled is False
    assert acc0.verification_required is True
    active = store.get_active_account()
    assert active is not None and active.id == ids[1]


async def test_concurrent_verification_failures_switch_once_and_pick_healthy_over_cooled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When acc0 needs verification while acc1 is 429-cooled and acc2 is healthy,
    rotator must pick acc2 (not blindly fall back to registry-first acc1) and
    concurrent failures on acc0 must not cascade extra switches."""
    from aistudio_api.application.account_orchestrator import try_switch_account

    store, _svc, rotator, client, ids = _setup_pool(
        monkeypatch, tmp_path, ["unverified", "cooled", "healthy"]
    )
    rotator.record_rate_limited(ids[1], model="gemini-3.8-flash")

    r1, r2 = await asyncio.gather(
        try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id=ids[0],
            is_verification_required=True,
        ),
        try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id=ids[0],
            is_verification_required=True,
        ),
    )
    assert r1 is True
    assert r2 is True
    acc0 = store.get_account(ids[0])
    assert acc0 is not None
    assert acc0.enabled is False
    assert acc0.verification_required is True
    # Must have picked healthy ids[2], NOT cooled ids[1]
    active = store.get_active_account()
    assert active is not None and active.id == ids[2]
    assert client.session.active_context is not None
    assert ids[2] in str(client.session.active_context.auth_file)


async def test_count_tokens_verification_403_disables_and_switches(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store, _svc, _rotator, client, ids = _setup_pool(
        monkeypatch, tmp_path, ["unverified", "healthy"]
    )

    async def _fake_count_tokens(**_kwargs: object) -> int:
        assert client.session.active_context is not None
        if ids[0] in str(client.session.active_context.auth_file):
            raise classify_error(403, json.dumps([7, VERIFY_ERROR_MSG]))
        return 42

    client.count_tokens = AsyncMock(side_effect=_fake_count_tokens)  # type: ignore[method-assign]
    req = GeminiCountTokensRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])]
    )
    result = await gemini_svc.handle_gemini_count_tokens(
        "gemini-3.8-flash", req, client
    )
    assert result == {"totalTokens": 42}
    acc0 = store.get_account(ids[0])
    assert acc0 is not None
    assert acc0.enabled is False
    assert acc0.verification_required is True
