"""Regression coverage for operational metadata, isolation and safety boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from aistudio_api.api.dependencies import require_api_key
from aistudio_api.api.request_middleware import RequestMiddleware
from aistudio_api.api.state import RuntimeState
from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import (
    current_request,
    record_result,
    upstream_attempt,
)
from aistudio_api.infrastructure.request_history import RequestHistory, request_history
from aistudio_api.infrastructure.safe_media import (
    PublicMediaTransport,
    resolve_public_address,
)
from aistudio_api.infrastructure.utils import logger as log_module
from aistudio_api.infrastructure.utils.log_files import (
    read_log_history,
    start_file_logging,
    stop_file_logging,
)
from aistudio_api.infrastructure.waa.signer import RemoteProofSigner, WaaSigner


@pytest.fixture(autouse=True)
def operations_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(log_module, "_dump_requests_override", False)
    monkeypatch.setattr(log_module, "_dump_to_file_override", False)
    monkeypatch.setattr(settings, "persist_stats", False)
    monkeypatch.setattr(settings, "request_history", True)


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_error_event_is_counted():
    state = RuntimeState()
    state.record("model", "error")
    assert state.model_stats["model"].errors == 1


def test_admin_password_protects_api_without_keys(monkeypatch):
    from fastapi import HTTPException

    from aistudio_api.api import dependencies

    monkeypatch.setattr(settings, "web_password", "admin-test")
    monkeypatch.setattr(dependencies, "_all_configured_api_keys", lambda: frozenset())
    scope = {
        "type": "http",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 1),
    }
    with pytest.raises(HTTPException) as error:
        require_api_key(Request(scope))
    assert error.value.status_code == 401
    scope["headers"] = [(b"authorization", b"Bearer admin-test")]
    require_api_key(Request(scope))


def test_unconfigured_api_fails_closed(monkeypatch):
    from fastapi import HTTPException

    from aistudio_api.api import dependencies

    monkeypatch.setattr(settings, "web_password", "")
    monkeypatch.setattr(settings, "allow_no_auth", False)
    monkeypatch.setattr(dependencies, "_all_configured_api_keys", lambda: frozenset())
    scope = {"type": "http", "headers": [], "query_string": b""}
    with pytest.raises(HTTPException) as error:
        require_api_key(Request(scope))
    assert error.value.status_code == 503


async def test_stream_final_result_and_request_id_survive_child_tasks():
    app = FastAPI()
    app.add_middleware(RequestMiddleware)

    @app.post("/v1/chat/completions")
    async def route():
        async def events():
            context = current_request.get()
            assert context is not None
            context.model = "models/test"
            with upstream_attempt(
                "/accounts/a/auth.json", "0", "http://u:secret@proxy:80"
            ) as attempt:
                attempt["status_code"] = 200
                yield "data: partial\n\n"
                await asyncio.sleep(0.02)
                record_result(
                    "error", error="mid-stream failure", usage={"total_tokens": 7}
                )
                yield 'data: {"error":"failed"}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    async with client_for(app) as client:
        response = await client.post("/v1/chat/completions", json={"stream": True})
    record = request_history().get(response.headers["x-request-id"])
    assert record is not None
    assert record["status_code"] == 200 and record["result"] == "error"
    assert record["model"] == "models/test" and record["account_id"] == "a"
    assert record["usage"] == {"total_tokens": 7}
    assert float(str(record["elapsed_ms"])) >= 20
    assert float(str(record["first_byte_ms"])) < float(str(record["elapsed_ms"]))
    assert "secret" not in json.dumps(record)
    assert current_request.get() is None


async def test_file_only_dump_and_management_exclusion(monkeypatch, tmp_path):
    app = FastAPI()
    app.add_middleware(RequestMiddleware)
    monkeypatch.setattr(log_module, "_dump_to_file_override", True)
    monkeypatch.setattr(log_module, "_dump_dir_override", str(tmp_path / "dumps"))

    @app.post("/v1/chat/completions")
    async def model_route(request: Request):
        await request.body()
        return {"reply": "hello"}

    @app.post("/config/yaml")
    async def config_route():
        return {"yaml_content": "secret"}

    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions", json={"messages": [], "api_key": "sensitive"}
        )
        await client.post("/config/yaml", json={"yaml_content": "api_keys: secret"})
    files = list((tmp_path / "dumps").glob("req_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["req_id"] == response.headers["x-request-id"]
    assert "sensitive" not in files[0].read_text()
    assert data["response_body"] == {"reply": "hello"}


async def test_dump_payload_limit_marks_instead_of_saving_partial_json(
    monkeypatch, tmp_path
):
    app = FastAPI()
    app.add_middleware(RequestMiddleware)
    monkeypatch.setattr(log_module, "_dump_to_file_override", True)
    monkeypatch.setattr(log_module, "_dump_dir_override", str(tmp_path / "dumps"))
    monkeypatch.setattr(settings, "dump_max_bytes", 16)

    @app.post("/v1/messages")
    async def route(request: Request):
        await request.body()
        return {"long": "x" * 64}

    async with client_for(app) as client:
        await client.post("/v1/messages", json={"long": "x" * 64})
    data = json.loads(next((tmp_path / "dumps").glob("req_*.json")).read_text())
    assert data["request_body_truncated"] is True
    assert data["response_body_truncated"] is True
    assert data["body"] is None and data["response_body"] is None


def test_dump_retention_enforces_count_age_and_total_size(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dump_max_files", 10)
    monkeypatch.setattr(settings, "dump_max_age_days", 3)
    monkeypatch.setattr(settings, "dump_max_total_bytes", 100)
    monkeypatch.setattr(log_module, "_last_dump_cleanup", 0)
    folder = tmp_path / "dumps"
    folder.mkdir()
    now = time.time()
    for index in range(3):
        path = folder / f"req_{index}.json"
        path.write_bytes(b"x" * 50)
        path.touch()
        os.utime(path, (now - (2 - index), now - (2 - index)))
    (folder / "req_old.json").write_bytes(b"old")
    os.utime(folder / "req_old.json", (now - 4 * 86400, now - 4 * 86400))
    (folder / "latest_request.json").write_bytes(b"y" * 30)
    os.utime(folder / "latest_request.json", (now, now))

    log_module._prune_dumps(folder)

    kept = list(folder.glob("req_*.json"))
    total = sum(path.stat().st_size for path in folder.iterdir())
    assert len(kept) == 1 and kept[0].name == "req_2.json"
    assert total <= settings.dump_max_total_bytes


def test_explicit_false_file_setting_survives_restart(monkeypatch):
    from aistudio_api.infrastructure.gateway import model_defaults

    monkeypatch.setattr(log_module, "_dump_to_file_override", None)
    monkeypatch.setattr(settings, "dump_to_file", False)
    monkeypatch.setattr(
        model_defaults,
        "get_configured_logging_settings",
        lambda: {"dump_requests": True, "dump_to_file": False},
    )
    assert log_module.is_dump_to_file_enabled() is False


def test_history_pagination_filters_retention_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_max_rows", 2)
    path = tmp_path / "history.sqlite3"
    history = RequestHistory(path)
    for number in range(4):
        history._last_cleanup = 0
        history.insert(
            {
                "request_id": str(number),
                "started_at": time.time() + number,
                "model": "m",
                "account_id": "a",
                "key_id": "k",
                "result": "success",
                "status_code": 200,
            }
        )
    restarted = RequestHistory(path)
    result = restarted.query(page_size=1, model="m", key_id="k")
    assert (
        result["total"] == 2
        and cast(list[dict[str, object]], result["items"])[0]["request_id"] == "3"
    )
    assert (
        cast(list[dict[str, object]], restarted.query(page=2, page_size=1)["items"])[0][
            "request_id"
        ]
        == "2"
    )
    assert restarted.query(search="' OR 1=1 --")["total"] == 0
    assert restarted.clear() == 2
    assert restarted.query()["total"] == 0


async def test_concurrent_request_contexts_do_not_mix():
    app = FastAPI()
    app.add_middleware(RequestMiddleware)

    @app.post("/v1/messages")
    async def route(request: Request):
        data = await request.json()
        context = current_request.get()
        assert context is not None
        context.model = data["model"]
        await asyncio.sleep(0.01)
        record_result("success")
        return {"ok": True}

    async with client_for(app) as client:
        responses = await asyncio.gather(
            *[client.post("/v1/messages", json={"model": str(i)}) for i in range(8)]
        )
    for number, response in enumerate(responses):
        record = request_history().get(response.headers["x-request-id"])
        assert record is not None
        assert record["model"] == str(number)


async def test_body_limit_rejects_before_handler(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 10)
    called = False
    app = FastAPI()
    app.add_middleware(RequestMiddleware)

    @app.post("/v1/messages")
    async def route():
        nonlocal called
        called = True
        return {}

    async with client_for(app) as client:
        response = await client.post("/v1/messages", content="x" * 11)
    assert response.status_code == 413 and not called


@pytest.mark.parametrize("started", [False, True])
async def test_timeout_closes_response_and_records_final_state(monkeypatch, started):
    monkeypatch.setattr(settings, "request_timeout", 0.02)

    async def slow_app(scope, receive, send):
        if started:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"data: first\n\n",
                    "more_body": True,
                }
            )
        await asyncio.sleep(10)

    async with client_for(RequestMiddleware(slow_app)) as client:
        response = await client.post("/v1/messages", json={})
    assert response.status_code == (200 if started else 504)
    record = request_history().get(response.headers["x-request-id"])
    assert record is not None
    assert record["result"] == "timeout"
    assert "timeout" in response.text


async def test_upstream_timeout_is_not_mislabeled_as_total_timeout():
    app = FastAPI()
    app.add_middleware(RequestMiddleware)

    @app.post("/v1/messages")
    async def route():
        raise TimeoutError("an upstream operation timed out")

    async with client_for(app) as client:
        response = await client.post("/v1/messages", json={})
    record = request_history().get(response.headers["x-request-id"])
    assert response.status_code == 500
    assert record is not None and record["result"] == "error"


async def test_concurrency_limit_is_released(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_requests", 1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_app(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestMiddleware(blocked_app)
    async with client_for(middleware) as client:
        first = asyncio.create_task(client.post("/v1/messages"))
        await entered.wait()
        second = await client.post("/v1/messages")
        assert second.status_code == 429
        release.set()
        assert (await first).status_code == 200
        assert (await client.post("/v1/messages")).status_code == 200
    assert middleware.active == 0


async def test_cancelled_stream_is_persisted_and_context_reset():
    started = asyncio.Event()

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        started.set()
        await asyncio.Event().wait()

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    middleware = RequestMiddleware(app)
    scope = {"type": "http", "method": "POST", "path": "/v1/messages", "headers": []}
    task = asyncio.create_task(middleware(scope, receive, send))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = cast(list[dict[str, object]], request_history().query()["items"])[0]
    assert record["result"] == "cancelled" and middleware.active == 0
    assert current_request.get() is None


async def test_signer_receives_proxy_and_shared_secret(monkeypatch):
    monkeypatch.setattr(settings, "signer_secret", "shared")
    monkeypatch.setattr(settings, "signer_secret_url", "http://signer")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer shared"
        return httpx.Response(200, json={"ok": True, "proof": "!signed"})

    signer = RemoteProofSigner(server_url="http://signer")
    signer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert (
            await signer.mint_proof(
                cookies={"SAPISID": "dummy"}, upstream_proxy="socks5://host:1080"
            )
            == "!signed"
        )
        assert seen["proxy"] == "socks5://host:1080"
    finally:
        await signer.close()


async def test_signer_shutdown_closes_retired_clients(monkeypatch):
    from aistudio_api.infrastructure.waa import signer as signer_module

    class StubSigner:
        def __init__(self, server_url: str, proxy_url: str | None = None):
            self.server_url = server_url
            self.proxy_url = proxy_url
            self.closed = False

        async def close(self):
            self.closed = True

    instances: list[StubSigner] = []

    def create_signer(server_url: str, proxy_url: str | None = None) -> StubSigner:
        instance = StubSigner(server_url, proxy_url)
        instances.append(instance)
        return instance

    monkeypatch.setattr(signer_module, "RemoteProofSigner", create_signer)
    signer = WaaSigner(proof_server_url="http://signer-one")
    await signer.reconfigure(server_url="http://signer-two")
    await signer.close()
    assert len(instances) == 2
    assert all(instance.closed for instance in instances)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"]
)
async def test_media_rejects_private_dns_answers(monkeypatch, address):
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(0, 0, 0, "", (address, 443))]),
    )
    with pytest.raises(ValueError, match="public"):
        await resolve_public_address("media.example", 443)


async def test_media_pins_checked_ip_and_preserves_tls_host(monkeypatch):
    from aistudio_api.infrastructure import safe_media

    monkeypatch.setattr(
        safe_media, "resolve_public_address", AsyncMock(return_value="8.8.8.8")
    )
    captured = []

    async def send(_self, request):
        captured.append(request)
        return httpx.Response(200, content=b"image")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async with httpx.AsyncClient(transport=PublicMediaTransport()) as client:
        await client.get("https://media.example/image")
    assert captured[0].url.host == "8.8.8.8"
    assert captured[0].headers["host"] == "media.example"
    assert captured[0].extensions["sni_hostname"] == "media.example"


async def test_media_redirect_is_resolved_and_checked_again(monkeypatch):
    from aistudio_api.infrastructure import safe_media

    async def resolve(host: str, _port: int) -> str:
        if host == "media.example":
            return "8.8.8.8"
        raise ValueError("Media URL must resolve only to public addresses")

    monkeypatch.setattr(safe_media, "resolve_public_address", resolve)

    async def send(_self, request):
        return httpx.Response(
            302,
            headers={"location": "http://private.example/metadata"},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async with httpx.AsyncClient(
        transport=PublicMediaTransport(), follow_redirects=True
    ) as client:
        with pytest.raises(ValueError, match="public"):
            await client.get("http://media.example/image")


def test_persistent_logs_redact_and_associate_request_id():
    from aistudio_api.infrastructure.request_context import RequestContext

    start_file_logging()
    token = current_request.set(RequestContext("req_test_log"))
    try:
        logging.getLogger("aistudio.test").warning(
            "proxy=http://user:secret@host:80 /api-keys/top-secret"
        )
    finally:
        current_request.reset(token)
        stop_file_logging()
    result = read_log_history(request_id="req_test_log")
    assert result["total"] == 1
    text = json.dumps(result)
    assert "secret" not in text and "***" in text
