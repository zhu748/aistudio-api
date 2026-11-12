"""WAA 签名器健康熔断（signer-grade attestation 质量降级）回归测试。

背景与判据来源：2026-11-12 新抓包（最新抓包.txt，手机浏览器成功会话）+
SOCKS5 出口代理线上实测矩阵：

- 原版浏览器 proof 重放 4/4 200（账号 / Cookie / 代理 / 请求形态全健康）；
- spinsnow 新铸 proof 4/4 403，且 5 次 mint（跨模型/跨 prompt）全部携带
  同一过期挑战构建（challenge_version 3109903533，浏览器当日为
  3153048796；加密环境载荷 1015B vs 1352-1436B）—— 服务级降级铁证。

结论：403 存在第四种语义「签名器 attestation 质量降级」，其与语义 3
（账号级限流）的请求内表现相同（403 + 轻量端点 200），唯一可观测差异
在跨账号维度 —— 语义 4 全账号发生（签名器为公共依赖）。本套件验证
SignerHealthTracker 的滑动窗口熔断判据与 Auth403RetryManager 的短路
行为。详见 docs/CAPTURE_ALIGNMENT_2026-11.md §9。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from aistudio_api.infrastructure.waa.health import (
    SignerHealthTracker,
    get_signer_health,
    reset_signer_health_for_tests,
)


@pytest.fixture(autouse=True)
def _fresh_singleton():
    reset_signer_health_for_tests()
    yield
    reset_signer_health_for_tests()


# ─────────────────────── 熔断判据 ───────────────────────


def test_single_account_never_trips():
    """语义 3（账号级限流）安全侧：单账号重复拒绝不触发熔断。

    单账号部署/单账号故障无法与语义 3 区分，必须保守放行完整阶梯。
    """
    t = SignerHealthTracker(cooldown_seconds=60.0)
    for _ in range(10):
        t.record_proof_rejected("acc-1")
    assert not t.is_degraded()


def test_cross_account_correlation_trips():
    """语义 4 判据：窗口内 >=3 次拒绝且涉及 >=2 账号 → 熔断。"""
    t = SignerHealthTracker(cooldown_seconds=60.0)
    t.record_proof_rejected("acc-1")
    t.record_proof_rejected("acc-1")
    assert not t.is_degraded()  # 2 次同账号：未达账号数阈值
    t.record_proof_rejected("acc-2")
    assert t.is_degraded()
    assert t.cooldown_remaining() > 0


def test_accept_clears_degradation():
    """半开语义：熔断期内一次成功（proof 被接受）立即解除。"""
    t = SignerHealthTracker(cooldown_seconds=60.0)
    t.record_proof_rejected("acc-1")
    t.record_proof_rejected("acc-2")
    t.record_proof_rejected("acc-2")
    assert t.is_degraded()
    t.record_proof_accepted("acc-2")
    assert not t.is_degraded()
    # 清窗后重新累计（旧的拒绝事件不残留）
    t.record_proof_rejected("acc-3")
    assert not t.is_degraded()


def test_window_pruning():
    """窗口外事件不参与判定（冷却过期后由新事件重新判定）。"""
    t = SignerHealthTracker(window_seconds=0.05, cooldown_seconds=0.05)
    t.record_proof_rejected("acc-1")
    t.record_proof_rejected("acc-2")
    t.record_proof_rejected("acc-2")
    assert t.is_degraded()
    import time

    time.sleep(0.08)
    assert not t.is_degraded()  # 冷却自然结束
    # 窗口外旧事件已剪枝：新事件单独不足以触发
    t.record_proof_rejected("acc-3")
    assert not t.is_degraded()


def test_state_summary_shape():
    """管理端点快照字段完整性（/config/signer-health）。"""
    t = SignerHealthTracker()
    t.record_proof_rejected("acc-1")
    t.record_proof_rejected("acc-2")
    t.record_proof_rejected("acc-2")
    s = t.state_summary()
    assert s["degraded"] is True
    assert s["recentRejects"] == 3
    assert s["recentAccounts"] == 2
    assert s["cooldownRemainingSec"] > 0
    assert s["tripThreshold"]["rejects"] == 3
    assert s["lastTrip"]["accountsInvolved"] == 2


# ─────────────────────── 403 阶梯短路集成 ───────────────────────


class _StubClient:
    """handle_403 依赖的最小客户端桩（仅访问 last_prepared_request 属性）。"""

    last_prepared_request = None


def _make_manager():
    from aistudio_api.application.api_service_gemini import Auth403RetryManager

    return Auth403RetryManager()


def _drive_403(manager, exc, model="models/gemini-3.5-flash-lite"):
    return asyncio.run(
        manager.handle_403(exc, model=model, client=_StubClient())
    )


def test_403_ladder_intact_when_not_degraded():
    """未熔断：完整阶梯语义保持（2 重放 + 2 重签 + 1 切号）。"""
    from aistudio_api.domain.errors import AuthError

    mgr = _make_manager()
    mgr.serving_account_id = "acc-1"
    exc = AuthError("The caller does not have permission")
    # 4 跳均返回 True（继续重试），不因健康跟踪短路
    for expected_count in (1, 2, 3, 4):
        assert _drive_403(mgr, exc) is True
        assert mgr.count == expected_count


def test_403_short_circuits_when_degraded():
    """熔断中：第 3 跳（重签阶段）直接终止并标注 signer_degraded。"""
    from aistudio_api.domain.errors import AuthError

    health = get_signer_health()
    # 预置跨账号拒绝事件触发熔断（模拟此前请求已烧过一轮）
    health.record_proof_rejected("acc-1")
    health.record_proof_rejected("acc-1")
    health.record_proof_rejected("acc-2")
    assert health.is_degraded()

    mgr = _make_manager()
    mgr.serving_account_id = "acc-3"
    exc = AuthError("The caller does not have permission")
    # 前两跳（免费重放）保持可用
    assert _drive_403(mgr, exc) is True
    assert _drive_403(mgr, exc) is True
    # 第 3 跳熔断短路
    with pytest.raises(HTTPException) as ei:
        _drive_403(mgr, exc)
    assert ei.value.status_code == 403
    assert ei.value.detail["type"] == "signer_degraded"
    assert "签名" in ei.value.detail["message"]


def test_403_records_rejection_events():
    """每次 403 记入拒绝事件（跨账号聚合的输入源）。"""
    from aistudio_api.domain.errors import AuthError

    health = get_signer_health()
    mgr = _make_manager()
    mgr.serving_account_id = "acc-9"
    _drive_403(mgr, AuthError("no permission"))
    s = health.state_summary()
    assert s["recentRejects"] == 1
    assert s["recentAccounts"] == 1


def test_ladder_trip_through_handle_403():
    """端到端：跨账号 403 风暴经 handle_403 聚合后触发熔断并短路后续请求。"""
    from aistudio_api.domain.errors import AuthError

    exc = AuthError("The caller does not have permission")
    health = get_signer_health()

    # 请求 A（账号 1）：完整阶梯走完（未熔断 —— 单账号信号不足以触发）
    mgr_a = _make_manager()
    mgr_a.serving_account_id = "acc-1"
    for _ in range(4):
        assert _drive_403(mgr_a, exc) is True
    assert not health.is_degraded()

    # 请求 B（账号 2）：第 2 次 403 记入后跨账号关联成立 → 熔断。
    # 熔断可能发生在请求中途：本请求前两跳（免费重放）仍放行。
    mgr_b = _make_manager()
    mgr_b.serving_account_id = "acc-2"
    assert _drive_403(mgr_b, exc) is True
    assert _drive_403(mgr_b, exc) is True
    assert health.is_degraded()

    # 请求 C（账号 3）：熔断中第 3 跳直接短路，不再重签/切号
    mgr_c = _make_manager()
    mgr_c.serving_account_id = "acc-3"
    assert _drive_403(mgr_c, exc) is True
    assert _drive_403(mgr_c, exc) is True
    with pytest.raises(HTTPException) as ei2:
        _drive_403(mgr_c, exc)
    assert ei2.value.detail["type"] == "signer_degraded"
