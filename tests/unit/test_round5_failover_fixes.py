"""第五轮优化回归测试：故障转移链路与跨线程 Cookie 安全。

覆盖本轮修复的三个高严重度问题：

1. ``handle_attempt_exception`` 的 UnboundLocalError：serving_account_id
   非空（有活跃账号时的正常路径）时 SessionExpiredError 分支引用了只在
   else 分支赋值的局部变量，导致「登录态失效→探活轮换→切号」整条故障
   转移链路 500。
2. ``Auth403RetryManager.handle_403`` 第 3/5 次重试用全局活跃账号而非本
   请求实际服务账号（并发串号）。
3. ``_persist_set_cookies`` 在工作线程逐键变更共享 Cookie dict：与事件
   循环上的迭代并发时抛 "dictionary changed size during iteration"。
4. stream_parser 快速跳扫与原逐字符状态机行为等价（含转义/跨块边界）。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi import HTTPException

from aistudio_api.application import api_service_gemini as gemini_svc
from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.account.cookie_parser import (
    filter_minimal_cookies,
)
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)


class _FakeAccount:
    def __init__(self, account_id: str) -> None:
        self.id = account_id


class _FakeAccountService:
    """最小账号服务桩：只实现 handle_attempt_exception 用到的接口。"""

    def __init__(self, accounts: dict[str, _FakeAccount], active_id: str | None) -> None:
        self._accounts = accounts
        self._active_id = active_id
        self.rotated: list[str] = []

    def get_account(self, account_id: str) -> _FakeAccount | None:
        return self._accounts.get(account_id)

    def get_active_account(self) -> _FakeAccount | None:
        if self._active_id and self._active_id in self._accounts:
            return self._accounts[self._active_id]
        return None

    def get_account_auth_path(self, account_id: str) -> str | None:
        return None  # 无 auth 文件 → 轮换探测直接跳过

    async def rotate_account_cookies(self, account_id: str, session: Any) -> dict:
        self.rotated.append(account_id)
        return {"ok": False}


@pytest.mark.asyncio
async def test_session_expired_with_serving_account_id_does_not_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serving_account_id 非空时 SessionExpiredError 不得 UnboundLocalError。

    修复前：if 分支只赋值 serving_acc，SessionExpiredError 分支引用
    active_acc → UnboundLocalError，切号兜底全部失效（500）。
    """
    accounts = {"acc_A": _FakeAccount("acc_A")}
    account_svc = _FakeAccountService(accounts, active_id="acc_A")
    monkeypatch.setattr(gemini_svc.runtime_state, "account_service", account_svc)
    monkeypatch.setattr(gemini_svc, "try_switch_account", lambda **k: _noop_true())
    monkeypatch.setattr(gemini_svc, "record_rotator_event", lambda *a, **k: None)

    retry = await gemini_svc.handle_attempt_exception(
        SessionExpiredError("登录态已失效"),
        attempt=0,
        model_path="models/gemini-3.7-flash",
        normalized_model="gemini-3.7-flash",
        has_yielded_data=False,
        serving_account_id="acc_A",
    )
    assert retry is True  # 切号成功 → 允许重试


@pytest.mark.asyncio
async def test_session_expired_switch_failure_raises_401_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切号失败时应抛出 401 HTTPException，而不是 UnboundLocalError/500。"""
    account_svc = _FakeAccountService({"acc_A": _FakeAccount("acc_A")}, "acc_A")
    monkeypatch.setattr(gemini_svc.runtime_state, "account_service", account_svc)
    monkeypatch.setattr(gemini_svc, "try_switch_account", lambda **k: _noop_false())
    monkeypatch.setattr(gemini_svc, "record_rotator_event", lambda *a, **k: None)

    with pytest.raises(HTTPException) as exc_info:
        await gemini_svc.handle_attempt_exception(
            SessionExpiredError("登录态已失效"),
            attempt=2,
            model_path="models/gemini-3.7-flash",
            normalized_model="gemini-3.7-flash",
            has_yielded_data=False,
            serving_account_id="acc_A",
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_handle_403_rotates_serving_account_not_global_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第 3 次 403 的 Cookie 轮换必须作用于本请求的服务账号。

    修复前用的是全局活跃账号：并发下活跃账号已被切走时，轮换的是无关
    账号（救不了当前请求还污染别人）。
    """
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    accounts = {
        "acc_serving": _FakeAccount("acc_serving"),
        "acc_global": _FakeAccount("acc_global"),
    }
    account_svc = _FakeAccountService(accounts, active_id="acc_global")
    monkeypatch.setattr(gemini_svc.runtime_state, "account_service", account_svc)

    switched: dict[str, Any] = {}

    async def fake_switch(*, model: str, failed_account_id: str | None, **kw: Any):
        switched["failed"] = failed_account_id
        return True

    monkeypatch.setattr(gemini_svc, "try_switch_account", fake_switch)
    monkeypatch.setattr(gemini_svc, "record_rotator_event", lambda *a, **k: None)

    manager = gemini_svc.Auth403RetryManager()
    manager.serving_account_id = "acc_serving"

    for round_index in range(5):
        exc = HTTPException(403, detail={"message": "forbidden"})
        retry = await manager.handle_403(
            exc, model="gemini-3.7-flash", client=AIStudioClient()
        )
        if round_index < 4:
            assert retry is True

    # 第 3 轮：轮换的是 serving 账号而非全局活跃账号
    assert account_svc.rotated == ["acc_serving"]
    # 第 5 轮：切号排除的也是 serving 账号
    assert switched["failed"] == "acc_serving"


