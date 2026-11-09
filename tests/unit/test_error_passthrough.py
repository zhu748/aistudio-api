"""失败可见性测试：上游/签名/代理失败时，错误必须能被看到。

复现用户在 Render + Chatbox 场景的问题：
- Chatbox 走 streamGenerateContent，代理返回 HTTP 200 + SSE 流；
- 失败时旧实现只发 ``{"error": ...}`` 事件，Chatbox 的 Gemini 解析器
  只读 ``candidates[].content.parts[].text``，会直接忽略 → 空回，
  用户端什么都看不到。
- 修复后：失败且尚未输出任何内容时，把错误以文本 part 透传，
  任何客户端都能看到具体失败原因；同时 /api/logs 能查到错误日志。

全程使用假客户端与假账号，不触碰真实 Cookie / 不发起真实上游请求。
"""

from __future__ import annotations

import json

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.application import api_service_gemini as gemini_svc
from aistudio_api.domain.errors import AuthError, TransportError
from aistudio_api.infrastructure.utils.logger import (
    clear_recent_logs,
    get_recent_logs,
)

_REQUEST_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "你好"}]}],
}


class _FakeAccount:
    id = "acc_fake_1"
    tier = "Pro"
    email = "fake@example.com"


class _FakeAccountService:
    def get_active_account(self):
        return _FakeAccount()


