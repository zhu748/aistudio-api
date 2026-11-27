"""上游交换快照（upstream_dump）的解析上限与环形淘汰回归测试。

背景：成功快照默认开启（logging.upstream_snapshot_keep=10）。长会话请求
体涨到 MB 级后，``json.loads`` 整棵树再加 ``json.dumps(indent=1)`` 会在
线程池里再放大 3~4 倍正文，与请求路径自身的拷贝叠加把 512MB 档容器顶过
OOM 阈值。修复（2026-11-27）：超过 ``_MAX_BODY_PARSE_CHARS`` 只保留头部
样本，``body_bytes`` 改为分块计数。本文件锁定这些行为，防止回退。
"""

from __future__ import annotations

import json

import pytest

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


def test_sanitize_wire_body_does_not_parse_oversized_body():
    # 构造一个合法 JSON 但超过解析上限的正文：必须走截断分支，
    # 不能返回 list（否则说明真的做了 json.loads）。
    huge = json.dumps(["x" * (dump._MAX_BODY_PARSE_CHARS + 10)])
    marker = dump._sanitize_wire_body(huge)
    assert isinstance(marker, str)
    assert "truncated" in marker
    assert f"limit {dump._MAX_BODY_PARSE_CHARS}" in marker


def test_sanitize_wire_body_non_json_falls_back_to_head():
    marker = dump._sanitize_wire_body("not-json" * 1000)
    assert isinstance(marker, str)
    assert len(marker) == dump._BODY_HEAD_CHARS


MODEL_URL = "https://x/$rpc/MakerSuiteService/GenerateContent"
COUNT_URL = "https://x/$rpc/MakerSuiteService/CountTokens"


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
