"""System and metadata routes.

拆分说明：原文件中的配置/签名服务器/日志/API Key 端点已分别迁至
``routes_config.py``、``routes_signer.py``、``routes_logs.py``（均在本包
无 prefix 的 public/protected router 上，由 app.py 以相同方式挂载）。
本文件仅保留健康探活、统计聚合与调度配额管理端点。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.response_models import (
    HealthResponse,
    ModelStatsResponse,
    StatsResponse,
    StatsTotalsResponse,
)
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState

logger = get_logger("routes.system")

public_router = APIRouter()
protected_router = APIRouter()


def health_response() -> HealthResponse:
    return HealthResponse(status="ok")


def stats_response() -> StatsResponse:
    from aistudio_api.api.response_models import AccountBreakdownItem
    from aistudio_api.api.state import runtime_state
    from aistudio_api.infrastructure.gateway.model_defaults import resolve_tier_quota

    stats = dict(runtime_state.model_stats)
    rotator = runtime_state.rotator
    account_svc = runtime_state.account_service
    accounts_list = account_svc.list_accounts() if account_svc else []

    totals = StatsTotalsResponse(
        requests=sum(s.requests for s in stats.values()),
        success=sum(s.success for s in stats.values()),
        rate_limited=sum(s.rate_limited for s in stats.values()),
        errors=sum(s.errors for s in stats.values()),
        prompt_tokens=sum(s.prompt_tokens for s in stats.values()),
        completion_tokens=sum(s.completion_tokens for s in stats.values()),
        reasoning_tokens=sum(s.reasoning_tokens for s in stats.values()),
        cached_tokens=sum(s.cached_tokens for s in stats.values()),
        total_tokens=sum(s.total_tokens for s in stats.values()),
    )

    models = {}
    for name, s in stats.items():
        clean_m = name.removeprefix("models/").lower()
        breakdown: list[AccountBreakdownItem] = []

        if rotator:
            from aistudio_api.application.account_rotator import get_pacific_date_key

            current_la = get_pacific_date_key()
            for acc in accounts_list:
                st = rotator.get_account_stats(acc.id)
                if st:
                    st._check_date_reset(current_la)
                    if clean_m in st.daily_models:
                        m_daily = st.daily_models[clean_m]
                        quota = resolve_tier_quota(acc.tier, clean_m)
                        used_pct = round((m_daily.requests / max(1, quota)) * 100, 1)
                        cd_rem = (
                            max(0, int(m_daily.cooldown_until - time.time()))
                            if m_daily.cooldown_until > time.time()
                            else 0
                        )
                        breakdown.append(
                            AccountBreakdownItem(
                                account_id=acc.id,
                                name=acc.name,
                                auth_user=acc.auth_user,
                                email=acc.email,
                                tier=acc.tier,
                                group=acc.group,
                                enabled=acc.enabled,
                                today_requests=m_daily.requests,
                                today_success=m_daily.success,
                                today_rate_limited=m_daily.rate_limited_total,
                                today_tokens=m_daily.total_tokens,
                                today_reasoning_tokens=m_daily.reasoning_tokens,
                                configured_quota=quota,
                                quota_used_percent=used_pct,
                                is_available=acc.enabled
                                and cd_rem == 0
                                and not st.session_expired,
                                cooldown_remaining=cd_rem,
                            )
                        )

        models[name] = ModelStatsResponse(
            requests=s.requests,
            success=s.success,
            rate_limited=s.rate_limited,
            errors=s.errors,
            prompt_tokens=s.prompt_tokens,
            completion_tokens=s.completion_tokens,
            reasoning_tokens=s.reasoning_tokens,
            cached_tokens=s.cached_tokens,
            total_tokens=s.total_tokens,
            finish_reasons=dict(s.finish_reasons),
            last_used=s.last_used,
            account_breakdown=breakdown,
        )
    return StatsResponse(models=models, totals=totals)


@public_router.get("/health", response_model=HealthResponse)
async def health():
    return health_response()


# /healthz：与 /health 等价的别名端点，遵循 Kubernetes / PaaS 探活命名惯例
# （Render / Fly.io / k8s 用户习惯性配置 /healthz）。无鉴权、不触上游、
# 不依赖账号，任何启动状态下都应返回 200。
@public_router.get("/healthz", response_model=HealthResponse, include_in_schema=False)
async def healthz():
    return health_response()


@protected_router.get("/stats", response_model=StatsResponse)
async def stats():
    # O(模型数 × 账号数) 的同步聚合（P3）：50 账号 × 10 模型时达数十毫秒，
    # 下放线程池避免阻塞事件循环拖慢并发流式请求。
    return await asyncio.to_thread(stats_response)


# ========== 调度与配额管理 API ==========


class ClearCooldownRequest(BaseModel):
    account_id: str | None = None
    model: str | None = None


@protected_router.get("/rotation")
async def get_rotation_status(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """获取账号黏性调度与配额状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        return {
            "enabled": False,
            "mode": "sticky",
            "message": "调度器未初始化",
            "accounts": {},
        }

    return {
        "enabled": True,
        "mode": "sticky",
        "accounts": rotator.get_all_stats(),
    }


@protected_router.post("/rotation/clear-cooldown")
async def clear_cooldown(
    req: ClearCooldownRequest,
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """清除指定账号或全局的 429 锁定状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    if req.account_id:
        rotator.clear_cooldown(req.account_id, model=req.model)
    else:
        rotator.clear_all_cooldowns()
    logger.info(
        "已清除冷却状态: 账号=%s, 模型=%s",
        req.account_id or "全部",
        req.model or "全部",
    )

    return {"ok": True, "accounts": rotator.get_all_stats()}


@protected_router.post("/rotation/cleanup-ghosts")
async def cleanup_ghosts(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """主动对齐并彻底清除已删除账号在调度器中残留的幽灵状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    cleaned_ids = rotator.cleanup_ghost_accounts()
    return {
        "ok": True,
        "cleaned_count": len(cleaned_ids),
        "cleaned_ids": cleaned_ids,
        "accounts": rotator.get_all_stats(),
    }


@protected_router.get("/rotation/accounts")
async def get_rotation_accounts(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, dict[str, object]]:
    """获取所有账号的调度与配额统计。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    return rotator.get_all_stats()


@protected_router.post("/rotation/next")
async def force_next_account(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """强制切换到下一个可用账号。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    next_account = await rotator.get_next_account()
    if next_account is None:
        raise HTTPException(404, detail="没有可用的账号")

    account_service = runtime_state.account_service
    client = runtime_state.client

    if account_service is None or client is None or client.session is None:
        raise HTTPException(503, detail="服务未就绪")

    result = await account_service.activate_account(
        next_account.id,
        client.session,
    )

    if result is None:
        raise HTTPException(500, detail="切换失败")

    logger.info("手动切换至下一账号: id=%s, name=%s", result.id, result.name)
    return {
        "ok": True,
        "account": {
            "id": result.id,
            "name": result.name,
            "email": result.email,
        },
    }
