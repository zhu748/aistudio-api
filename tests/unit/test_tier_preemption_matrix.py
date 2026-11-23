"""真实 AccountRotator 的 tier 抢占矩阵回归（原先 test_tier_preemption 全 mock）。

覆盖 _get_eligible_candidates 的 required_tiers 硬过滤与交叉场景：
各 tier 账号 × 请求 pro_required/ultra_required 模型 × 冷却/禁用状态。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore

TIERS = ["Free", "Plus", "Pro", "Ultra", "Ultra_20X"]


def _make_store() -> tuple[MagicMock, dict[str, AccountMeta]]:
    accounts: dict[str, AccountMeta] = {}
    for tier in TIERS:
        acc_id = f"acc_{tier.lower()}"
        accounts[acc_id] = AccountMeta(
            id=acc_id,
            name=f"Account {tier}",
            email=f"{tier.lower()}@example.com",
            created_at="2026-01-01",
            tier=tier,
        )
    store = MagicMock(spec=AccountStore)
    store.list_accounts.return_value = list(accounts.values())
    return store, accounts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("required_tiers", "expected_ids"),
    [
        # Pro 要求：Free/Plus 被硬过滤，Pro/Ultra/Ultra_20X 均可入选
        (("Pro",), {"acc_pro", "acc_ultra", "acc_ultra_20x"}),
        # Ultra 要求：仅 Ultra 系
        (("Ultra",), {"acc_ultra", "acc_ultra_20x"}),
        # Ultra_20X 显式要求：仅自身
        (("Ultra_20X",), {"acc_ultra_20x"}),
        # 多 tier 并集
        (("Free", "Pro"), {"acc_free", "acc_pro", "acc_ultra", "acc_ultra_20x"}),
    ],
)
async def test_required_tiers_hard_filters_candidates(required_tiers, expected_ids):
    store, _ = _make_store()
    rotator = AccountRotator(account_store=store)

    picked = await rotator.get_next_account(
        model="gemini-3.7-pro",
        required_tiers=required_tiers,
    )
    assert picked is not None
    # 反复抽取（模拟失败转移）：所有可选账号都应来自期望集合，且不重复选取
    seen: set[str] = {picked.id}
    assert seen <= expected_ids
    for _ in range(len(TIERS)):
        nxt = await rotator.get_next_account(
            model="gemini-3.7-pro",
            required_tiers=required_tiers,
            failed_account_id=next(iter(seen)),
        )
        if nxt is None:
            break
        seen.add(nxt.id)
        assert nxt.id in expected_ids
    # 期望集合中至少能选中一个
    assert seen & expected_ids


@pytest.mark.asyncio
async def test_required_tiers_excludes_cooled_down_account():
    store, _ = _make_store()
    rotator = AccountRotator(account_store=store)

    # Pro 账号在目标模型上 429 冷却；Pro/Ultra 均可入选时应避开 acc_pro
    rotator.record_rate_limited("acc_pro", model="gemini-3.7-pro")

    picked = await rotator.get_next_account(
        model="gemini-3.7-pro", required_tiers=("Pro", "Ultra")
    )
    assert picked is not None
    assert picked.id != "acc_pro"

    # 唯一可选账号冷却时快速失败（None 而非异常）
    picked = await rotator.get_next_account(
        model="gemini-3.7-pro", required_tiers=("Pro",)
    )
    assert picked is None


@pytest.mark.asyncio
async def test_required_tiers_empty_set_returns_any_available():
    store, _ = _make_store()
    rotator = AccountRotator(account_store=store)

    picked = await rotator.get_next_account(model="gemini-3.7-flash")
    assert picked is not None
    assert picked.id in {f"acc_{t.lower()}" for t in TIERS}


@pytest.mark.asyncio
async def test_required_tiers_none_when_no_account_matches():
    store, _ = _make_store()
    rotator = AccountRotator(account_store=store)

    picked = await rotator.get_next_account(
        model="gemini-3.7-pro", required_tiers=("Ultra_20X", "Pro")
    )
    # Ultra_20X 与 Pro 都存在 → 应选中其一
    assert picked is not None
    assert picked.id in {"acc_pro", "acc_ultra_20x"}

    # 全部禁用后 → None（快速失败而非抛异常）
    for acc in store.list_accounts.return_value:
        acc.enabled = False
    picked = await rotator.get_next_account(
        model="gemini-3.7-pro", required_tiers=("Pro",)
    )
    assert picked is None
