"""账号会话一致性与 Claude effort 映射回归测试。

背景（2026-11-26 线上问题）：
1. 「明明激活的账号是第二个，请求却用第一个（甚至已停用）」——
   ``AccountStore.get_active_account`` 的禁用回退会重写
   ``active_account_id`` 而不切网关会话；``try_switch_account`` 的快路径
   只看注册表，误判「已切换完成」。修复：session.ensure_ready 对账 +
   orchestrator 漂移校正 + toggle 路由先取切换前活跃账号。
2. 「Claude Code 开 Max 却没映射到 xhigh」——effort 走顶层
   ``output_config: {"effort": "max"}``（schema 原本无此字段被 pydantic
   丢弃），``thinking: {"type": "adaptive"}`$ 本身不携带档位。
   修复：schema 收编 output_config + compat_claude effort 归一化接入
   reasoningEffort 通道（max → xhigh）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import try_switch_account
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.account_service import AccountService
from aistudio_api.application.compat_service import claude_to_gemini_request
from aistudio_api.config import settings
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.session import GatewaySession


def _bound_account_id(session: GatewaySession) -> str | None:
    ctx = session.active_context
    if ctx is None or not ctx.auth_file:
        return None
    return Path(ctx.auth_file).parent.name


@pytest.fixture
async def two_account_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AccountStore, AccountService, GatewaySession, str, str]:
    """双账号环境：A（u/0，导入顺序在前）与 B（u/1），A 为初始活跃账号。

    用 AISTUDIO_ACCOUNTS_DIR 指向临时目录，使 session 的惰性 AccountStore
    单例与显式构造的 store 解析到同一账号池。
    """
    accounts_dir = tmp_path / "accounts"
    monkeypatch.setenv("AISTUDIO_ACCOUNTS_DIR", str(accounts_dir))
    store = AccountStore()
    account_a = store.save_account(
        name="Account A",
        email="a@gmail.com",
        storage_state={"cookies": [{"name": "SAPISID", "value": "sid-a"}]},
        auth_user="0",
        tier="Pro",
    )
    account_b = store.save_account(
        name="Account B",
        email="b@gmail.com",
        storage_state={"cookies": [{"name": "SAPISID", "value": "sid-b"}]},
        auth_user="1",
        tier="Pro",
    )
    service = AccountService(store)
    session = GatewaySession()
    # 初始激活第一个账号（与批量导入「首个导入账号自动激活」一致）
    activated = await service.activate_account(account_a.id, session)
    assert activated is not None
    assert _bound_account_id(session) == account_a.id
    return store, service, session, account_a.id, account_b.id


# ---------------------------------------------------------------------------
# Bug1：注册表活跃账号与会话上下文分叉
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ready_reconciles_after_disable_fallback_rewrite(
    two_account_env,
) -> None:
    """禁用回退只重写注册表时，ensure_ready 应把会话对齐到回退后的活跃账号。"""
    store, _service, session, acc_a, acc_b = two_account_env

    # 用户禁用当前活跃账号 A：get_active_account 的禁用回退把
    # active_account_id 重写为首个启用账号 B（只改注册表，不切会话）
    store.toggle_account_enabled(acc_a, False)
    assert store.get_active_account().id == acc_b
    # 分叉出现：注册表说 B 活跃，会话仍停在 A（旧实现的 bug 现场）
    assert _bound_account_id(session) == acc_a

    ctx = await session.ensure_ready()
    assert ctx.auth_file is not None
    assert _bound_account_id(session) == acc_b
    assert ctx.auth_user == "1"


@pytest.mark.asyncio
async def test_try_switch_account_corrects_session_drift(
    two_account_env,
) -> None:
    """快路径必须校验会话一致性：分叉时实际切号而非凭注册表返回 True。"""
    store, service, session, acc_a, acc_b = two_account_env
    store.toggle_account_enabled(acc_a, False)
    # 触发禁用回退重写（注册表 active → B），会话仍在 A 上
    store.get_active_account()

    client = MagicMock()
    client.session = session
    rotator = AccountRotator(store)

    orig = (runtime_state.rotator, runtime_state.account_service, runtime_state.client)
    runtime_state.rotator = rotator
    runtime_state.account_service = service
    runtime_state.client = client
    try:
        # 旧实现：锁外/锁内快路径看到 failed=A、当前=B 即返回 True，
        # 会话不动 → 请求继续用已停用的 A 发出
        switched = await try_switch_account(failed_account_id=acc_a)
        assert switched is True
        assert _bound_account_id(session) == acc_b
        rebased = store.get_active_account()
        assert rebased is not None
        assert rebased.id == acc_b
    finally:
        (
            runtime_state.rotator,
            runtime_state.account_service,
            runtime_state.client,
        ) = orig


@pytest.mark.asyncio
async def test_toggle_route_switches_session_when_disabling_active_account(
    two_account_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面板禁用「当前活跃账号」时，会话必须真正切到下一个可用账号。"""
    store, service, session, acc_a, acc_b = two_account_env
    rotator = AccountRotator(store)
    client = MagicMock()
    client.session = session

    monkeypatch.setattr(settings, "web_password", "admin-test")
    monkeypatch.setattr(
        "aistudio_api.api.dependencies._all_configured_api_keys",
        lambda: frozenset(),
    )
    orig = (runtime_state.rotator, runtime_state.account_service, runtime_state.client)
    runtime_state.rotator = rotator
    runtime_state.account_service = service
    runtime_state.client = client
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            resp = await http.post(
                f"/api/accounts/{acc_a}/toggle",
                json={"enabled": False},
                headers={"Authorization": "Bearer admin-test"},
            )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        # 关键断言：注册表与会话都切到 B，后续请求不再走已停用的 A
        assert store.get_active_account().id == acc_b
        assert _bound_account_id(session) == acc_b
    finally:
        (
            runtime_state.rotator,
            runtime_state.account_service,
            runtime_state.client,
        ) = orig


