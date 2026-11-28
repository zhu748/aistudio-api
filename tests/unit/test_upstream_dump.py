"""上游交换快照的脱敏、完整下载、有限预览与环形淘汰。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict

import httpx
import pytest
from fastapi import FastAPI

from aistudio_api.api.routes_operations import router
from aistudio_api.config import settings
from aistudio_api.infrastructure.request_context import RequestContext, current_request
from aistudio_api.infrastructure.request_history import request_history
from aistudio_api.infrastructure.utils import upstream_dump as dump
from aistudio_api.infrastructure.utils.logger import set_dump_dir


@pytest.fixture(autouse=True)
def _isolated_dump_dir(tmp_path):
    set_dump_dir(tmp_path)
    try:
        yield tmp_path
    finally:
        set_dump_dir(None)


def test_utf8_len_matches_encode_for_multibyte():
    for text in ("", "abc", "中文", "😀" * 100, "a\ud800b"):
        assert dump._utf8_len(text) == len(text.encode("utf-8", "replace"))


def test_utf8_len_counts_across_chunk_boundary():
    # 长度跨过 64KiB 分块边界时仍要统计完整，不能少算尾部
    text = "a" * (dump._BODY_COUNT_CHUNK * 2 + 7)
    assert dump._utf8_len(text) == len(text)


def test_sanitize_wire_body_parses_small_json_and_truncates_proof():
    proof = "P" * (dump._PROOF_MIN_CHARS + 100)
    body = json.dumps([proof, "keep", 42])
    parsed = dump._sanitize_wire_body(body)
    assert isinstance(parsed, list)
    assert parsed[0].startswith("<proof truncated len=")
    assert parsed[1:] == ["keep", 42]


def test_sanitize_wire_body_non_json_falls_back_to_head():
    marker = dump._sanitize_wire_body("not-json" * 1000)
    assert isinstance(marker, str)
    assert len(marker) == dump._BODY_HEAD_CHARS


MODEL_URL = "https://x/$rpc/MakerSuiteService/GenerateContent"
COUNT_URL = "https://x/$rpc/MakerSuiteService/CountTokens"


@pytest.mark.parametrize("status", [200, 400])
async def test_large_upstream_snapshot_serves_small_preview_and_full_json(
    monkeypatch, status
):
    monkeypatch.setattr(settings, "dump_max_bytes", 1024)
    monkeypatch.setenv("AISTUDIO_UPSTREAM_SNAPSHOT_KEEP", "1")
    context = RequestContext("req_full_wire")
    request_history().insert(
        {
            **asdict(context),
            "started_at": time.time(),
            "status_code": status,
            "result": "success" if status == 200 else "error",
        }
    )
    nested = ('多轮正文 "[{}]" \\ \n' * 48_000) + "请求末尾"
    proof = "!proof" + "x" * 5000 + "sensitive-proof-tail"
    wire = ["models/demo", [[nested]], None, None, proof, ["保留尾部槽位"]]
    body = json.dumps(wire, separators=(",", ":"), ensure_ascii=False)
    expected_bytes = len(body.encode("utf-8"))
    assert expected_bytes > 1024 * 1024

    def reject_full_parse(_body: str) -> None:
        pytest.fail("the large snapshot must not use the full-body JSON parser")

    monkeypatch.setattr(dump, "_sanitize_wire_body", reject_full_parse)
    token = current_request.set(context)
    try:
        written = dump.save_upstream_exchange(
            url=MODEL_URL,
            wire_headers={"cookie": "request-cookie-secret"},
            body=body,
            status=status,
            response_sample=b"response sample",
            response_headers={"set-cookie": "response-cookie-secret"},
        )
    finally:
        current_request.reset(token)
    assert written is not None

    app = FastAPI()
    app.include_router(router)
    endpoint = f"/api/requests/{context.request_id}/upstream"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        preview = await client.get(endpoint)
        downloaded = await client.get(endpoint, params={"download": 1})

    assert preview.status_code == downloaded.status_code == 200
    assert preview.json()["upstream"]["body_bytes"] == expected_bytes
    assert len(preview.json()["upstream"]["wire_body"]) <= dump._BODY_HEAD_CHARS
    assert "下载快照" in preview.json()["note"]
    assert len(preview.content) < 10_000
    assert downloaded.headers["content-disposition"] == (
        'attachment; filename="req_full_wire.upstream.json"'
    )
    assert downloaded.content == written.read_bytes()
    assert int(downloaded.headers["content-length"]) == len(downloaded.content)
    payload = downloaded.json()
    assert payload["response"]["status"] == status
    assert payload["upstream"]["body_bytes"] == expected_bytes
    actual_wire = payload["upstream"]["wire_body"]
    assert actual_wire[:4] == wire[:4]
    assert actual_wire[4].startswith("<proof truncated len=")
    assert actual_wire[5:] == wire[5:]
    assert b"sensitive-proof-tail" not in downloaded.content
    assert b"request-cookie-secret" not in downloaded.content
    assert b"response-cookie-secret" not in downloaded.content


def test_should_save_exchange_gates_auxiliary_success(monkeypatch):
    monkeypatch.setenv("AISTUDIO_UPSTREAM_SNAPSHOT_KEEP", "3")
    assert dump.should_save_exchange(MODEL_URL, 200) is True
    assert dump.should_save_exchange(COUNT_URL, 200) is False
    # 失败现场无条件保留
    assert dump.should_save_exchange(COUNT_URL, 500) is True


def test_should_save_exchange_respects_keep_zero(monkeypatch):
    monkeypatch.setenv("AISTUDIO_UPSTREAM_SNAPSHOT_KEEP", "0")
    assert dump.should_save_exchange(MODEL_URL, 200) is False
    assert dump.upstream_snapshot_keep() == 0


def test_save_success_snapshot_prunes_ring(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_UPSTREAM_SNAPSHOT_KEEP", "2")
    for index in range(4):
        written = dump.save_upstream_exchange(
            url=MODEL_URL,
            wire_headers=[("x-goog-api-key", "secret-value")],
            body=f'["turn-{index}"]',
            status=200,
            response_sample=b'{"ok":true}',
            response_headers={"set-cookie": "session=abc"},
        )
        assert written is not None
        # 节流只影响淘汰频率，这里强制每次都能收敛到 keep 条
        dump._prune_success_snapshots(tmp_path, 2, force=True)
    remaining = sorted(p.name for p in tmp_path.glob("req_*.upstream.ok.json"))
    assert len(remaining) == 2


def test_saved_snapshot_redacts_credentials_and_counts_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_UPSTREAM_SNAPSHOT_KEEP", "5")
    body = json.dumps(["正文" * 10])
    written = dump.save_upstream_exchange(
        url=MODEL_URL,
        wire_headers={"authorization": "Bearer super-secret"},
        body=body,
        status=200,
        response_sample=b"{}",
    )
    assert written is not None
    snapshot = json.loads(written.read_text(encoding="utf-8"))
    assert snapshot["upstream"]["headers"] == [["authorization", "<redacted len=19>"]]
    assert snapshot["upstream"]["body_bytes"] == len(body.encode("utf-8"))
    assert snapshot["upstream"]["wire_body"] == ["正文" * 10]
    assert "super-secret" not in written.read_text(encoding="utf-8")


def test_error_snapshot_not_overwritten_by_auxiliary_rpc(monkeypatch, tmp_path):
    monkeypatch.setenv("AISTUDIO_UPSTREAM_SNAPSHOT_KEEP", "5")
    from aistudio_api.infrastructure import request_context

    token = request_context.current_request.set(
        request_context.RequestContext(request_id="req_fixed")
    )
    try:
        dump.save_upstream_exchange(
            url=MODEL_URL,
            wire_headers=[],
            body="[]",
            status=500,
            response_sample=b"boom",
        )
        files = list(tmp_path.glob("req_*.upstream.json"))
        assert len(files) == 1
        first = files[0].read_text(encoding="utf-8")

        dump.save_upstream_exchange(
            url=COUNT_URL,
            wire_headers=[],
            body="[]",
            status=500,
            response_sample=b"aux",
        )
    finally:
        request_context.current_request.reset(token)
    files = list(tmp_path.glob("req_*.upstream.json"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == first