@pytest.mark.asyncio
async def test_persist_set_cookies_no_dict_iteration_race() -> None:
    """工作线程 Set-Cookie 回写 vs 事件循环迭代的并发安全。

    修复前：_persist_set_cookies 在线程内逐键插入共享 dict，与事件循环
    上的 filter_minimal_cookies 迭代并发必触发 RuntimeError。
    修复后：线程在快照上合并 + 单次原子 update；读侧先 dict() 快照。
    """
    from aistudio_api.infrastructure.gateway.transport import _persist_set_cookies

    shared: dict[str, str] = {
        "SAPISID": "sapisid",
        "__Secure-1PSID": "psid",
        "__Secure-3PSID": "psid3",
        "SID": "sid",
        "HSID": "hsid",
        "SSID": "ssid",
    }
    set_cookie_headers = [
        f"__Secure-1PSIDTS=fresh-{i}; Path=/; Secure; HttpOnly" for i in range(3)
    ]
    errors: list[Exception] = []
    stop = threading.Event()

    def reader() -> None:
        """模拟事件循环线程：持续迭代共享 dict（快照 + 过滤）。"""
        while not stop.is_set():
            try:
                for _ in range(200):
                    filtered = filter_minimal_cookies(shared)
                    assert "SAPISID" in filtered
            except Exception as exc:
                errors.append(exc)
                return

    def writer() -> None:
        """模拟工作线程：Set-Cookie 回写（含新键插入）。"""
        for _ in range(200):
            _persist_set_cookies(list(set_cookie_headers), None, shared)

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        # 在主线程跑写者即可：读者线程的迭代与写者的插入真正并发
        writer()
    finally:
        stop.set()
        reader_thread.join(timeout=5)

    assert errors == [], f"并发迭代触发异常: {errors}"
    assert shared["__Secure-1PSIDTS"] == "fresh-2"
    # 原有键不受影响
    assert shared["SAPISID"] == "sapisid"


def _noop_true():
    async def _inner() -> bool:
        return True

    return _inner()


def _noop_false():
    async def _inner() -> bool:
        return False

    return _inner()


# ========== stream_parser 快速跳扫等价性 ==========


def _chunks_of(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _collect(parser: IncrementalJSONStreamParser, chunks: list[str]) -> list[object]:
    out: list[object] = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    out.extend(parser.finish())
    return out


def test_fast_scan_matches_reference_on_tricky_payloads() -> None:
    """跳扫状态机与原逐字符实现行为等价：转义/嵌套/截断/跨块边界。"""
    payload = (
        ")]}'\n"
        '[[["你好\\n世界","含\\"引号\\"文本",123]],[null,{"prompt":1}],null,"resp_1"]'
    )
    reference = _collect(IncrementalJSONStreamParser(), [payload])
    for size in (1, 2, 3, 7, 13, 10_000):
        got = _collect(IncrementalJSONStreamParser(), _chunks_of(payload, size))
        assert got == reference, f"分块大小 {size} 下结果不一致"


def test_fast_scan_large_inline_data() -> None:
    """大体积 base64（inline_data 图像）经跳扫后仍完整切出。"""
    import base64

    blob = base64.b64encode(b"x" * 300_000).decode()
    payload = (
        ")]}'\n["
        f'[[["text",0],null,{{"inline_data":{{"data":"{blob}"}}}}]],'
        '[null,{"p":1}],null,"resp_img"]'
    )
    parser = IncrementalJSONStreamParser()
    events = _collect(parser, _chunks_of(payload, 8192))
    assert len(events) >= 1
    # 大 payload 被完整无损切出（blob 嵌在结构深处，全序列化后检查）
    import json as _json

    assert blob[:64] in _json.dumps(events, ensure_ascii=False)
    assert blob[-64:] in _json.dumps(events, ensure_ascii=False)


def test_fast_scan_oversized_image_feed_latency() -> None:
    """性能回归护栏：1.5MB base64 分块解析应在毫秒级（跳扫后）。

    逐字符实现实测 ~190ms/MB（1.5MB ≈ 227ms 纯 CPU 阻塞事件循环）；
    跳扫后应 < 20ms。阈值留 10 倍余量防 CI 抖动。
    """
    import base64

    blob = base64.b64encode(b"y" * 1_100_000).decode()  # ~1.47MB
    chunk_json = f'[[["x",0],null,{{"data":"{blob}"}}]],'
    payload = ")]}'\n[" + chunk_json + '[null,{"p":1}],"r1"]'
    parser = IncrementalJSONStreamParser()
    start = time.perf_counter()
    events = _collect(parser, _chunks_of(payload, 64_000))
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert events, "大分块必须被切出"
    assert elapsed_ms < 20.0, f"feed 解析耗时回归: {elapsed_ms:.1f}ms（跳扫后应 <20ms）"
