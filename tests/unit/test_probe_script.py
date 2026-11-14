"""Probe the real ASGI adapters offline; validate privacy and failure handling."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import api_service_gemini
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

SPEC = importlib.util.spec_from_file_location(
    "aistudio_probe_script",
    Path(__file__).parents[2] / "scripts" / "probe_aistudio.py",
)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)
SECRET = "private-cookie-proof-api-key-must-not-appear-in-report"
# 上游 thought_signature 字段为 TYPE_BYTES（JSON 表示恒为 base64）；
# 签名往返探测使用 base64 形态的假签名，与真实上游行为一致。
SECRET_SIG = base64.b64encode(SECRET.encode()).decode()


async def test_full_probe_through_real_adapters(monkeypatch, capsys):
    returned_signatures = []

    async def generate(**kwargs):
        parts = [p for content in kwargs["contents"] for p in content.parts]
        prompt = " ".join(p.text or "" for p in parts)
        calls = []
        if any(p.function_response is not None for p in parts):
            returned_signatures.extend(
                p.thought_signature for p in parts if p.thought_signature
            )
            text = "7"
        elif kwargs["tools"]:
            calls = [
                {
                    "name": "probe_sum",
                    "args": {"a": 2, "b": 5},
                    "call_id": "c",
                    "thought_signature": SECRET_SIG,
                }
            ]
            text = ""
        elif "Compute 17" in prompt:
            text = "391"
        elif "Return only this JSON" in prompt:
            text = '{"ok":true,"value":7}'
        else:
            text = "PROBE_OK"
        return ModelOutput(
            candidates=[Candidate(text=text, function_calls=calls, finish_reason=1)],
            usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        )

    async def stream(**_kwargs):
        yield "body", "PROBE_OK"
        yield "finish_reason", 1
        yield "usage", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}

    client = SimpleNamespace(generate_content=generate, stream_generate_content=stream)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_client] = lambda: client
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(model_discovery, "get_models", AsyncMock(return_value=[]))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.ASGITransport(app)),
    )
    monkeypatch.setenv("PROBE_TEST_KEY", SECRET)
    try:
        report = await probe.probe(
            probe.Options(model="test", full=True, key_env="PROBE_TEST_KEY")
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
    assert report["summary"] == {"passed": 25, "failed": 0, "skipped": 0}
    assert returned_signatures == [SECRET_SIG] * 4
    assert SECRET not in json.dumps(report)
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("protocol", probe.PROTOCOLS)
def test_http_200_stream_error_is_failure_and_quota_stops_probe(protocol):
    events = [{"error": {"code": 429, "message": SECRET}}]
    result = probe.summarize(protocol, "stream", {"_events": events, "_done": True})
    assert result["passed"] is False
    assert result["error_class"] == "rate_limited"
    assert probe.should_stop({"http_status": 200}, result)
    assert SECRET not in json.dumps(result)


async def test_unauthorized_gateway_stops_before_generation(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            401, json={"error": {"message": SECRET}}, headers={"set-cookie": SECRET}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )
    report = await probe.probe(probe.Options(model="test", full=True))
    assert len(requests) == 1
    assert report["summary"] == {"passed": 0, "failed": 1, "skipped": 20}
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("protocol", probe.PROTOCOLS)
def test_stream_without_terminal_marker_cannot_pass(protocol):
    result = probe.summarize(protocol, "stream", {"_events": [], "_done": False})
    assert result["passed"] is False


async def test_malformed_json_fails_without_logging_raw_response():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=SECRET, headers={"content-type": "application/json"}
            )
        ),
        base_url="http://test",
    ) as client:
        status, received = await probe.exchange(client, "/test", {}, 1)
    assert not status["transport_ok"] and received == {}
    assert SECRET not in json.dumps(status)


def test_feature_400_does_not_prevent_testing_other_protocols():
    assert not probe.should_stop({"http_status": 400}, {})


@pytest.mark.parametrize("raw", ["[]", "null", "42"])
async def test_non_object_json_is_recorded_as_decode_failure(raw):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)),
        base_url="http://test",
    ) as client:
        status, received = await probe.exchange(client, "/test", {}, 1)
    assert status["transport_ok"] is False and received == {}


@pytest.mark.parametrize("value", ['{"ok":1,"value":7}', '{"ok":true,"value":7.0}'])
def test_json_shape_requires_boolean_and_integer(value):
    result = probe.summarize(
        "responses",
        "json",
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"text": value}]}],
        },
    )
    assert result["passed"] is False


def test_report_write_and_exit_status(tmp_path, monkeypatch, capsys):
    async def fake_probe(_args):
        return {"summary": {"passed": 0, "failed": 1, "skipped": 0}}

    output = tmp_path / "probe-report.json"
    monkeypatch.setattr(probe, "probe", fake_probe)
    monkeypatch.setattr(
        sys, "argv", ["probe_aistudio.py", "--model", "test", "--out", str(output)]
    )
    assert probe.main() == 1
    assert (
        cast(dict[str, Any], json.loads(output.read_text()))["summary"]["failed"] == 1
    )
    assert not list(tmp_path.glob(".probe-*"))
    assert SECRET not in capsys.readouterr().out