class _FailingStreamClient:
    """模拟上游流式请求失败的 AIStudioClient（不触网）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        # 403 阶梯重试会读取该属性（真实客户端由 prepare_request 写入）
        self.last_prepared_request = None
        self.session = None

    async def stream_generate_content(self, **kwargs):
        raise self._exc
        yield  # pragma: no cover - 仅为使其成为异步生成器

    async def generate_content(self, **kwargs):
        raise self._exc


@pytest.fixture
def isolated_runtime(monkeypatch):
    """隔离账号编排与轮换：只测错误可见性，不测切号逻辑。"""
    from aistudio_api.api.state import runtime_state

    monkeypatch.setattr(gemini_svc, "ensure_active_account",
                        lambda attempt, model=None: _noop())
    monkeypatch.setattr(gemini_svc, "try_switch_account",
                        lambda *a, **k: _noop_false())
    monkeypatch.setattr(gemini_svc, "record_rotator_event", lambda *a, **k: None)

    old_client = runtime_state.client
    old_svc = runtime_state.account_service
    from typing import cast

    from aistudio_api.application.account_service import AccountService

    # 测试桩仅实现鸭子类型接口，用 cast 告知类型检查器
    runtime_state.account_service = cast(AccountService, _FakeAccountService())
    yield runtime_state
    runtime_state.client = old_client
    runtime_state.account_service = old_svc


async def _noop() -> None:
    return None


async def _noop_false() -> bool:
    return False


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _collect_sse(resp: httpx.Response) -> list[dict]:
    events = []
    for block in resp.text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            events.append(json.loads(block[len("data: "):]))
    return events


@pytest.mark.asyncio
async def test_stream_transport_error_yields_visible_error_text(isolated_runtime):
    """代理/网络失败（Render 最典型）：流式 200 空回 → 必须透传可见错误文本。"""
    isolated_runtime.client = _FailingStreamClient(
        TransportError(
            "HTTP request failed: ConnectError: [Errno 111] Connection refused"
        )
    )
    clear_recent_logs()

    async with _client() as client:
        resp = await client.post(
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
            json=_REQUEST_BODY,
        )

    assert resp.status_code == 200  # SSE 已开始，状态码无法改变
    events = await _collect_sse(resp)
    assert events, "SSE 流不应为空"

    # 核心断言：必须存在一个含文本 part 的事件，把错误原因说清楚
    text_parts = [
        p["text"]
        for ev in events
        for cand in ev.get("candidates", [])
        for p in cand.get("content", {}).get("parts", [])
        if "text" in p
    ]
    assert text_parts, (
        "失败时未透传任何可见文本 —— Chatbox 会显示为空回复，"
        "这正是 '空回啥都没有' 的根因"
    )
    joined = "\n".join(text_parts)
    assert "ConnectError" in joined or "Connection refused" in joined
    # 结构化错误事件也保留（SDK 兼容）
    assert any("error" in ev for ev in events)

    # 同时：面板日志里必须能查到这条失败记录
    logs = get_recent_logs(min_level="WARNING", limit=100)
    log_text = "\n".join(str(e.get("message")) for e in logs)
    assert "ConnectError" in log_text or "流式生成失败" in log_text


@pytest.mark.asyncio
async def test_stream_auth_403_error_yields_visible_error_text(isolated_runtime):
    """上游 403（账号风控）：同样必须可见，不能静默空回。"""
    isolated_runtime.client = _FailingStreamClient(
        AuthError("权限被拒绝 (HTTP 403): PERMISSION_DENIED")
    )
    clear_recent_logs()

    async with _client() as client:
        resp = await client.post(
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
            json=_REQUEST_BODY,
        )

    events = await _collect_sse(resp)
    text_parts = [
        p["text"]
        for ev in events
        for cand in ev.get("candidates", [])
        for p in cand.get("content", {}).get("parts", [])
        if "text" in p
    ]
    assert text_parts, "403 失败也需要可见的错误文本"
    assert "403" in "\n".join(text_parts)


@pytest.mark.asyncio
async def test_nonstream_error_returns_gemini_json_error(isolated_runtime):
    """非流式：返回标准 Gemini 错误 JSON 与 HTTP 状态码（Chatbox 可直接展示）。"""
    isolated_runtime.client = _FailingStreamClient(
        TransportError("Remote proof minting failed after 2 attempts: timeout")
    )

    async with _client() as client:
        resp = await client.post(
            "/v1beta/models/gemini-3.8-flash:generateContent",
            json=_REQUEST_BODY,
        )

    assert resp.status_code in (500, 502)
    data = resp.json()
    assert "error" in data
    assert data["error"]["message"]
    assert "Remote proof minting failed" in data["error"]["message"]


@pytest.mark.asyncio
async def test_stream_midway_error_keeps_partial_output(isolated_runtime):
    """已产出部分内容后再失败：不追加错误文本污染正文，只发结构化错误事件。"""

    class _MidwayClient:
        async def stream_generate_content(self, **kwargs):
            yield ("body", "前半段正常输出")
            raise TransportError("upstream connection truncated mid-stream")
            yield  # pragma: no cover

    isolated_runtime.client = _MidwayClient()

    async with _client() as client:
        resp = await client.post(
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
            json=_REQUEST_BODY,
        )

    events = await _collect_sse(resp)
    text_parts = [
        p["text"]
        for ev in events
        for cand in ev.get("candidates", [])
        for p in cand.get("content", {}).get("parts", [])
        if "text" in p
    ]
    joined = "\n".join(text_parts)
    assert "前半段正常输出" in joined
    # 中途失败不追加错误文本（保持已生成内容纯净）
    assert "truncated" not in joined
    # 但结构化错误事件仍然下发
    assert any("error" in ev for ev in events)


@pytest.mark.asyncio
async def test_stream_upstream_200_empty_injects_diagnostic(isolated_runtime):
    """上游 200 但零内容（风控拦截变体）：注入可见诊断文本而非静默空回。"""

    class _EmptyStreamClient:
        async def stream_generate_content(self, **kwargs):
            # 只发 usage/finish 事件，无任何内容事件
            yield ("usage", {"prompt_tokens": 10, "completion_tokens": 0,
                             "total_tokens": 10})
            yield ("finish_reason", 1)

    isolated_runtime.client = _EmptyStreamClient()
    clear_recent_logs()

    async with _client() as client:
        resp = await client.post(
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
            json=_REQUEST_BODY,
        )

    events = await _collect_sse(resp)
    text_parts = [
        p["text"]
        for ev in events
        for cand in ev.get("candidates", [])
        for p in cand.get("content", {}).get("parts", [])
        if "text" in p
    ]
    joined = "\n".join(text_parts)
    assert joined, "上游 200 空内容也必须有诊断文本，不能静默空回"
    assert "空回诊断" in joined
    # 日志同步留痕
    logs = get_recent_logs(min_level="WARNING", limit=50)
    log_text = "\n".join(str(e.get("message")) for e in logs)
    assert "未产生任何内容" in log_text


@pytest.mark.asyncio
async def test_nonstream_upstream_200_empty_injects_diagnostic(isolated_runtime):
    """非流式上游 200 但零内容：响应体内注入诊断 part。"""
    from aistudio_api.domain.models import Candidate, ModelOutput

    class _EmptyOutputClient:
        async def generate_content(self, **kwargs):
            return ModelOutput(
                candidates=[Candidate(text="", finish_reason=1)],
                model="models/gemini-3.8-flash",
            )

    isolated_runtime.client = _EmptyOutputClient()

    async with _client() as client:
        resp = await client.post(
            "/v1beta/models/gemini-3.8-flash:generateContent",
            json=_REQUEST_BODY,
        )

    assert resp.status_code == 200
    data = resp.json()
    parts = (
        data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    )
    joined = "\n".join(p.get("text", "") for p in parts)
    assert "空回诊断" in joined
