"""抗截断模型变体 (Model Variants) 单元测试。

覆盖：后缀解析（含 models/ 前缀与未选中模型不误判）、配置规范化与
持久化、模型列表变体暴露、变体详情端点、generateContent /
streamGenerateContent 两条请求链路（流式增量与缓冲单条 SSE）、
请求头 X-Anti-Truncation 对变体的覆盖优先级、面板配置 API。
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.requests import Request

from aistudio_api.api.app import app
from aistudio_api.api.routes_gemini import _parse_anti_truncation_header
from aistudio_api.application.anti_truncation import (
    BUFFER_VARIANT_SUFFIX,
    STREAM_VARIANT_SUFFIX,
    get_selected_variant_models,
    resolve_model_variant,
)
from aistudio_api.config import settings
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_anti_truncation_settings,
    update_anti_truncation_settings,
)

_VARIANT_CONFIG = """
anti_truncation:
  enabled: false
  tool_name: router_emit
  model_variants:
    enabled: true
    models:
      - gemini-3.7-flash
      - models/gemini-3-pro-preview
"""

_DISABLED_CONFIG = """
anti_truncation:
  enabled: false
  tool_name: router_emit
  model_variants:
    enabled: false
    models:
      - gemini-3.7-flash
"""


def _write_config(tmp_path, content: str):
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    return config


@pytest.fixture
def variant_config(tmp_path, monkeypatch: pytest.MonkeyPatch):
    config = _write_config(tmp_path, _VARIANT_CONFIG)
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.delenv("AISTUDIO_ANTI_TRUNCATION", raising=False)
    return config


@pytest.fixture
def disabled_config(tmp_path, monkeypatch: pytest.MonkeyPatch):
    config = _write_config(tmp_path, _DISABLED_CONFIG)
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.delenv("AISTUDIO_ANTI_TRUNCATION", raising=False)
    return config


# ---------------------------------------------------------------------------
# 后缀解析
# ---------------------------------------------------------------------------


def test_resolve_stream_variant(variant_config):
    result = resolve_model_variant(f"gemini-3.7-flash{STREAM_VARIANT_SUFFIX}")
    assert result is not None
    assert result.model_path == "gemini-3.7-flash"
    assert result.anti_truncation is True
    assert result.buffered is False


def test_resolve_buffer_variant(variant_config):
    result = resolve_model_variant("models/gemini-3-pro-preview-at")
    assert result is not None
    assert result.model_path == "models/gemini-3-pro-preview"
    assert result.anti_truncation is True
    assert result.buffered is True


def test_resolve_long_suffix_wins_over_short(variant_config):
    """`-at-stream` 必须先于 `-at` 解析，不会被误判为缓冲变体。"""
    result = resolve_model_variant("models/gemini-3.7-flash-at-stream")
    assert result is not None
    assert result.buffered is False
    assert result.model_path == "models/gemini-3.7-flash"


def test_resolve_unselected_base_returns_none(variant_config):
    """后缀命中但基础模型不在选中列表：按原模型处理，不误吞。"""
    assert resolve_model_variant("other-model-at") is None
    assert resolve_model_variant("other-model-at-stream") is None


def test_resolve_plain_model_returns_none(variant_config):
    assert resolve_model_variant("gemini-3.7-flash") is None
    assert resolve_model_variant("models/gemini-3.7-flash") is None


def test_resolve_disabled_returns_none(disabled_config):
    assert resolve_model_variant("gemini-3.7-flash-at") is None
    assert resolve_model_variant("gemini-3.7-flash-at-stream") is None


def test_selected_models_normalized(variant_config):
    selected = get_selected_variant_models()
    assert selected == {"gemini-3.7-flash", "gemini-3-pro-preview"}


def test_settings_shape_normalized(variant_config):
    at_settings = get_anti_truncation_settings()
    variants = at_settings["model_variants"]
    assert isinstance(variants, dict)
    assert variants["enabled"] is True
    assert variants["models"] == ["gemini-3.7-flash", "models/gemini-3-pro-preview"]


def test_settings_defaults_when_missing(tmp_path, monkeypatch):
    config = _write_config(tmp_path, "api_keys: []\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.delenv("AISTUDIO_ANTI_TRUNCATION", raising=False)
    variants = get_anti_truncation_settings()["model_variants"]
    assert variants == {"enabled": False, "models": []}


# ---------------------------------------------------------------------------
# 持久化 round-trip
# ---------------------------------------------------------------------------


def test_update_settings_round_trip(tmp_path, monkeypatch):
    config = _write_config(tmp_path, "api_keys: []\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.delenv("AISTUDIO_ANTI_TRUNCATION", raising=False)

    result = update_anti_truncation_settings(
        enabled=True,
        variants_enabled=True,
        models=["gemini-3.7-flash", "", "gemini-3-pro-preview"],
    )
    assert result["enabled"] is True
    result_variants = result["model_variants"]
    assert isinstance(result_variants, dict)
    assert result_variants["enabled"] is True
    assert result_variants["models"] == [
        "gemini-3.7-flash",
        "gemini-3-pro-preview",
    ]

    # 重新读取仍生效（已落盘）。
    reread = get_anti_truncation_settings()
    assert reread["enabled"] is True
    reread_variants = reread["model_variants"]
    assert isinstance(reread_variants, dict)
    assert reread_variants["models"] == [
        "gemini-3.7-flash",
        "gemini-3-pro-preview",
    ]

    # 只更新部分字段，其余保持。
    update_anti_truncation_settings(enabled=False)
    reread = get_anti_truncation_settings()
    assert reread["enabled"] is False
    reread_variants = reread["model_variants"]
    assert isinstance(reread_variants, dict)
    assert reread_variants["enabled"] is True


# ---------------------------------------------------------------------------
# 模型列表变体暴露
# ---------------------------------------------------------------------------


_FAKE_DISCOVERED = [
    {
        "id": "gemini-3.7-flash",
        "displayName": "Gemini 3.7 Flash",
        "description": "Flash text model",
        "supportedGenerationMethods": ["generateContent"],
    },
    {
        "id": "gemini-3.1-pro-preview",
        "displayName": "Gemini 3.1 Pro Preview",
        "supportedGenerationMethods": ["generateContent"],
    },
    {
        "id": "gemini-3.1-flash-image",
        "displayName": "Nano Banana 2",
        "is_image_model": True,
        "supportedGenerationMethods": ["generateContent"],
    },
]


def _patch_discovery(monkeypatch: pytest.MonkeyPatch, models):
    from aistudio_api.infrastructure.gateway import model_discovery as md

    async def fake_get_models(self, **kwargs):
        return list(models)

    monkeypatch.setattr(md.ModelDiscoveryService, "get_models", fake_get_models)


@pytest.mark.asyncio
async def test_list_models_exposes_variants(variant_config, monkeypatch):
    _patch_discovery(monkeypatch, _FAKE_DISCOVERED)
    monkeypatch.setattr(settings, "api_keys", frozenset())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1beta/models")

    assert resp.status_code == 200
    names = [m["name"] for m in resp.json()["models"]]
    assert "models/gemini-3.7-flash" in names
    assert "models/gemini-3.7-flash-at-stream" in names
    assert "models/gemini-3.7-flash-at" in names
    # 未选中的模型不出现变体。
    assert "models/gemini-3.1-pro-preview-at" not in names

    display = {
        m["name"]: m["displayName"] for m in resp.json()["models"]
    }
    assert "流式抗截断" in display["models/gemini-3.7-flash-at-stream"]
    assert "非流式抗截断" in display["models/gemini-3.7-flash-at"]


@pytest.mark.asyncio
async def test_list_models_no_variants_when_disabled(disabled_config, monkeypatch):
    _patch_discovery(monkeypatch, _FAKE_DISCOVERED)
    monkeypatch.setattr(settings, "api_keys", frozenset())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1beta/models")

    names = [m["name"] for m in resp.json()["models"]]
    assert "models/gemini-3.7-flash-at" not in names
    assert "models/gemini-3.7-flash-at-stream" not in names


@pytest.mark.asyncio
async def test_get_variant_model_detail(variant_config, monkeypatch):
    _patch_discovery(monkeypatch, _FAKE_DISCOVERED)
    monkeypatch.setattr(settings, "api_keys", frozenset())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/v1beta/models/gemini-3.7-flash{BUFFER_VARIANT_SUFFIX}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "models/gemini-3.7-flash-at"
    assert "非流式抗截断" in data["displayName"]
    assert "generateContent" in data["supportedGenerationMethods"]


# ---------------------------------------------------------------------------
# 请求链路：变体 → 强制抗截断
# ---------------------------------------------------------------------------


def _stub_output(text: str = "", content: str | None = None) -> ModelOutput:
    # 无函数调用时用空列表（域模型的 function_calls 不接受 None）
    function_calls: list[dict[str, object]] = (
        [{"name": "router_emit", "args": {"content": content}}]
        if content is not None
        else []
    )
    return ModelOutput(
        candidates=[
            Candidate(text=text, function_calls=function_calls, finish_reason=1)
        ],
        model="models/gemini-3.7-flash",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        response_id="resp_variant",
    )


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub_client):
    from aistudio_api.api import state as api_state
    from aistudio_api.application import api_service_gemini as svc

    async def fake_ensure_active_account(attempt, model=None):
        return None

    monkeypatch.setattr(svc, "ensure_active_account", fake_ensure_active_account)
    monkeypatch.setattr(svc, "record_rotator_event", lambda *a, **k: None)
    monkeypatch.setattr(api_state.runtime_state, "client", stub_client)


def _tools_contain_router_emit(tools) -> bool:
    if not tools:
        return False
    for tool in tools:
        if isinstance(tool, list) and len(tool) > 1 and isinstance(tool[1], list):
            for decl in tool[1]:
                if isinstance(decl, list) and decl and decl[0] == "router_emit":
                    return True
    return False


@pytest.mark.asyncio
async def test_nonstream_variant_forces_anti_truncation(
    variant_config, monkeypatch
):
    captured: dict[str, object] = {}

    class StubClient:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return _stub_output(content="变体还原正文")

    _install_stub(monkeypatch, StubClient())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            f"/v1beta/models/gemini-3.7-flash{BUFFER_VARIANT_SUFFIX}:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    # 基础模型被正确还原，抗截断注入 + 还原都发生。
    assert captured["model"] == "models/gemini-3.7-flash"
    assert _tools_contain_router_emit(captured["tools"])
    assert data["candidates"][0]["content"]["parts"][0]["text"] == "变体还原正文"


@pytest.mark.asyncio
async def test_stream_buffer_variant_yields_single_event(
    variant_config, monkeypatch
):
    captured: dict[str, object] = {}

    class StubClient:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return _stub_output(content="缓冲整体正文")

        async def stream_generate_content(self, **kwargs):
            raise AssertionError("缓冲变体不应调用流式上游")
            yield  # pragma: no cover

    _install_stub(monkeypatch, StubClient())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            f"/v1beta/models/gemini-3.7-flash{BUFFER_VARIANT_SUFFIX}:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # 收集所有 data: 事件。
    events = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.split("\n")
        if line.startswith("data: ")
    ]
    # 单条完整事件包含全部正文 + usage + finishReason。
    full = [e for e in events if e.get("candidates")]
    texts = [
        p.get("text", "")
        for e in full
        for p in e["candidates"][0].get("content", {}).get("parts", [])
    ]
    assert "缓冲整体正文" in "".join(texts)
    assert any("usageMetadata" in e for e in events)
    # 上游走的是非流式调用且带抗截断注入。
    assert captured["model"] == "models/gemini-3.7-flash"
    assert _tools_contain_router_emit(captured["tools"])


@pytest.mark.asyncio
async def test_stream_variant_uses_streaming_transport(variant_config, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_stream_generate_content(**kwargs):
        captured.update(kwargs)
        yield ("body", "流式")
        yield ("body", "增量")
        yield ("finish_reason", 1)

    class StubClient:
        stream_generate_content = staticmethod(fake_stream_generate_content)

        async def generate_content(self, **kwargs):
            raise AssertionError("流式变体不应调用缓冲上游")

    _install_stub(monkeypatch, StubClient())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            f"/v1beta/models/gemini-3.7-flash{STREAM_VARIANT_SUFFIX}:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert resp.status_code == 200
    assert "流式" in resp.text and "增量" in resp.text
    assert captured["model"] == "models/gemini-3.7-flash"
    assert _tools_contain_router_emit(captured["tools"])


@pytest.mark.asyncio
async def test_header_off_overrides_variant(variant_config, monkeypatch):
    captured: dict[str, object] = {}

    class StubClient:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return _stub_output(text="普通正文")

    _install_stub(monkeypatch, StubClient())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            f"/v1beta/models/gemini-3.7-flash{BUFFER_VARIANT_SUFFIX}:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            headers={"X-Anti-Truncation": "0"},
        )

    assert resp.status_code == 200
    # 请求头显式关闭：不注入合成工具，模型名仍被还原为基础模型。
    assert captured["model"] == "models/gemini-3.7-flash"
    assert not _tools_contain_router_emit(captured.get("tools"))
    assert captured.get("tools") is None
    assert resp.json()["candidates"][0]["content"]["parts"][0]["text"] == "普通正文"


@pytest.mark.asyncio
async def test_unselected_variant_model_passthrough(variant_config, monkeypatch):
    """不在选中列表的 -at 结尾模型按原模型处理（无注入）。"""
    captured: dict[str, object] = {}

    class StubClient:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return _stub_output(text="原样正文")

    _install_stub(monkeypatch, StubClient())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            "/v1beta/models/other-model-at:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert resp.status_code == 200
    assert captured["model"] == "models/other-model-at"
    assert captured.get("tools") is None


# ---------------------------------------------------------------------------
# 面板配置 API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_anti_truncation_api_round_trip(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    from aistudio_api.infrastructure.gateway import model_discovery as md

    config = _write_config(tmp_path, "api_keys: []\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.delenv("AISTUDIO_ANTI_TRUNCATION", raising=False)
    monkeypatch.setattr(settings, "web_password", "")

    discovery = md.model_discovery
    monkeypatch.setattr(
        discovery,
        "get_models",
        AsyncMock(return_value=list(_FAKE_DISCOVERED)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 初始：全部关闭。
        resp = await client.get("/config/anti-truncation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["variants"]["enabled"] is False
        # 生图模型不进可选列表。
        available_ids = [m["id"] for m in data["available_models"]]
        assert "gemini-3.7-flash" in available_ids
        assert "gemini-3.1-pro-preview" in available_ids
        assert "gemini-3.1-flash-image" not in available_ids

        # 开启变体并选择模型。
        resp = await client.put(
            "/config/anti-truncation",
            json={
                "enabled": False,
                "variants_enabled": True,
                "models": ["gemini-3.7-flash"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["variants"]["enabled"] is True
        assert data["variants"]["models"] == ["gemini-3.7-flash"]

    # 持久化生效：解析器现在能识别变体。
    result = resolve_model_variant("gemini-3.7-flash-at")
    assert result is not None
    assert result.buffered is True


# ---------------------------------------------------------------------------
# 请求头解析（回归）
# ---------------------------------------------------------------------------


def test_parse_header_regression():
    req = Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-anti-truncation", b"1")],
        }
    )
    assert _parse_anti_truncation_header(req) is True
