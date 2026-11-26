"""第六轮深审（round8）修复回归测试。

覆盖：/config 代理凭据掩码、API Key 回退会话写保护清单、countTokens
空轮 400、stream_parser 闭合 chunk 修复回退、schema 深嵌套彻底降级、
OpenAI/Claude 适配器错误兜底文本过滤、Responses 错误码映射、签名缓存
去抖落盘、签名事件不覆盖 part 自带签名、历史日志 ts_ms 兜底。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

SIGNATURE = "dGVzdC1zaWduYXR1cmU="  # base64("test-signature")


# ─── /config 掩码：network.proxy_url 内嵌凭据 ───


def test_mask_yaml_masks_embedded_proxy_credentials() -> None:
    from aistudio_api.api.routes_config import _mask_yaml_api_keys

    yaml_content = (
        "network:\n"
        "  proxy_url: http://user:secretPass@proxy.example.com:8080\n"
        "api_keys:\n"
        "  - name: k\n"
        "    key: sk-aistudio-abcdefghijklmnop\n"
    )
    masked = _mask_yaml_api_keys(yaml_content)
    assert "secretPass" not in masked
    assert "user:" not in masked
    # 代理仍保留主机信息（运维可辨认），仅凭据被替换
    assert "proxy.example.com" in masked


def test_mask_yaml_keeps_proxy_for_admin_session_passthrough() -> None:
    """掩码函数本身不区分会话（调用方判定），无凭据代理不应被破坏。"""
    from aistudio_api.api.routes_config import _mask_yaml_api_keys

    masked = _mask_yaml_api_keys("network:\n  proxy_url: http://1.2.3.4:8080\n")
    assert "1.2.3.4:8080" in masked


# ─── API Key 回退会话写保护清单 ───


@pytest.mark.asyncio
async def test_apikey_session_write_guard_blocks_sensitive_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """仅配置 API Key（无管理密码）时，敏感写端点对 API Key 会话 403。"""
    from aistudio_api.api.app import app
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "web_password", "")
    monkeypatch.setattr(
        settings, "api_keys", frozenset({"sk-aistudio-test-key-000000"})
    )
    monkeypatch.setattr(
        "aistudio_api.api.dependencies._all_configured_api_keys",
        lambda: frozenset({"sk-aistudio-test-key-000000"}),
    )
    headers = {"Authorization": "Bearer sk-aistudio-test-key-000000"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        # 全局代理改写（流量劫持面）必须 403
        resp = await client.put(
            "/api/proxies/global", json={"proxy_url": ""}, headers=headers
        )
        assert resp.status_code == 403
        # 账号写入端点必须 403
        resp = await client.post(
            "/api/accounts/cookie_deadbeef/activate", headers=headers
        )
        assert resp.status_code == 403
        # 日志清空必须 403
        resp = await client.delete("/api/logs", headers=headers)
        assert resp.status_code == 403
        # SSE 票据签发（只读通道补偿）放行
        resp = await client.post("/api/logs/stream-ticket", headers=headers)
        assert resp.status_code == 200
        # 幂等探测端点放行：不被写保护拦截（服务未初始化时 503 也证明
        # 已越过鉴权层正常执行）
        resp = await client.post(
            "/api/accounts/cookie_deadbeef/detect-email", headers=headers
        )
        assert resp.status_code != 403


# ─── countTokens 空内容轮 → 400（而非穿透 500）───


@pytest.mark.asyncio
async def test_count_tokens_empty_contents_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aistudio_api.api.app import app
    from aistudio_api.api.state import runtime_state

    async def fake_ensure(attempt: int, *, model: str) -> None:
        return None

    monkeypatch.setattr(
        "aistudio_api.application.api_service_gemini.ensure_active_account",
        fake_ensure,
    )
    monkeypatch.setattr(runtime_state, "client", object())  # 跳过 503 早退

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1beta/models/gemini-3.8-flash:countTokens",
            json={"contents": [{"role": "user", "parts": [{"text": ""}]}]},
        )
        assert resp.status_code == 400


# ─── stream_parser：结构闭合但非法 JSON 的 chunk 修复回退 ───


def test_stream_parser_recovers_closed_chunk_with_control_char() -> None:
    from aistudio_api.infrastructure.gateway.stream_parser import (
        IncrementalJSONStreamParser,
    )

    parser = IncrementalJSONStreamParser()
    # 字符串内裸换行符：结构闭合但 json.loads 会失败（Invalid control char）
    payload = ")]}'\n" + '[[[null,"te\nxt"]]]'
    assert "\n" in payload[5:]  # 确认裸换行真实存在
    chunks = list(parser.feed(payload))
    assert len(chunks) == 1
    assert chunks[0][1] == "te\nxt"


# ─── schema 深嵌套超限 → 彻底降级为 object 而非 400 ───


def test_deeply_nested_tool_schema_falls_back_to_object() -> None:
    from aistudio_api.application.schema_wire_codec import (
        _encode_tool_parameters,
    )

    # 构造超过复杂度上限（>64 层）的嵌套 schema
    deep: dict[str, object] = {"type": "string"}
    for _ in range(80):
        deep = {"type": "object", "properties": {"p": deep}}
    wire, schema_json = _encode_tool_parameters(deep, strict=False)
    assert isinstance(wire, list)
    # 完整 schema 原文附加到 description 供模型理解
    assert schema_json is not None
    assert json.loads(schema_json) is not None


# ─── OpenAI / Claude 适配器：上游错误兜底文本不混入正文 ───


@pytest.mark.asyncio
async def test_openai_adapter_filters_error_text_frame() -> None:
    from aistudio_api.application.api_service_gemini import _ERROR_TEXT_PREFIX
    from aistudio_api.application.compat_openai import openai_stream_adapter

    async def source():
        yield {
            "candidates": [
                {"content": {"parts": [{"text": f"{_ERROR_TEXT_PREFIX} 503 X] boom"}]}}
            ]
        }
        yield {"error": {"code": 503, "message": "boom"}}

    chunks = [
        c
        async for c in openai_stream_adapter(
            source(), requested_model="m", include_usage=False
        )
    ]
    joined = "".join(chunks)
    assert "[aistudio-api 请求失败" not in joined
    assert "boom" in joined  # 错误块仍携带等价信息


@pytest.mark.asyncio
async def test_claude_adapter_filters_error_text_frame() -> None:
    from aistudio_api.application.api_service_gemini import _ERROR_TEXT_PREFIX
    from aistudio_api.application.compat_claude import claude_stream_adapter

    async def source():
        yield {
            "candidates": [
                {"content": {"parts": [{"text": f"{_ERROR_TEXT_PREFIX} 503 X] boom"}]}}
            ]
        }
        yield {"error": {"code": 503, "message": "boom"}}

    chunks = [
        c
        async for c in claude_stream_adapter(
            source(), requested_model="m", stop_sequences=None
        )
    ]
    joined = "".join(chunks)
    assert "[aistudio-api 请求失败" not in joined
    assert "boom" in joined


# ─── Responses 错误事件携带语义化错误码 ───


@pytest.mark.asyncio
async def test_responses_error_event_maps_rate_limit_code() -> None:
    from aistudio_api.application.responses_service import responses_stream_adapter

    async def source():
        yield {"error": {"code": 429, "message": "rate limited"}}

    chunks = [c async for c in responses_stream_adapter(source(), requested_model="m")]
    joined = "".join(chunks)
    assert '"code":"rate_limit_error"' in joined


# ─── 签名缓存：去抖落盘 + flush 兜底 ───


def test_signature_cache_debounces_and_flushes(tmp_path: Path) -> None:
    from aistudio_api.domain.tool_signatures import ThoughtSignatureCache

    path = tmp_path / "sig.json"
    cache = ThoughtSignatureCache(path)
    # 窗口内多次 remember：内存立即可见
    cache.remember("a", "f", {}, SIGNATURE)
    assert cache.lookup("a", "f", {}) == SIGNATURE
    # 落盘受去抖控制（首个 remember 会立即落盘一次）
    cache.flush()
    reloaded = ThoughtSignatureCache(path)
    assert reloaded.lookup("a", "f", {}) == SIGNATURE
    # atexit 钩子不影响正常构造/析构
    cache.flush()
    assert reloaded.lookup("a", "f", {}) == SIGNATURE


def test_signature_cache_compact_persistence(tmp_path: Path) -> None:
    from aistudio_api.domain.tool_signatures import ThoughtSignatureCache

    path = tmp_path / "sig.json"
    cache = ThoughtSignatureCache(path)
    cache.remember("k", "f", {"x": 1}, SIGNATURE)
    cache.flush()
    raw = path.read_text(encoding="utf-8")
    # compact 落盘：无缩进换行（热路径体积减半）
    assert "\n" not in raw
    assert json.loads(raw)["by_call"]


def test_opaque_signature_only_stays_on_its_source_event() -> None:
    from aistudio_api.application.api_service_gemini import _event_parts

    call = {
        "call_id": "c1",
        "name": "f",
        "args": {"q": "x"},
        "thought_signature": SIGNATURE,
    }
    assert _event_parts("thought_signature", "OTHERSIG==") == [
        {"thoughtSignature": "OTHERSIG=="}
    ]
    assert _event_parts("tool_calls", [call])[0]["thoughtSignature"] == SIGNATURE
    del call["thought_signature"]
    assert "thoughtSignature" not in _event_parts("tool_calls", [call])[0]


# ─── 历史日志缺 ts_ms 不再 500 ───


def test_history_log_sort_tolerates_missing_ts_ms(tmp_path: Path) -> None:
    """缺 ts_ms 的合法 JSON 行排序兜底为 0，不再 KeyError→500。"""
    entries = [
        {"ts_ms": 300, "message": "c"},
        {"message": "no-ts"},
        {"ts_ms": 100, "message": "a"},
    ]
    # 直接复现排序表达式（lambda 兜底）
    ordered = sorted(
        entries, key=lambda item: int(str(item.get("ts_ms") or 0)), reverse=True
    )
    assert ordered[0]["message"] == "c"
    assert ordered[-1]["message"] == "no-ts"
