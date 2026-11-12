"""Operator-facing metrics use client outcomes and retain per-call diagnostics."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.routes_operations import (
    request_payload,
    router as operations_router,
)
from aistudio_api.api.schemas import (
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
)
from aistudio_api.application.account_rotator import (
    AccountModelDailyStats,
    AccountStats,
)
from aistudio_api.application.api_service_gemini import handle_gemini_generate_content
from aistudio_api.application.compat_service import (
    claude_stream_adapter,
    openai_stream_adapter,
)
from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, GeneratedImage, ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.request_context import (
    RequestContext,
    active_requests,
    current_request,
    record_output_timing,
    record_requested_parameters,
    record_result,
    upstream_attempt,
)
from aistudio_api.infrastructure.request_history import RequestHistory, request_history
from aistudio_api.infrastructure.utils import logger as log_module


@pytest.fixture(autouse=True)
def observation_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "persist_stats", False)
    monkeypatch.setattr(settings, "persist_rotator", False)
    monkeypatch.setattr(settings, "request_history", True)
    monkeypatch.setattr(log_module, "_dump_requests_override", False)
    monkeypatch.setattr(log_module, "_dump_to_file_override", False)


async def test_retry_success_counts_one_client_call_and_two_attempts():
    app = FastAPI()
    app.add_middleware(RequestMiddleware)

    @app.post("/v1/messages")
    async def generate(request: Request):
        await request.json()
        context = current_request.get()
        assert context is not None
        context.model = "models/test"
        with upstream_attempt("/accounts/first/auth.json", "0", "") as attempt:
            attempt["status_code"] = 429
            record_result("rate_limited")
        with upstream_attempt("/accounts/second/auth.json", "0", "") as attempt:
            attempt["status_code"] = 200
            record_result("success", usage={"total_tokens": 25})
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/messages", json={"model": "client-alias"})
    record = request_history().get(response.headers["x-request-id"])
    assert record is not None and record["requested_model"] == "client-alias"
    metrics = request_history().metrics(since=time.time() - 60, until=time.time())
    totals = cast(dict[str, object], metrics["totals"])
    assert totals["requests"] == 1 and totals["success"] == 1
    assert totals["success_rate"] == 100 and totals["attempts"] == 2
    assert totals["retried"] == 1 and totals["total_tokens"] == 25


async def test_running_call_is_visible_without_inflating_completed_metrics():
    app = FastAPI()
    app.add_middleware(RequestMiddleware)
    entered, release = asyncio.Event(), asyncio.Event()

    @app.post("/v1/messages")
    async def generate():
        entered.set()
        await release.wait()
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.post("/v1/messages", json={}))
        await entered.wait()
        live = active_requests()
        assert len(live) == 1 and live[0]["result"] == "running"
        assert request_history().get(str(live[0]["request_id"])) is not None
        assert request_history().query()["total"] == 0
        assert request_history().clear() == 0
        release.set()
        await pending
    assert not active_requests()
    assert request_history().query()["total"] == 1


def test_daily_429_total_survives_success_reset_and_reload():
    account = AccountStats(account_id="test")
    account.record_rate_limited("test-model")
    account.record_success("test-model")
    account.clear_cooldown("test-model")
    reloaded = AccountStats.from_dict(account.to_dict())
    assert reloaded.daily_models["test-model"].rate_limited == 0
    assert reloaded.daily_models["test-model"].rate_limited_total == 1
    assert AccountModelDailyStats.from_dict({"rate_limited": 3}).rate_limited_total == 3


def test_interrupted_calls_recover_and_are_counted_once(tmp_path):
    history = RequestHistory(tmp_path / "requests.sqlite3")
    record = {
        **asdict(RequestContext("req_restart", model="m")),
        "started_at": time.time(),
        "result": "running",
        "status_code": 0,
    }
    history.insert(record)
    assert history.recover_interrupted() == 1
    assert history.recover_interrupted() == 0
    item = history.get("req_restart")
    assert item is not None and item["result"] == "interrupted"
    assert history.query(result="failed")["total"] == 1


def test_request_parameters_are_safe_and_effective_thinking_is_recorded():
    context = RequestContext("req_parameters", category="model")
    token = current_request.set(context)
    try:
        record_requested_parameters(
            {
                "reasoning_effort": "low",
                "thinking": {"budget_tokens": 512, "secret": "never-store"},
                "messages": "never-store",
                "authorization": "never-store",
            }
        )
        AistudioWireCodec().build_request(
            model="test",
            generation_config_overrides={"thinking_config": [1, 1024, None, 3]},
            temperature=0.7,
            max_tokens=2048,
        )
        assert context.request_parameters["reasoning_effort"] == "low"
        assert context.effective_parameters["thinking_level"] == "high"
        assert context.effective_parameters["thinking_budget"] == 1024
        assert context.effective_parameters["max_tokens"] == 2048
        assert "never-store" not in json.dumps(asdict(context))
        record_output_timing("thinking")
        record_output_timing("body")
        assert (
            context.first_thought_ms is not None
            and context.first_content_ms is not None
        )
        assert context.first_thought_ms <= context.first_content_ms
    finally:
        current_request.reset(token)


def test_log_category_and_request_id_search_exclude_management_noise():
    log_module.setup_logging("INFO")
    log_module.get_logger("routes.system").info("management marker")
    token = current_request.set(
        RequestContext(
            "req_category_test", category="model", model="test", account_id="account"
        )
    )
    try:
        log_module.get_logger("session").warning("signing failed")
    finally:
        current_request.reset(token)
    entries = log_module.get_recent_logs(category="model", search="req_category_test")
    assert len(entries) == 1 and entries[0]["account_id"] == "account"
    assert "management marker" not in str(entries)


def test_metrics_filters_percentiles_and_no_data(tmp_path):
    history = RequestHistory(tmp_path / "metrics.sqlite3")
    now = time.time()
    for index, elapsed in enumerate([100, 200, 300]):
        history.insert(
            {
                **asdict(
                    RequestContext(
                        f"req_metric_{index}",
                        model="models/m",
                        account_id="a",
                        protocol="openai",
                    )
                ),
                "started_at": now,
                "result": "success",
                "status_code": 200,
                "elapsed_ms": elapsed,
                "first_byte_ms": elapsed / 2,
            }
        )
    metrics = history.metrics(since=now - 1, until=now + 1, model="m")
    totals = cast(dict[str, object], metrics["totals"])
    assert totals["latency_p50_ms"] == 200 and totals["latency_p95_ms"] == 290
    assert history.query(model="m", protocol="openai", min_elapsed_ms=200)["total"] == 2
    assert history.query(retried=True)["total"] == 0
    empty = cast(
        dict[str, object], history.metrics(since=now + 2, until=now + 3)["totals"]
    )
    assert empty["success_rate"] is None and empty["latency_p95_ms"] is None


async def test_payload_endpoint_rejects_paths_and_redacts_existing_dumps(
    monkeypatch, tmp_path
):
    history = request_history()
    history.insert(
        {
            **asdict(RequestContext("req_payload", model="m")),
            "started_at": time.time(),
            "result": "success",
            "status_code": 200,
        }
    )
    folder = tmp_path / "dumps"
    folder.mkdir()
    monkeypatch.setattr(log_module, "_dump_dir_override", str(folder))
    (folder / "req_payload.json").write_text(
        json.dumps({"body": {"password": "sensitive", "temperature": 0.7}})
    )
    result = await request_payload("req_payload")
    assert result["available"] is True and "sensitive" not in json.dumps(result)
    with pytest.raises(HTTPException):
        await request_payload("../private")


@pytest.mark.parametrize("protocol", ["openai", "claude"])
async def test_stream_adapter_failure_overrides_prior_success(protocol):
    async def broken_source():
        yield 'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}\n\n'
        raise RuntimeError("adapter source failed")

    context = RequestContext("req_adapter", category="model", result="success")
    token = current_request.set(context)
    try:
        stream = (
            openai_stream_adapter(
                broken_source(), requested_model="test", include_usage=True
            )
            if protocol == "openai"
            else claude_stream_adapter(
                broken_source(), requested_model="test", stop_sequences=None
            )
        )
        chunks = [chunk async for chunk in stream]
        assert chunks and context.result == "error"
        assert "adapter source failed" in context.error
    finally:
        current_request.reset(token)


async def test_metrics_fixed_routes_and_anonymous_filters():
    app = FastAPI()
    app.include_router(operations_router)
    now = time.time()
    request_history().insert(
        {
            **asdict(RequestContext("req_route")),
            "started_at": now,
            "result": "error",
            "status_code": 401,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/api/requests/stats")
        assert (
            response.status_code == 200 and response.json()["totals"]["requests"] == 1
        )
        response = await client.get(
            "/api/requests",
            params={
                "model": "__unknown__",
                "account_id": "__unassigned__",
                "key_id": "__anonymous__",
            },
        )
        assert response.status_code == 200 and response.json()["total"] == 1
        invalid = await client.get(
            "/api/requests/stats", params={"since": now + 100, "until": now}
        )
        assert invalid.status_code == 400


async def test_gemini_nested_reasoning_effort_is_retained_before_stream_starts():
    context = RequestContext("req_gemini_effort", category="model")
    token = current_request.set(context)
    try:
        request = GeminiGenerateContentRequest(
            contents=[], generationConfig=GeminiGenerationConfig(reasoningEffort="high")
        )
        await handle_gemini_generate_content(
            "test", request, cast(AIStudioClient, object()), stream=True
        )
        assert context.request_parameters["reasoningEffort"] == "high"
    finally:
        current_request.reset(token)


@pytest.mark.parametrize("with_answer", [False, True])
async def test_buffered_thought_images_are_separate_from_answer_timing(
    monkeypatch, with_answer
):
    from aistudio_api.api.state import runtime_state
    from aistudio_api.application import api_service_gemini as service

    monkeypatch.setattr(service, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(runtime_state, "account_service", None)
    monkeypatch.setattr(runtime_state, "rotator", None)
    output = ModelOutput(
        candidates=[
            Candidate(
                text="answer" if with_answer else "",
                reasoning_images=[GeneratedImage("image/png", b"image", 5)],
            )
        ]
    )
    client = cast(
        AIStudioClient, SimpleNamespace(generate_content=AsyncMock(return_value=output))
    )
    context = RequestContext("req_buffer_timing", category="model")
    token = current_request.set(context)
    try:
        await handle_gemini_generate_content(
            "test",
            GeminiGenerateContentRequest.model_validate(
                {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
            ),
            client,
            stream=False,
            anti_truncation=False,
        )
        assert context.first_thought_ms is not None
        assert (context.first_content_ms is not None) == with_answer
    finally:
        current_request.reset(token)