@pytest.mark.asyncio
async def test_ensure_ready_noop_when_session_already_matches_registry(
    two_account_env,
) -> None:
    """正常路径（无分叉）对账零副作用：会话保持且不产生额外切换。"""
    _store, _service, session, acc_a, _acc_b = two_account_env

    ctx_before = session.active_context
    ctx = await session.ensure_ready()
    assert ctx is ctx_before
    assert _bound_account_id(session) == acc_a


@pytest.mark.asyncio
async def test_ensure_ready_skips_reconcile_for_non_pool_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """legacy settings.auth_file（不在账号池内）不被对账越权改写。"""
    from aistudio_api.infrastructure.gateway.session import SessionContext

    accounts_dir = tmp_path / "accounts_legacy"
    monkeypatch.setenv("AISTUDIO_ACCOUNTS_DIR", str(accounts_dir))
    store = AccountStore()
    store.save_account(
        name="Pool Account",
        email="pool@gmail.com",
        storage_state={"cookies": [{"name": "SAPISID", "value": "sid-pool"}]},
    )

    legacy_auth = tmp_path / "auth.json"
    session = GatewaySession()
    session.set_ready_context(
        SessionContext(
            auth_user="0",
            auth_file=str(legacy_auth),
            cookies={"SAPISID": "legacy"},
        )
    )
    ctx = await session.ensure_ready()
    assert ctx.auth_file == str(legacy_auth)


# ---------------------------------------------------------------------------
# Bug2：Claude effort（output_config.effort）→ 思考档位
# ---------------------------------------------------------------------------


def _claude_request(payload: dict[str, Any]) -> ClaudeMessagesRequest:
    base = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    return ClaudeMessagesRequest.model_validate({**base, **payload})


@pytest.mark.asyncio
async def test_claude_effort_max_maps_to_xhigh() -> None:
    """Claude Code Max 档（adaptive thinking + output_config.effort=max）→ xhigh。"""
    req = _claude_request(
        {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "max"},
        }
    )
    gemini_req, _resolved = await claude_to_gemini_request(req)
    gc = gemini_req.generationConfig
    assert gc is not None
    # max 归一化为 xhigh 顶格档（目录 6 档 EXTRA_HIGH 的文本标签）
    assert gc.reasoningEffort == "xhigh"
    # adaptive 无预算：不再落入 budget 推断的 thinkingConfig
    assert gc.thinkingConfig is None


@pytest.mark.asyncio
async def test_claude_effort_words_and_none() -> None:
    for word in ("high", "very_high", "none"):
        req = _claude_request({"output_config": {"effort": word}})
        gemini_req, _ = await claude_to_gemini_request(req)
        gc = gemini_req.generationConfig
        assert gc is not None
        assert gc.reasoningEffort == word


@pytest.mark.asyncio
async def test_claude_effort_priority_over_budget_tokens() -> None:
    """effort 与 budget_tokens 并存时，显式 effort 优先（budget 只剩兜底语义）。"""
    req = _claude_request(
        {
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "output_config": {"effort": "max"},
        }
    )
    gemini_req, _ = await claude_to_gemini_request(req)
    gc = gemini_req.generationConfig
    assert gc is not None
    assert gc.reasoningEffort == "xhigh"
    assert gc.thinkingConfig is None


@pytest.mark.asyncio
async def test_claude_budget_mapping_without_effort_unchanged() -> None:
    """无 effort 时 budget_tokens 映射维持既有行为。"""
    req = _claude_request({"thinking": {"type": "enabled", "budget_tokens": 4096}})
    gemini_req, _ = await claude_to_gemini_request(req)
    gc = gemini_req.generationConfig
    assert gc is not None
    assert gc.thinkingConfig == {"thinkingLevel": "MEDIUM"}
    assert gc.reasoningEffort is None


@pytest.mark.asyncio
async def test_claude_effort_thinking_variant_and_unknown_word() -> None:
    """个别网关把 effort 塞进 thinking.effort 的变体也生效；无法识别的词忽略。"""
    req = _claude_request({"thinking": {"type": "adaptive", "effort": "max"}})
    gemini_req, _ = await claude_to_gemini_request(req)
    gc = gemini_req.generationConfig
    assert gc is not None
    assert gc.reasoningEffort == "xhigh"

    req = _claude_request({"output_config": {"effort": "bogus"}})
    gemini_req, _ = await claude_to_gemini_request(req)
    gc = gemini_req.generationConfig
    assert gc is not None
    assert gc.reasoningEffort is None
    assert gc.thinkingConfig is None


@pytest.mark.asyncio
async def test_claude_adaptive_without_effort_keeps_default() -> None:
    """adaptive 无 effort：不注入档位，profile 默认档照常生效（原行为）。"""
    req = _claude_request({"thinking": {"type": "adaptive"}})
    gemini_req, _ = await claude_to_gemini_request(req)
    gc = gemini_req.generationConfig
    assert gc is not None
    assert gc.reasoningEffort is None
    assert gc.thinkingConfig is None
