"""第二轮深审（Gemini 管线/账号/wire/管理面）修复的回归测试。"""
from __future__ import annotations

import json

import pytest


def test_redact_text_google_cookie_free_text() -> None:
    from aistudio_api.infrastructure.utils.redaction import redact_text
    for name in ("HSID", "SSID", "SID", "APISID", "SIDCC", "NID"):
        out = redact_text(f"{name}=secretvalue123")
        assert "secretvalue123" not in out, name
    out = redact_text("Cookie: HSID=x; SSID=y; SID=z")
    assert "HSID=x" not in out
    assert "consider" in redact_text("consider this")


def test_mask_yaml_api_keys_structural() -> None:
    from aistudio_api.api.routes_system import _mask_yaml_api_keys
    for raw in (
        "api_keys: [{name: prod, key: sk-flow-secret-value-456}]",
        '{"api_keys": [{"name": "p", "key": "sk-json-secret-789"}]}',
        "api_keys:\n  - sk-plain-list-secret-000",
    ):
        out = _mask_yaml_api_keys(raw)
        assert "sk-flow-secret" not in out
        assert "sk-json-secret" not in out
        assert "sk-plain-list" not in out
    out = _mask_yaml_api_keys(
        "waa:\n  servers:\n    - id: r1\n      headers:\n"
        "        Authorization: Bearer signer-secret\n")
    assert "signer-secret" not in out
    out = _mask_yaml_api_keys("key: [unclosed\n  bad: - x")
    assert "unclosed" not in out


def test_api_key_session_cannot_write_config() -> None:
    from unittest.mock import MagicMock, patch

    from fastapi import HTTPException

    from aistudio_api.api import dependencies as dep

    req = MagicMock()
    req.method = "PUT"
    req.url.path = "/config/yaml"
    req.client.host = "1.2.3.4"
    req.headers = {"authorization": "Bearer sk-fallback-key"}
    with patch.object(dep.settings, "web_password", ""), patch.object(
        dep, "_all_configured_api_keys", return_value=frozenset({"sk-fallback-key"})
    ), pytest.raises(HTTPException) as ei:
        dep.require_web_auth(req)
    assert ei.value.status_code == 403
    req.method = "GET"
    with patch.object(dep.settings, "web_password", ""), patch.object(
        dep, "_all_configured_api_keys", return_value=frozenset({"sk-fallback-key"})
    ):
        dep.require_web_auth(req)  # 只读放行


def test_no_credentials_not_counted_for_rate_limit() -> None:
    from unittest.mock import MagicMock, patch

    from aistudio_api.api import dependencies as dep

    req = MagicMock()
    req.method = "GET"
    req.url.path = "/stats"
    req.client.host = "8.8.8.8"
    req.headers = {}
    with patch.object(dep.settings, "web_password", "pw"), patch.object(
        dep, "_all_configured_api_keys", return_value=set()
    ):
        dep._auth_fail_counts.clear()
        with pytest.raises(dep.HTTPException):
            dep.require_web_auth(req)
        assert dep._auth_fail_counts.get("8.8.8.8") in (None, [])
        dep._auth_fail_counts.clear()


def test_trailing_slash_auth_not_bypassed() -> None:
    import asyncio

    from aistudio_api.api.request_middleware import RequestMiddleware

    captured = {}

    async def app(scope, receive, send):
        captured["path"] = scope["path"]

    mw = RequestMiddleware(app)

    async def run():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        await mw({"type": "http", "method": "GET", "path": "/stats/",
                  "raw_path": b"/stats/", "headers": [], "query_string": b""},
                 receive, lambda m: None)

    asyncio.run(run())
    assert captured["path"] == "/stats"


def test_to_wire_combined_part_keeps_text() -> None:
    from aistudio_api.infrastructure.gateway.wire_types import AistudioPart, PartIndex
    wire = AistudioPart(text="caption", inline_data=("image/png", "aGVsbG8=")).to_wire()
    assert wire[PartIndex.TEXT] == "caption"
    assert wire[PartIndex.INLINE_DATA] == ["image/png", "aGVsbG8="]
    fc = AistudioPart(text="call now", function_call=("f", {"x": 1})).to_wire()
    assert fc[PartIndex.TEXT] == "call now"


