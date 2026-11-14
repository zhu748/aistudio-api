"""Client captures must survive validation and protocol conversion byte for byte."""

from __future__ import annotations

import base64
import json
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from aistudio_api.api import routes_compat, routes_gemini
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.routes_operations import (
    request_payload,
    router as operations_router,
)
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.config import settings
from aistudio_api.infrastructure.utils import logger as log_module


@pytest.fixture(autouse=True)
def capture_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "request_history", True)
    monkeypatch.setattr(log_module, "_dump_dir_override", str(tmp_path / "dumps"))
    monkeypatch.setattr(log_module, "_dump_to_file_override", True)
    monkeypatch.setattr(log_module, "_dump_requests_override", False)


@pytest.mark.parametrize("protocol", ["openai", "claude", "responses", "gemini"])
@pytest.mark.parametrize("stream", [False, True])
async def test_original_body_survives_real_protocol_routes(
    monkeypatch, protocol, stream
):
    app = FastAPI()
    app.add_middleware(RequestMiddleware)
    app.include_router(routes_compat.router)
    app.include_router(routes_gemini.router)
    app.include_router(operations_router)
    app.dependency_overrides[get_client] = object
    converted: list[GeminiGenerateContentRequest] = []
    frame = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]
    }

    async def generate(model, req, client, **kwargs):
        converted.append(req)
        if kwargs["stream"]:
            return StreamingResponse(
                iter([f"data: {json.dumps(frame)}\n\n"]),
                media_type="text/event-stream",
            )
        return GeminiGenerateContentResponse.model_validate(frame)

    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", generate)
    monkeypatch.setattr(routes_gemini, "handle_gemini_generate_content", generate)
    schema = {"type": "object", "properties": {"password": {"type": "string"}}}
    payload: dict[str, object] = {"model": "gpt-4o-mini", "stream": stream}
    if protocol == "openai":
        path = "/v1/chat/completions"
        payload["messages"] = [
            {"role": "system", "content": "角色卡"},
            {"role": "user", "content": "你好 Bearer prompt-text"},
        ]
        payload["tools"] = [
            {"type": "function", "function": {"name": "f", "parameters": schema}}
        ]
    elif protocol == "claude":
        path = "/v1/messages"
        payload.update(
            model="claude-opus",
            system="角色卡",
            messages=[{"role": "user", "content": "你好"}],
            tools=[{"name": "f", "input_schema": schema}],
        )
    elif protocol == "responses":
        path = "/v1/responses"
        payload.update(instructions="角色卡", input="你好")
    else:
        operation = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1beta/models/test:{operation}"
        payload = {"contents": [{"role": "user", "parts": [{"text": "你好"}]}]}
    # Deliberately preserve CRLF, duplicate unknown keys, Unicode escapes,
    # scientific notation, null and a field name redaction would corrupt.
    raw = (
        '{\r\n "extension": 1e-03, "extension": {"password":"keep-me"},\r\n'
        ' "escaped": "\\u4f60\\u597d", "explicit_null": null,\r\n'
        + json.dumps(payload, ensure_ascii=False)[1:-1]
        + "\r\n}\n"
    ).encode()

    async def chunks():
        # Split even UTF-8 characters across ASGI messages.
        for offset in range(0, len(raw), 13):
            yield raw[offset : offset + 13]

    target = path + "/?tag=a&tag=b&key=transport-secret&escaped=%2f%20"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            target,
            content=chunks(),
            headers=[
                ("content-type", "application/json"),
                ("authorization", "Bearer transport-secret"),
                ("cookie", "session=transport-secret"),
                ("x-api-key", "transport-secret"),
                ("x-client-tag", "first"),
                ("x-client-tag", "second"),
            ],
        )
        assert response.status_code == 200, response.text
        request_id = response.headers["x-request-id"]
        result = await client.get(f"/api/requests/{request_id}/payload")
    assert result.status_code == 200
    captured = result.json()["payload"]["client_request"]
    assert captured["source"] == "client"
    assert captured["body"].encode() == raw
    assert captured["body_encoding"] == "utf-8"
    assert captured["body_bytes"] == len(raw)
    assert captured["complete"] is True and captured["truncated"] is False
    assert captured["target"] == target.replace("transport-secret", "***")
    assert [item for item in captured["headers"] if item[0] == "x-client-tag"] == [
        ["x-client-tag", "first"],
        ["x-client-tag", "second"],
    ]
    assert "transport-secret" not in json.dumps(captured)
    assert len(converted) == 1
    assert "extension" not in converted[0].model_dump()
    saved = json.loads((log_module.get_dump_dir() / f"{request_id}.json").read_text())
    assert saved["client_request"]["body"].encode() == raw
    assert (
        saved["response_body"] is None if stream else saved["response_body"] is not None
    )


@pytest.mark.parametrize("raw", [b'{"model":', b"not-json\r\n", b"\xff\x00\xfe"])
async def test_validation_failure_still_keeps_original_bytes(raw):
    app = FastAPI()
    app.add_middleware(RequestMiddleware)
    app.include_router(routes_compat.router)
    app.dependency_overrides[get_client] = object
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=raw,
            headers={"content-type": "application/json"},
        )
    assert response.status_code in (400, 422)
    result = await request_payload(response.headers["x-request-id"])
    payload = cast(dict[str, object], result["payload"])
    captured = cast(dict[str, object], payload["client_request"])
    body = str(captured["body"])
    restored = (
        base64.b64decode(body)
        if captured["body_encoding"] == "base64"
        else body.encode()
    )
    assert restored == raw and captured["complete"] is True


@pytest.mark.parametrize(
    "mode", ["disabled", "console_only", "over_limit", "unread", "management"]
)
async def test_capture_limits_and_scope(monkeypatch, mode):
    monkeypatch.setattr(
        log_module, "_dump_to_file_override", mode not in ("disabled", "console_only")
    )
    monkeypatch.setattr(log_module, "_dump_requests_override", mode == "console_only")
    monkeypatch.setattr(
        settings, "dump_max_bytes", 16 if mode == "over_limit" else 1024
    )
    app = FastAPI()
    app.add_middleware(RequestMiddleware)
    path = "/api/config" if mode == "management" else "/v1/messages"

    @app.post(path)
    async def accept(request: Request):
        if mode != "unread":
            await request.body()
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, content=b'{"messages": "original payload"}')
    files = list(log_module.get_dump_dir().glob("*.json"))
    if mode in ("disabled", "console_only", "management"):
        assert not files
        return
    result = await request_payload(response.headers["x-request-id"])
    payload = cast(dict[str, object], result["payload"])
    captured = cast(dict[str, object], payload["client_request"])
    assert captured["complete"] is False
    assert captured["truncated"] is (mode == "over_limit")
    assert captured["body"] is None if mode == "over_limit" else captured["body"] == ""
