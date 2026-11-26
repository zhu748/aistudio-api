"""WAA 签名器运维路由（自 routes_system.py 拆分）。

多节点管理（列表/切换/新增/删除/探活测试）与远程签名服务支持已移除：
本地原生引擎是唯一签名方式，无面板切换面。
"""

from __future__ import annotations

from fastapi import APIRouter

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.signer")

public_router = APIRouter()
protected_router = APIRouter()


@protected_router.get("/config/signer-health")
async def get_signer_health_state() -> dict[str, object]:
    """WAA 签名器健康熔断状态（语义与判据见 waa/health.py）。

    面板/运维排障用途：当 GenerateContent 集中 403 而 CountTokens 等
    轻量端点正常时，优先查看本端点 —— ``degraded=true`` 意味着
    「新铸 proof 被上游拒绝」已跨账号关联，根因大概率在本地签名引擎，
    而非账号。
    """
    from aistudio_api.infrastructure.waa.health import get_signer_health

    return get_signer_health().state_summary()
