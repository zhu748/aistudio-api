"""抗截断传输 (Anti-Truncation Transport) 单元测试。

覆盖：配置解析、请求侧注入与安全跳过条件、非流式还原、流式增量
ContentDecoder、流式频道仲裁（含全量重发去重）、真实工具透传、
finish_reason 归一与请求头覆盖解析。
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from aistudio_api.api.routes_gemini import _parse_anti_truncation_header
from aistudio_api.application.anti_truncation import (
    ContentDecoder,
    prepare_anti_truncation,
    restore_anti_truncation_output,
    restore_anti_truncation_stream,
)
from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    encode_function_declaration_to_wire,
)
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_anti_truncation_settings,
)
from aistudio_api.infrastructure.gateway.wire_codec import build_tools_from_names
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioPart,
)


@pytest.fixture
def at_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """通过环境变量强制开启抗截断传输（覆盖隔离配置）。"""
    monkeypatch.setenv("AISTUDIO_ANTI_TRUNCATION", "1")


def _make_normalized(**overrides) -> NormalizedGeminiRequest:
    base: dict = {
        "model": "models/gemini-2.5-flash",
        "contents": [
            AistudioContent(role="user", parts=[AistudioPart(text="写一首长诗")])
        ],
        "system_instruction": None,
        "tools": None,
        "safety_settings": None,
    }
    base.update(overrides)
    return NormalizedGeminiRequest(**base)


def _decl(name: str) -> list:
    return encode_function_declaration_to_wire(
        {"name": name, "parameters": {"type": "object", "properties": {}}}
    )


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def test_settings_default_disabled():
    settings = get_anti_truncation_settings()
    assert settings["enabled"] is False
    assert settings["tool_name"] == "router_emit"


def test_settings_env_forces_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AISTUDIO_ANTI_TRUNCATION", "true")
    assert get_anti_truncation_settings()["enabled"] is True


def test_settings_env_forces_disabled(tmp_path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "anti_truncation:\n  enabled: true\n  tool_name: my_tool\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.setenv("AISTUDIO_ANTI_TRUNCATION", "0")
    settings = get_anti_truncation_settings()
    assert settings["enabled"] is False
    assert settings["tool_name"] == "my_tool"


def test_settings_reads_config_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "anti_truncation:\n  enabled: true\n  tool_name: my_tool\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.delenv("AISTUDIO_ANTI_TRUNCATION", raising=False)
    settings = get_anti_truncation_settings()
    assert settings["enabled"] is True
    assert settings["tool_name"] == "my_tool"


# ---------------------------------------------------------------------------
# 请求侧注入与安全跳过
# ---------------------------------------------------------------------------


def test_prepare_disabled_leaves_request_untouched():
    normalized = _make_normalized()
    contents_before = list(normalized.contents)
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is False
    assert ctx.skip_reason == "disabled"
    assert normalized.tools is None
    assert normalized.contents == contents_before


def test_prepare_header_override_forces_on():
    normalized = _make_normalized()
    ctx = prepare_anti_truncation(normalized, override=True)
    assert ctx.active is True
    assert ctx.tool_name == "router_emit"


def test_prepare_header_override_forces_off(at_enabled):
    normalized = _make_normalized()
    ctx = prepare_anti_truncation(normalized, override=False)
    assert ctx.active is False
    assert ctx.skip_reason == "header-off"
    assert normalized.tools is None


def test_prepare_injects_synthetic_tool_and_instruction(at_enabled):
    normalized = _make_normalized()
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is True
    assert ctx.tool_name == "router_emit"

    # 注入了恰好一个函数声明容器，且内含合成声明。
    assert normalized.tools is not None
    assert len(normalized.tools) == 1
    container = normalized.tools[0]
    assert container[0] is None
    decls = container[1]
    assert isinstance(decls, list)
    assert len(decls) == 1
    assert decls[0][0] == "router_emit"

    # 末尾追加了 user 角色传输指令。
    assert len(normalized.contents) == 2
    tail = normalized.contents[-1]
    assert tail.role == "user"
    assert "router_emit" in (tail.parts[0].text or "")


def test_prepare_skips_image_model(at_enabled):
    normalized = _make_normalized(model="models/imagen-4.0-generate-001")
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is False
    assert ctx.skip_reason == "image-model"
    assert normalized.tools is None


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"response_schema": {"type": "object"}}, "structured-output"),
        (
            {"response_mime_type": "application/json"},
            "structured-output",
        ),
        ({"stop_sequences": ["END"]}, "stop-sequences"),
    ],
)
def test_prepare_skips_structured_or_stopped(at_enabled, overrides, reason):
    normalized = _make_normalized(
        generation_config_overrides=dict(overrides)
    )
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is False
    assert ctx.skip_reason == reason


def test_prepare_allows_plain_text_mime(at_enabled):
    normalized = _make_normalized(
        generation_config_overrides={"response_mime_type": "text/plain"}
    )
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is True


def test_prepare_skips_tool_history(at_enabled):
    normalized = _make_normalized(
        contents=[
            AistudioContent(role="user", parts=[AistudioPart(text="天气")]),
            AistudioContent(
                role="model",
                parts=[
                    AistudioPart(
                        function_call=("get_weather", {"city": "Shanghai"})
                    )
                ],
            ),
        ]
    )
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is False
    assert ctx.skip_reason == "tool-history"


def test_prepare_skips_builtin_tools(at_enabled):
    builtin = build_tools_from_names(["google_search"])
    assert builtin, "google_search 应产生内置工具容器"
    normalized = _make_normalized(tools=builtin)
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is False
    assert ctx.skip_reason == "builtin-tools"


def test_prepare_coexists_with_client_function_declarations(at_enabled):
    normalized = _make_normalized(tools=[[None, [_decl("get_weather")]]])
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is True
    # 合成声明追加进同一容器，不新增 tools 元素。
    assert normalized.tools is not None
    assert len(normalized.tools) == 1
    decls = normalized.tools[0][1]
    assert isinstance(decls, list)
    names = [d[0] for d in decls]
    assert names == ["get_weather", "router_emit"]
    # 有可调用工具时指令为共存话术。
    assert "Call the other tools" in (normalized.contents[-1].parts[0].text or "")


def test_prepare_random_name_on_prefix_conflict(at_enabled):
    normalized = _make_normalized(
        tools=[[None, [_decl("router_emit_v2")]]]
    )
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is True
    assert ctx.tool_name != "router_emit"
    assert ctx.tool_name.startswith("router_")
    assert normalized.tools is not None
    decls = normalized.tools[0][1]
    assert isinstance(decls, list)
    names = [d[0] for d in decls]
    assert "router_emit" not in names
    assert ctx.tool_name in names


@pytest.mark.parametrize(
    "tool_config, reason",
    [
        ([None, [2]], "tool-choice"),
        ([None, [3]], "tool-mode-none"),
    ],
)
def test_prepare_skips_forced_or_disabled_tool_config(at_enabled, tool_config, reason):
    normalized = _make_normalized(
        tools=[[None, [_decl("get_weather")]]],
        tool_config=tool_config,
    )
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is False
    assert ctx.skip_reason == reason


def test_prepare_allows_auto_tool_config(at_enabled):
    normalized = _make_normalized(
        tools=[[None, [_decl("get_weather")]]],
        tool_config=[None, [1]],
    )
    ctx = prepare_anti_truncation(normalized)
    assert ctx.active is True


# ---------------------------------------------------------------------------
# 非流式还原
# ---------------------------------------------------------------------------


def _make_output(**candidate_kwargs) -> ModelOutput:
    return ModelOutput(
        candidates=[Candidate(**candidate_kwargs)],
        model="models/gemini-2.5-flash",
    )


def _active_ctx(tool_name: str = "router_emit"):
    ctx = prepare_anti_truncation(_make_normalized(), override=True)
    assert ctx.active
    return ctx


def test_restore_nonstream_from_dict_args():
    ctx = _active_ctx()
    output = _make_output(
        function_calls=[{"name": "router_emit", "args": {"content": "你好世界"}}]
    )
    restore_anti_truncation_output(output, ctx)
    assert output.candidates[0].text == "你好世界"
    assert output.candidates[0].function_calls == []
    assert ctx.restored is True
    assert ctx.synthetic_seen is True


def test_restore_nonstream_from_json_string_arguments():
    ctx = _active_ctx()
    output = _make_output(
        function_calls=[
            {"name": "router_emit", "arguments": '{"content": "abc"}'}
        ]
    )
    restore_anti_truncation_output(output, ctx)
    assert output.candidates[0].text == "abc"
    assert ctx.restored is True


def test_restore_nonstream_passthrough_plain_text():
    ctx = _active_ctx()
    output = _make_output(text="纯文本回复")
    restore_anti_truncation_output(output, ctx)
    assert output.candidates[0].text == "纯文本回复"
    assert ctx.restored is False
    assert ctx.synthetic_seen is False


def test_restore_nonstream_keeps_real_calls():
    ctx = _active_ctx()
    real = {"name": "get_weather", "args": {"city": "Shanghai"}}
    output = _make_output(
        function_calls=[
            {"name": "router_emit", "args": {"content": "正文"}},
            dict(real),
        ]
    )
    restore_anti_truncation_output(output, ctx)
    assert output.candidates[0].text == "正文"
    assert output.candidates[0].function_calls == [real]


def test_restore_nonstream_concatenates_multiple_synthetic_calls():
    ctx = _active_ctx()
    output = _make_output(
        function_calls=[
            {"name": "router_emit", "args": {"content": "第一段"}},
            {"name": "router_emit", "args": {"content": "第二段"}},
        ]
    )
    restore_anti_truncation_output(output, ctx)
    assert output.candidates[0].text == "第一段第二段"
    assert ctx.multiple_calls is True


def test_restore_nonstream_inactive_ctx_is_noop():
    ctx = prepare_anti_truncation(_make_normalized())
    output = _make_output(
        function_calls=[{"name": "router_emit", "args": {"content": "x"}}]
    )
    restore_anti_truncation_output(output, ctx)
    assert output.candidates[0].text == ""
    assert output.candidates[0].function_calls  # 原样保留


# ---------------------------------------------------------------------------
# ContentDecoder 流式 JSON 识别器
# ---------------------------------------------------------------------------


def test_content_decoder_incremental_fragments():
    decoder = ContentDecoder()
    assert decoder.feed('{"content": "Hel') == "Hel"
    assert decoder.feed("lo wor") == "lo wor"
    assert decoder.feed('ld"}') == "ld"
    assert decoder.valid is True


def test_content_decoder_escapes_unicode_and_surrogate_pairs():
    decoder = ContentDecoder()
    raw = '{"content": "a\\nb\\u4e2d\\ud83d\\ude00"}'
    out = decoder.feed(raw)
    assert out == "a\nb中\U0001f600"
    assert decoder.valid is True


def test_content_decoder_rejects_garbage():
    decoder = ContentDecoder()
    decoder.feed("xyz")
    assert decoder.invalid is True
    assert decoder.valid is False


def test_content_decoder_ignores_non_content_keys():
    decoder = ContentDecoder()
    out = decoder.feed('{"other": "skip", "content": "keep"}')
    assert out == "keep"


# ---------------------------------------------------------------------------
# 流式还原
# ---------------------------------------------------------------------------


def _events(*items):
    async def gen():
        for item in items:
            yield item

    return gen()


async def _collect(stream):
    return [item async for item in stream]


async def test_stream_restore_string_fragments():
    ctx = _active_ctx()
    events = restore_anti_truncation_stream(
        _events(
            ("tool_calls", [{"name": "router_emit", "arguments": '{"content": "Hel'}]),
            ("tool_calls", [{"name": "router_emit", "arguments": "lo wor"}]),
            ("tool_calls", [{"name": "router_emit", "arguments": 'ld"}'}]),
            ("finish_reason", 10),
            ("usage", None),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    bodies = [p for t, p in out if t == "body"]
    assert "".join(bodies) == "Hello world"
    # 合成调用不外泄为 tool_calls 事件。
    assert all(t != "tool_calls" for t, _ in out)
    # 工具协议收尾码归一为 STOP(1)。
    fr = [p for t, p in out if t == "finish_reason"]
    assert fr == [1]
    assert ctx.restored is True
    assert ctx.synthetic_seen is True
    assert ctx.stream_done is True


async def test_stream_restore_dict_after_fragments_no_duplicate():
    """decoder 分片后补发完整 dict 参数帧：只补差异，不重复正文。"""
    ctx = _active_ctx()
    events = restore_anti_truncation_stream(
        _events(
            ("tool_calls", [{"name": "router_emit", "arguments": '{"content": "Hel'}]),
            ("tool_calls", [{"name": "router_emit", "args": {"content": "Hello world"}}]),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    bodies = [p for t, p in out if t == "body"]
    assert "".join(bodies) == "Hello world"


async def test_stream_restore_dict_snapshot_resend():
    """上游以完整 dict 快照重发：仅下发增量后缀。"""
    ctx = _active_ctx()
    events = restore_anti_truncation_stream(
        _events(
            ("tool_calls", [{"name": "router_emit", "args": {"content": "Hello"}}]),
            ("tool_calls", [{"name": "router_emit", "args": {"content": "Hello world"}}]),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    bodies = [p for t, p in out if t == "body"]
    assert bodies == ["Hello", " world"]


async def test_stream_plain_passthrough():
    ctx = _active_ctx()
    events = restore_anti_truncation_stream(
        _events(
            ("body", "你好"),
            ("body", "世界"),
            ("finish_reason", 1),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    bodies = [p for t, p in out if t == "body"]
    assert bodies == ["你好", "世界"]
    assert ctx.restored is False
    assert ctx.plain_seen is True


async def test_stream_plain_channel_wins_arbitration():
    """纯文本先到并胜出：后续工具通道正文被抑制，不重复。"""
    ctx = _active_ctx()
    events = restore_anti_truncation_stream(
        _events(
            ("body", "Hello"),
            ("tool_calls", [{"name": "router_emit", "arguments": '{"content": "Hello"}'}]),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    bodies = [p for t, p in out if t == "body"]
    assert bodies == ["Hello"]
    assert ctx.plain_seen is True


async def test_stream_synthetic_suppresses_plain_echo():
    ctx = _active_ctx()
    events = restore_anti_truncation_stream(
        _events(
            ("tool_calls", [{"name": "router_emit", "arguments": '{"content": "Hel'}]),
            ("body", "Hello"),  # 纯文本回声
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    bodies = [p for t, p in out if t == "body"]
    assert "".join(bodies) == "Hel"


async def test_stream_real_calls_pass_through():
    ctx = _active_ctx()
    real = {"name": "get_weather", "args": {"city": "Shanghai"}}
    events = restore_anti_truncation_stream(
        _events(
            ("tool_calls", [dict(real)]),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    tool_events = [p for t, p in out if t == "tool_calls"]
    assert tool_events == [[real]]


async def test_stream_thought_signature_emitted_before_body():
    ctx = _active_ctx()
    events = restore_anti_truncation_stream(
        _events(
            (
                "tool_calls",
                [
                    {
                        "name": "router_emit",
                        "args": {"content": "Hello"},
                        "thought_signature": "sig-abc",
                    }
                ],
            ),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    types = [t for t, _ in out]
    assert "thought_signature" in types
    assert types.index("thought_signature") < types.index("body")


async def test_stream_inactive_ctx_passthrough():
    ctx = prepare_anti_truncation(_make_normalized())
    events = restore_anti_truncation_stream(
        _events(
            ("body", "x"),
            ("tool_calls", [{"name": "router_emit", "args": {"content": "y"}}]),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    assert [t for t, _ in out] == ["body", "tool_calls", "done"]


async def test_stream_usage_and_response_id_pass_through():
    ctx = _active_ctx()
    usage = {"prompt_tokens": 5, "completion_tokens": 10}
    events = restore_anti_truncation_stream(
        _events(
            ("response_id", "resp_1"),
            ("usage", usage),
            ("done", None),
        ),
        ctx,
    )
    out = await _collect(events)
    assert ("response_id", "resp_1") in out
    assert ("usage", usage) in out


# ---------------------------------------------------------------------------
# 请求头解析
# ---------------------------------------------------------------------------


def _request_with_header(value: str | None) -> Request:
    headers = [] if value is None else [(b"x-anti-truncation", value.encode())]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }
    return Request(scope)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1", True),
        ("true", True),
        ("ON", True),
        ("Yes", True),
        ("0", False),
        ("false", False),
        ("OFF", False),
        ("No", False),
        (None, None),
        ("bogus", None),
    ],
)
def test_parse_anti_truncation_header(raw, expected):
    assert _parse_anti_truncation_header(_request_with_header(raw)) == expected


# ---------------------------------------------------------------------------
# 端到端链路（路由 → 请求头 → 注入 → 假客户端 → 还原 → 响应）
# ---------------------------------------------------------------------------


async def test_end_to_end_generate_content_with_header(monkeypatch):
    import httpx

    from aistudio_api.api import state as api_state
    from aistudio_api.api.app import app
    from aistudio_api.application import api_service_gemini as svc

    captured: dict[str, object] = {}

    class StubClient:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return ModelOutput(
                candidates=[
                    Candidate(
                        text="",
                        function_calls=[
                            {
                                "name": "router_emit",
                                "args": {"content": "端到端正文"},
                            }
                        ],
                        finish_reason=1,
                    )
                ],
                model="models/gemini-2.5-flash",
                usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                response_id="resp_e2e",
            )

    async def fake_ensure_active_account(attempt, model=None):
        return None

    monkeypatch.setattr(svc, "ensure_active_account", fake_ensure_active_account)
    monkeypatch.setattr(svc, "record_rotator_event", lambda *a, **k: None)
    monkeypatch.setattr(api_state.runtime_state, "client", StubClient())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            "/v1beta/models/gemini-2.5-flash:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            headers={"X-Anti-Truncation": "1"},
        )

    assert resp.status_code == 200
    data = resp.json()
    parts = data["candidates"][0]["content"]["parts"]
    assert parts[0]["text"] == "端到端正文"
    assert data["candidates"][0]["finishReason"] == "STOP"

    # 请求侧注入确实发生：合成声明 + 末尾传输指令。
    tools = captured["tools"]
    assert isinstance(tools, list) and tools and tools[0][1]
    names = [decl[0] for decl in tools[0][1]]
    assert "router_emit" in names
    contents = captured["contents"]
    assert isinstance(contents, list)
    tail_text = contents[-1].parts[0].text or ""
    assert "router_emit" in tail_text


async def test_end_to_end_header_off_disables_injection(monkeypatch):
    import httpx

    from aistudio_api.api import state as api_state
    from aistudio_api.api.app import app
    from aistudio_api.application import api_service_gemini as svc

    captured: dict[str, object] = {}

    class StubClient:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return ModelOutput(
                candidates=[Candidate(text="普通正文", finish_reason=1)],
                model="models/gemini-2.5-flash",
                usage={},
            )

    async def fake_ensure_active_account(attempt, model=None):
        return None

    monkeypatch.setattr(svc, "ensure_active_account", fake_ensure_active_account)
    monkeypatch.setattr(svc, "record_rotator_event", lambda *a, **k: None)
    monkeypatch.setattr(api_state.runtime_state, "client", StubClient())
    monkeypatch.setenv("AISTUDIO_ANTI_TRUNCATION", "1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            "/v1beta/models/gemini-2.5-flash:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            headers={"X-Anti-Truncation": "0"},
        )

    assert resp.status_code == 200
    assert resp.json()["candidates"][0]["content"]["parts"][0]["text"] == "普通正文"
    # 请求头强制关闭：未注入合成工具。
    assert captured.get("tools") is None
