"""第七轮审查修复的回归测试（掩码 / Cookie 新鲜度 / keepalive env>yaml / 停止词等）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.requests import Request

from aistudio_api.api.routes_accounts import to_account_response
from aistudio_api.config import (
    discover_keepalive_allow_private,
    discover_keepalive_interval,
)
from aistudio_api.domain.tool_signatures import ThoughtSignatureCache
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.utils.log_files import read_log_history
from aistudio_api.infrastructure.utils.redaction import redact_text

# ─── 账号响应代理凭据掩码（API Key 回退会话） ────────────────────────────


class _AccountStub:
    id = "acc_1"
    name = "stub"
    email = None
    created_at = "2026-11-23T00:00:00"
    last_used = None
    auth_user = "0"
    cookie_id = "cookie_x"
    tier = "Free"
    enabled = True
    group = "default"
    notes = ""
    proxy_url = "http://user:secretPass@proxy.example.com:8080"


def _api_key_request() -> Request:
    return Request({"type": "http", "method": "GET", "headers": [], "path": "/"})


@pytest.mark.asyncio
async def test_account_response_masks_proxy_for_api_key_session(monkeypatch):
    monkeypatch.setattr(
        "aistudio_api.api.dependencies.is_admin_password_session",
        lambda request: False,
    )
    resp = to_account_response(cast(Any, _AccountStub()), request=_api_key_request())
    assert resp.proxy_url == "http://***@proxy.example.com:8080"
    assert "secretPass" not in (resp.proxy_url or "")


@pytest.mark.asyncio
async def test_account_response_keeps_proxy_for_admin_session(monkeypatch):
    monkeypatch.setattr(
        "aistudio_api.api.dependencies.is_admin_password_session",
        lambda request: True,
    )
    resp = to_account_response(cast(Any, _AccountStub()), request=_api_key_request())
    assert resp.proxy_url == "http://user:secretPass@proxy.example.com:8080"


def test_account_response_without_request_keeps_raw_proxy():
    resp = to_account_response(cast(Any, _AccountStub()))
    assert resp.proxy_url == "http://user:secretPass@proxy.example.com:8080"


# ─── keepalive interval / allow_private 的 env > config.yaml 链 ──────────


def _write_network_config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_keepalive_interval_falls_back_to_yaml(tmp_path, monkeypatch):
    cfg = _write_network_config(tmp_path, "network:\n  keepalive_interval: 45\n")
    monkeypatch.delenv("AISTUDIO_KEEPALIVE_INTERVAL", raising=False)
    monkeypatch.setattr("aistudio_api.config.resolve_config_file", lambda: cfg)
    assert discover_keepalive_interval() == 45


def test_keepalive_interval_env_overrides_yaml(tmp_path, monkeypatch):
    cfg = _write_network_config(tmp_path, "network:\n  keepalive_interval: 45\n")
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_INTERVAL", "90")
    monkeypatch.setattr("aistudio_api.config.resolve_config_file", lambda: cfg)
    assert discover_keepalive_interval() == 90


def test_keepalive_allow_private_falls_back_to_yaml(tmp_path, monkeypatch):
    cfg = _write_network_config(tmp_path, "network:\n  keepalive_allow_private: true\n")
    monkeypatch.delenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr("aistudio_api.config.resolve_config_file", lambda: cfg)
    assert discover_keepalive_allow_private() is True


def test_keepalive_allow_private_defaults_false(tmp_path, monkeypatch):
    cfg = _write_network_config(tmp_path, "network:\n  keepalive_url: ''\n")
    monkeypatch.delenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", raising=False)
    monkeypatch.setattr("aistudio_api.config.resolve_config_file", lambda: cfg)
    assert discover_keepalive_allow_private() is False


# ─── Cookie 回写新鲜度仲裁（overwrite_keys） ──────────────────────────────


def _make_store_with_account(tmp_path: Path) -> tuple[AccountStore, Path]:
    AccountStore(accounts_dir=tmp_path / "accounts")
    auth_dir = tmp_path / "accounts" / "acc_v2"
    auth_dir.mkdir(parents=True)
    auth_file = auth_dir / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SIDCC", "value": "sidcc-v2"},
                    {"name": "__Secure-1PSIDTS", "value": "psidts-v2"},
                    {"name": "SID", "value": "sid-stable"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (auth_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": "acc_v2",
                "name": "v2",
                "created_at": "2026-11-23T00:00:00",
                "cookie_id": "cookie_v2",
            }
        ),
        encoding="utf-8",
    )
    # 重新加载 registry（首个实例仅用于触发目录写入，不需保留引用）
    store2 = AccountStore(accounts_dir=tmp_path / "accounts")
    return store2, auth_file


def test_overwrite_keys_prevents_stale_snapshot_rollback(tmp_path):
    store, auth_file = _make_store_with_account(tmp_path)
    # 磁盘已是 v2；请求开始时的旧快照（v1）+ 本次仅 PSIDTS 更新
    stale_snapshot = {
        "SIDCC": "sidcc-v1",
        "__Secure-1PSIDTS": "psidts-v3",
        "SID": "sid-stable",
    }
    store.update_account_cookies(
        auth_file=str(auth_file),
        new_cookies=stale_snapshot,
        overwrite_keys={"__Secure-1PSIDTS"},
    )
    on_disk = json.loads(auth_file.read_text(encoding="utf-8"))
    values = {c["name"]: c["value"] for c in on_disk["cookies"]}
    # 未在 overwrite_keys 中的键不被旧快照回退
    assert values["SIDCC"] == "sidcc-v2"
    # updated 键正常推进
    assert values["__Secure-1PSIDTS"] == "psidts-v3"


def test_overwrite_keys_none_keeps_full_merge(tmp_path):
    store, auth_file = _make_store_with_account(tmp_path)
    fresh_full = {
        "SIDCC": "sidcc-v3",
        "__Secure-1PSIDTS": "psidts-v3",
        "SID": "sid-stable",
    }
    store.update_account_cookies(auth_file=str(auth_file), new_cookies=fresh_full)
    on_disk = json.loads(auth_file.read_text(encoding="utf-8"))
    values = {c["name"]: c["value"] for c in on_disk["cookies"]}
    assert values["SIDCC"] == "sidcc-v3"
    assert values["__Secure-1PSIDTS"] == "psidts-v3"


# ─── 签名缓存：保存失败保留脏标记（退出兜底可重试） ──────────────────────


def test_signature_cache_keeps_dirty_when_save_fails(tmp_path, monkeypatch):
    cache = ThoughtSignatureCache(path=tmp_path / "cache.json")
    sig = "CsEBCsoB" + "A" * 200  # 通过形状校验的伪签名

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(
        "aistudio_api.infrastructure.utils.common.atomic_write_json", _boom
    )
    cache.remember("call-1", "tool_x", {"a": 1}, sig)
    assert cache._dirty is True  # 保存失败不得清除脏标记
    # 退出兜底 flush 仍会重试（此处仍失败，但 dirty 继续保留）
    cache.flush()
    assert cache._dirty is True


def test_signature_cache_clears_dirty_on_success(tmp_path):
    cache = ThoughtSignatureCache(path=tmp_path / "cache.json")
    sig = "CsEBCsoB" + "B" * 200
    cache.remember("call-2", "tool_y", {"b": 2}, sig)
    assert cache._dirty is False  # 首次 remember 立即落盘成功
    assert json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))


# ─── 日志历史 ts_ms 畸形值兜底 ────────────────────────────────────────────


def test_read_log_history_sorts_malformed_ts_ms(tmp_path, monkeypatch):
    lines = [
        json.dumps({"ts_ms": 3000, "message": "c"}),
        json.dumps({"ts_ms": 12.5, "message": "bad-float"}),
        json.dumps({"ts_ms": "not-a-number", "message": "bad-str"}),
        json.dumps({"message": "missing"}),
        json.dumps({"ts_ms": 1000, "message": "a"}),
    ]
    (tmp_path / "runtime.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "aistudio_api.infrastructure.utils.log_files.log_directory",
        lambda: tmp_path,
    )
    result = read_log_history(page=1, page_size=10, search="", level="all")
    items = cast(list[dict[str, Any]], result["items"])
    messages = [item.get("message") for item in items]
    assert messages[0] == "c"
    assert set(messages[1:]) == {"bad-float", "bad-str", "missing", "a"}


# ─── 脱敏：Authorization 头 / 裸 userinfo ─────────────────────────────────


def test_redact_text_masks_authorization_header_any_scheme():
    assert (
        redact_text("Authorization: SAPISIDHASH abc123def/456") == "Authorization: ***"
    )
    assert redact_text("authorization: Basic dXNlcjpwYXNz") == "authorization: ***"


def test_redact_text_masks_bare_userinfo():
    assert redact_text("user:pass@proxy.example.com:8080") == (
        "***@proxy.example.com:8080"
    )


def test_redact_text_keeps_normal_email_and_time():
    assert redact_text("mail: a@b.com") == "mail: a@b.com"
    assert redact_text("meeting at 12:30 room 4") == "meeting at 12:30 room 4"


# ─── OpenAI stop 停止词响应侧截断 ────────────────────────────────────────


def _sse_completion_content(frames: list[str]) -> str:
    text = []
    for frame in frames:
        if not frame.startswith("data: "):
            continue
        payload = frame[len("data: ") :].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                text.append(delta["content"])
    return "".join(text)


@pytest.mark.asyncio
async def test_openai_stream_adapter_truncates_on_stop_sequence():
    from aistudio_api.application.compat_openai import openai_stream_adapter

    def frame(text: str | None, finish: str | None = None) -> str:
        candidate: dict = {}
        if finish:
            candidate["finishReason"] = finish
        if text is not None:
            candidate["content"] = {"parts": [{"text": text}]}
        payload = {"candidates": [candidate]}
        return "data: " + json.dumps(payload) + "\n\n"

    async def source():
        # 上游不执行 stopSequences：停止词及其后文本全部泄漏
        yield frame("hello ")
        yield frame("END")
        yield frame(" world and more secrets")
        yield frame(None, finish="STOP")

    chunks: list[str] = []
    async for chunk in openai_stream_adapter(
        source(),
        requested_model="gemini-3.7-flash",
        include_usage=False,
        stop_sequences=["END"],
    ):
        chunks.append(chunk)

    content = _sse_completion_content(chunks)
    assert content == "hello "
    assert "secrets" not in content
    # 终结块 finish_reason 为 stop
    assert any('"finish_reason":"stop"' in c for c in chunks)


def test_openai_completion_truncates_on_stop_sequence():
    from aistudio_api.api.response_models import (
        GeminiGenerateContentResponse,
    )
    from aistudio_api.application.compat_openai import build_openai_completion

    resp = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {"text": "hello "},
                            {"text": "END world"},
                            {"text": " trailing"},
                        ]
                    },
                }
            ]
        }
    )
    completion = build_openai_completion(
        resp, requested_model="gemini-3.7-flash", stop_sequences=["END"]
    )
    choices = cast(list[dict[str, Any]], completion["choices"])
    message = cast(dict[str, Any], choices[0]["message"])
    assert message["content"] == "hello "
    assert choices[0]["finish_reason"] == "stop"


def test_openai_completion_without_stop_keeps_full_text():
    from aistudio_api.api.response_models import (
        GeminiGenerateContentResponse,
    )
    from aistudio_api.application.compat_openai import build_openai_completion

    resp = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "hello END world"}]},
                }
            ]
        }
    )
    completion = build_openai_completion(resp, requested_model="m")
    choices = cast(list[dict[str, Any]], completion["choices"])
    message = cast(dict[str, Any], choices[0]["message"])
    assert message["content"] == "hello END world"


# ─── resolve_compat_model provider 前缀剥离 ──────────────────────────────


@pytest.mark.parametrize(
    ("requested", "channel", "expected"),
    [
        # conftest 模板映射：gpt-4o-mini → gemini-3.7-flash（前缀剥离后命中）
        ("openai/gpt-4o-mini", "openai", "gemini-3.7-flash"),
        # claude 家族回退（前缀剥离后家族正则可命中）
        ("anthropic/claude-sonnet-4-5", "claude", "gemini-3.8-flash"),
        # google/ 前缀剥离后原样透传（已是 Gemini 模型名）
        ("google/gemini-3.7-flash", "openai", "gemini-3.7-flash"),
        # 白名单前缀剥离后无映射/家族命中 → 剥离名原样透传
        ("deepseek/deepseek-chat", "openai", "deepseek-chat"),
    ],
)
def test_resolve_compat_model_strips_provider_prefix(requested, channel, expected):
    from aistudio_api.application.compat_shared import resolve_compat_model

    assert resolve_compat_model(requested, channel=channel) == expected


def test_resolve_compat_model_keeps_unknown_slash_name():
    from aistudio_api.application.compat_shared import resolve_compat_model

    assert (
        resolve_compat_model("my-custom/model-x", channel="openai")
        == "my-custom/model-x"
    )


# ─── tool_choice allowed_tools ───────────────────────────────────────────


def test_openai_tool_choice_allowed_tools_maps_to_allowlist():
    from aistudio_api.application.compat_openai import (
        _openai_tool_choice_to_config,
    )

    config = _openai_tool_choice_to_config(
        {
            "type": "allowed_tools",
            "tools": [
                {"type": "function", "function": {"name": "get_weather"}},
                {"type": "function", "function": {"name": "get_time"}},
            ],
        }
    )
    assert config == {
        "functionCallingConfig": {
            "mode": "AUTO",
            "allowedFunctionNames": ["get_weather", "get_time"],
        }
    }


# ─── countTokens 系统指令计入统计 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_tokens_includes_system_instruction(monkeypatch):
    from aistudio_api.api.schemas import GeminiCountTokensRequest
    from aistudio_api.application import api_service_gemini as svc

    captured: dict[str, Any] = {}

    class _Client:
        async def count_tokens(self, *, model, contents):
            captured["model"] = model
            captured["contents"] = contents
            return 42

    async def _noop_ensure(attempt, model=None):
        return None

    monkeypatch.setattr(svc, "ensure_active_account", _noop_ensure)

    req = GeminiCountTokensRequest.model_validate(
        {
            "generateContentRequest": {
                "systemInstruction": {
                    "role": "user",
                    "parts": [{"text": "be brief"}],
                },
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            }
        }
    )
    result = await svc.handle_gemini_count_tokens(
        "gemini-3.7-flash", req, cast(Any, _Client())
    )

    assert result["totalTokens"] == 42
    # 系统指令作为额外 content 前插（计数与 generateContent 实际消耗一致）
    texts: list[str] = []
    for content in cast(list[Any], captured["contents"]):
        for part in content.parts:
            if getattr(part, "text", None):
                texts.append(part.text)
    assert "be brief" in texts
    assert "hi" in texts