def test_decode_part_keeps_text_alongside_payload() -> None:
    from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
    codec = AistudioWireCodec()
    part = codec._decode_part([None, "look at this", ["image/png", "aGVsbG8="]])
    assert part.text == "look at this"
    assert part.inline_data == ("image/png", "aGVsbG8=")


def test_short_tuple_executable_code_still_safe() -> None:
    from aistudio_api.infrastructure.gateway.wire_types import AistudioPart
    AistudioPart(executable_code=("PYTHON",)).to_wire()
    AistudioPart(code_execution_result=("OUTCOME_OK",)).to_wire()


def test_streaming_code_chunk_not_dropped() -> None:
    from aistudio_api.infrastructure.gateway.stream_parser import (
        IncrementalJSONStreamParser,
    )
    from aistudio_api.infrastructure.gateway.wire_parser import parse_response_chunk

    parser = IncrementalJSONStreamParser()
    code_part = [None] * 9
    code_part[8] = [1, "print(1)"]
    chunk = json.dumps([[[[[[[code_part, "model"]]]], []]]])
    parsed = None
    for c in parser.feed(chunk + "\n\n"):
        parsed = c
    assert parsed is not None
    cand = parse_response_chunk(parsed)
    assert cand.code_output


def test_invalid_thinking_level_is_value_error() -> None:
    from aistudio_api.application.chat_service import _parse_thinking_level
    with pytest.raises(ValueError):
        _parse_thinking_level("ULTRA")
    with pytest.raises(ValueError):
        _parse_thinking_level("banana")


def test_media_resolution_string_mapped_to_int() -> None:
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiGenerateContentRequest,
        GeminiGenerationConfig,
        GeminiPart,
    )
    from aistudio_api.application.chat_service import normalize_gemini_request

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hi")])],
        generationConfig=GeminiGenerationConfig(
            mediaResolution="MEDIA_RESOLUTION_HIGH"
        ),
    )
    out = normalize_gemini_request(req, "gemini-2.5-flash")
    gc = out.generation_config_overrides or {}
    assert gc.get("media_resolution") == 3


def test_rotator_partial_config_deep_merged() -> None:
    import pathlib
    import tempfile

    from aistudio_api.infrastructure.gateway import model_defaults as md

    with tempfile.TemporaryDirectory() as td:
        cfg = pathlib.Path(td) / "config.yaml"
        cfg.write_text("rotator:\n  max_retries: 3\n", encoding="utf-8")
        md._CONFIG_CACHE.clear()
        merged = md._load_yaml_config(cfg)
        rot = merged.get("rotator") or {}
        assert rot.get("max_retries") == 3
        assert isinstance(rot.get("cooldowns"), dict) and rot["cooldowns"]
        assert isinstance(rot.get("tier_weights"), dict) and rot["tier_weights"]


def test_delete_account_holds_lock() -> None:
    import pathlib
    import tempfile
    import threading

    from aistudio_api.infrastructure.account.account_store import AccountStore

    errors: list[Exception] = []
    with tempfile.TemporaryDirectory() as td:
        store = AccountStore(accounts_dir=pathlib.Path(td))
        a1 = store.save_account(
            name="A", email=None,
            storage_state={"SAPISID": "x1", "__Secure-1PAPISID": "a"},
        )
        store.save_account(
            name="B", email=None,
            storage_state={"SAPISID": "x2", "__Secure-1PAPISID": "b"},
        )
        [a.id for a in store.list_accounts()]
        stop = threading.Event()

        def churn():
            while not stop.is_set():
                try:
                    store.list_accounts()
                    store.get_active_account()
                except RuntimeError as e:
                    errors.append(e)

        t = threading.Thread(target=churn)
        t.start()
        for _ in range(100):
            store.delete_account(a1.id)
            store.save_account(
                name="A", email=None,
                storage_state={"SAPISID": "x1", "__Secure-1PAPISID": "a"},
            )
        stop.set()
        t.join()
    assert not errors, errors[:2]
