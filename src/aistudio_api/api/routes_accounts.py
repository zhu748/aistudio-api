"""账号管理路由。"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path as FastApiPath,
    Request,
)
from pydantic import BaseModel

from aistudio_api.api.dependencies import (
    get_account_service,
    get_runtime_state,
)
from aistudio_api.infrastructure.account.account_store import AccountMeta
from aistudio_api.infrastructure.account.cookie_parser import (
    load_cookies_from_auth_file,
    normalize_benefit_tier,
    probe_account_benefit_tier,
    probe_account_email,
)
from aistudio_api.infrastructure.utils.common import mask_email
from aistudio_api.infrastructure.utils.redaction import redact_text

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState
    from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.accounts")

router = APIRouter(prefix="/accounts")


class AccountResponse(BaseModel):
    id: str
    name: str
    email: str | None
    created_at: str
    last_used: str | None
    auth_user: str = "0"
    cookie_id: str | None = None
    tier: str = "Free"
    enabled: bool = True
    group: str = "default"
    notes: str = ""
    proxy_url: str | None = None
    verification_required: bool = False


def to_account_response(
    account: AccountMeta, *, request: Request | None = None
) -> AccountResponse:
    """AccountMeta → 响应模型。

    传入 ``request`` 时按会话级别掩码代理内嵌凭据：API Key 回退会话
    只应看到脱敏地址（与 GET /config、/api/proxies 同一信任边界），
    管理密码会话保持明文便于面板复制。不传（测试/内部调用）保持原样。
    """
    cid = getattr(account, "cookie_id", None) or (
        f"cookie_{account.created_at[:16]}"
        if getattr(account, "created_at", None)
        else f"cookie_{account.id}"
    )
    proxy_url = getattr(account, "proxy_url", None)
    if proxy_url and request is not None:
        from aistudio_api.api.dependencies import is_admin_password_session

        if not is_admin_password_session(request):
            proxy_url = redact_text(proxy_url)
    return AccountResponse(
        id=account.id,
        name=account.name,
        email=account.email,
        created_at=account.created_at or "",
        last_used=account.last_used,
        auth_user=getattr(account, "auth_user", "0") or "0",
        cookie_id=cid,
        tier=getattr(account, "tier", "Free") or "Free",
        enabled=getattr(account, "enabled", True),
        group=getattr(account, "group", "default") or "default",
        notes=getattr(account, "notes", "") or "",
        proxy_url=proxy_url,
        verification_required=bool(getattr(account, "verification_required", False)),
    )


class UpdateAccountRequest(BaseModel):
    name: str


class UpdateGroupRequest(BaseModel):
    group: str


class UpdateNotesRequest(BaseModel):
    notes: str


class ToggleAccountRequest(BaseModel):
    enabled: bool | None = None


class PresetResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    action: str = "snapshot"
    account_ids: list[str] = []
    target_tiers: list[str] = []
    target_groups: list[str] = []
    is_system: bool = False
    created_at: str = ""
    updated_at: str = ""


class CreatePresetRequest(BaseModel):
    name: str
    description: str = ""
    action: str = "snapshot"
    account_ids: list[str] = []
    target_tiers: list[str] = []
    target_groups: list[str] = []


class SnapshotPresetRequest(BaseModel):
    name: str
    description: str = ""


class ApplyPresetResponse(BaseModel):
    ok: bool = True
    preset_id: str
    preset_name: str
    enabled_count: int
    disabled_count: int
    enabled_account_ids: list[str]
    disabled_account_ids: list[str]


@router.get("", response_model=list[AccountResponse])
@router.get("/", response_model=list[AccountResponse])
async def list_accounts(
    request: Request,
    account_service: AccountService = Depends(get_account_service),
) -> list[AccountResponse]:
    """列出所有账号。"""
    accounts = account_service.list_accounts()
    return [to_account_response(a, request=request) for a in accounts]


@router.get("/active", response_model=AccountResponse)
async def get_active_account(
    request: Request,
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """获取当前活跃账号。"""
    account = account_service.get_active_account()
    if account is None:
        raise HTTPException(status_code=404, detail="没有活跃账号")
    return to_account_response(account, request=request)


@router.post("/{account_id}/activate", response_model=AccountResponse)
async def activate_account(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> AccountResponse:
    """切换到指定账号。"""
    gateway_session = runtime_state.client.session if runtime_state.client else None

    if gateway_session is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    account = await account_service.activate_account(account_id, gateway_session)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在或切换失败")
    if runtime_state.rotator:
        runtime_state.rotator.add_account(account.id)
        runtime_state.rotator.clear_cooldown(account.id)
    # 目录缓存失效已收敛到 AccountService.activate_account（覆盖 rotator
    # 自动换号等全部切号入口），路由层不再重复调用。
    logger.info("激活账号: id=%s, name=%s", account.id, account.name)
    return to_account_response(account, request=request)


@router.post("/{account_id}/detect-email", response_model=AccountResponse)
async def detect_account_email(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """自动获取并更新指定账号的真实邮箱与订阅等级。"""
    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    target_auth = account_service.get_account_auth_path(account_id)
    email: str | None = None
    detected_tier: str | None = None

    if target_auth:
        try:
            cookie_dict = load_cookies_from_auth_file(target_auth)
            if cookie_dict:
                # 账号绑定独立代理时，探活请求走同一出口
                acc_proxy = getattr(acc, "proxy_url", None)
                email = await probe_account_email(
                    cookie_dict,
                    auth_user=acc.auth_user or "0",
                    proxy_url=acc_proxy,
                )
                http_tier = await probe_account_benefit_tier(
                    cookie_dict,
                    auth_user=acc.auth_user or "0",
                    proxy_url=acc_proxy,
                )
                if http_tier:
                    detected_tier = http_tier
        except Exception as e:
            logger.debug("探测账号 %s 邮箱/等级失败: %s", account_id, e)
    if email:
        account_service.update_account_email(account_id, email)
        logger.info("更新账号邮箱: id=%s, email=%s", account_id, mask_email(email))

    if detected_tier:
        # 层级变化影响目录拉取的 tier 头与付费门控，目录缓存失效已收敛到
        # AccountService.update_account_tier。
        account_service.update_account_tier(account_id, detected_tier)
        logger.info("更新账号订阅等级: id=%s, tier=%s", account_id, detected_tier)
    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(acc, request=request)


@router.post("/{account_id}/detect-tier", response_model=AccountResponse)
async def detect_account_tier(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """探测并更新指定账号的订阅权益等级 (Free / Pro / Ultra / Plus)。"""
    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    detected_tier: str | None = None
    target_auth = account_service.get_account_auth_path(account_id)
    if target_auth:
        try:
            cookie_dict = load_cookies_from_auth_file(target_auth)
            if cookie_dict:
                detected_tier = await probe_account_benefit_tier(
                    cookie_dict,
                    auth_user=acc.auth_user or "0",
                    proxy_url=getattr(acc, "proxy_url", None),
                )
        except Exception as e:
            logger.debug("探测账号 %s 订阅等级失败: %s", account_id, e)

    if detected_tier:
        # 目录缓存失效已收敛到 AccountService.update_account_tier。
        account_service.update_account_tier(account_id, detected_tier)
        logger.info("更新账号订阅等级: id=%s, tier=%s", account_id, detected_tier)

    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(acc, request=request)


class UpdateTierRequest(BaseModel):
    tier: str


# 与 cookie_parser._CANONICAL_TIERS 同键集：手动端点对未知输入显式 400，
# 而非像 normalize_benefit_tier 一样静默落 Free（拼写错误会把 Pro 号降级）。
_TIER_INPUTS: frozenset[str] = frozenset({"free", "plus", "pro", "ultra", "ultra_20x"})


@router.put("/{account_id}/tier", response_model=AccountResponse)
async def update_account_tier_manual(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateTierRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> AccountResponse:
    """手动设置账号的订阅权益等级 (Free / Plus / Pro / Ultra / Ultra_20X)。

    自动探测失败会落 Free（fail-soft），误标 Pro 号会被付费门控拦截；
    本端点是面板/API 侧唯一的纠正入口（上游 DeconstructedCube a78e54b
    同款端点，校验语义收紧为未知输入 400）。
    """
    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    raw_tier = req.tier.strip().lower()
    if raw_tier not in _TIER_INPUTS:
        raise HTTPException(
            status_code=400,
            detail="无效的订阅等级，允许值: Free / Plus / Pro / Ultra / Ultra_20X",
        )
    clean_tier = normalize_benefit_tier(req.tier)
    account_service.update_account_tier(account_id, clean_tier)
    # 活跃账号层级变化同步内存上下文，避免在途/后续请求仍带旧 tier 头
    gateway_session = runtime_state.client.session if runtime_state.client else None
    active = account_service.get_active_account()
    if (
        gateway_session is not None
        and active is not None
        and active.id == account_id
        and gateway_session.active_context is not None
    ):
        gateway_session.active_context.tier = clean_tier
    # 目录缓存失效已收敛到 AccountService.update_account_tier。
    logger.info("手动更新账号订阅等级: id=%s, tier=%s", account_id, clean_tier)
    updated = account_service.get_account(account_id)
    if not updated:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(updated, request=request)


@router.post("/{account_id}/rotate-cookies")
async def rotate_account_cookies(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """手动触发指定账号及其同源子账号的 Google Cookie 轮换续期。"""
    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    gateway_session = runtime_state.client.session if runtime_state.client else None
    res = await account_service.rotate_account_cookies(account_id, gateway_session)
    ok = bool(res.get("ok"))
    if ok and runtime_state.rotator:
        runtime_state.rotator.clear_cooldown(account_id)
    return {
        "ok": ok,
        "account_id": account_id,
        "status": res.get("status", 0),
        "updated": res.get("updated", []),
        "skipped": res.get("skipped"),
        "error": res.get("error"),
    }


@router.delete("/{account_id}")
async def delete_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, bool]:
    """删除账号。"""
    active_account = account_service.get_active_account()
    is_active = active_account is not None and active_account.id == account_id

    success = account_service.delete_account(account_id)
    if not success:
        raise HTTPException(status_code=404, detail="账号不存在")

    if runtime_state.rotator:
        runtime_state.rotator.remove_account(account_id)
    if is_active:
        remaining_accounts = account_service.list_accounts()
        new_active = remaining_accounts[0] if remaining_accounts else None
        gateway_session = runtime_state.client.session if runtime_state.client else None
        if new_active is not None:
            account_service.set_active_account(new_active.id)
            if gateway_session is not None:
                new_auth_path = account_service.get_account_auth_path(new_active.id)
                await gateway_session.switch_auth(
                    str(new_auth_path) if new_auth_path else None
                )
        else:
            if gateway_session is not None:
                await gateway_session.switch_auth(None)

    logger.info("已删除账号: id=%s", account_id)
    return {"ok": True}


@router.delete("/group/{cookie_id}")
async def delete_cookie_group(
    cookie_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, int]:
    """按 Cookie 组批量删除该 Cookie 下的所有子账号。"""
    active_account = account_service.get_active_account()
    active_id = active_account.id if active_account is not None else None
    active_deleted = False

    accounts = account_service.list_accounts()
    deleted_count = 0
    rotator = runtime_state.rotator
    for a in accounts:
        acc_cid = getattr(a, "cookie_id", None) or f"cookie_{a.created_at[:16]}"
        if acc_cid == cookie_id:
            if a.id == active_id:
                active_deleted = True
            if account_service.delete_account(a.id):
                deleted_count += 1
                if rotator:
                    rotator.remove_account(a.id)
                logger.info("已删除子账号: cookie_id=%s, id=%s", cookie_id, a.id)

    if active_deleted:
        remaining_accounts = account_service.list_accounts()
        new_active = remaining_accounts[0] if remaining_accounts else None
        gateway_session = runtime_state.client.session if runtime_state.client else None
        if new_active is not None:
            account_service.set_active_account(new_active.id)
            if gateway_session is not None:
                new_auth_path = account_service.get_account_auth_path(new_active.id)
                await gateway_session.switch_auth(
                    str(new_auth_path) if new_auth_path else None
                )
        else:
            if gateway_session is not None:
                await gateway_session.switch_auth(None)

    return {"deleted": deleted_count}


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateAccountRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号名称。"""
    account = account_service.update_account(account_id, req.name)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    logger.info("账号名称已更新: id=%s, name=%s", account.id, req.name)
    return to_account_response(account, request=request)


@router.post("/{account_id}/toggle", response_model=AccountResponse)
async def toggle_account(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: ToggleAccountRequest = Body(default_factory=ToggleAccountRequest),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """切换单个账号的使能状态 (Enabled/Disabled)。"""
    # 先取切换前的活跃账号：toggle 后再调 get_active_account()，其「禁用
    # 回退」会把 active_account_id 重写为首个启用账号，旧条件
    # active_acc.id == account_id 便永远命中不了（切换被跳过、会话停留在
    # 已禁用账号上继续发请求的分叉根因）。
    active_before = account_service.get_active_account()
    account = account_service.toggle_account_enabled(account_id, req.enabled)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    # 如果刚好关闭了当前活跃账号，顺延切到下一个可用账号
    if active_before and active_before.id == account_id and not account.enabled:
        from aistudio_api.application.account_orchestrator import try_switch_account

        await try_switch_account(failed_account_id=account_id)

    logger.info("账号状态已切换: id=%s, enabled=%s", account.id, account.enabled)
    return to_account_response(account, request=request)


@router.put("/{account_id}/group", response_model=AccountResponse)
async def update_account_group(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateGroupRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号所属业务分组。"""
    account = account_service.update_account_group(account_id, req.group)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    logger.info("账号分组已更新: id=%s, group=%s", account.id, account.group)
    return to_account_response(account, request=request)


@router.put("/{account_id}/notes", response_model=AccountResponse)
async def update_account_notes(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateNotesRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号备注。"""
    account = account_service.update_account_notes(account_id, req.notes)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(account, request=request)


class UpdateProxyRequest(BaseModel):
    proxy_url: str | None = None


@router.put("/{account_id}/proxy", response_model=AccountResponse)
async def update_account_proxy(
    request: Request,
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateProxyRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号的独立出口代理（留空表示回退全局 AISTUDIO_PROXY）。

    支持 http:// / https:// / socks5:// 等完整地址，或 host:port 简写。
    """
    from aistudio_api.infrastructure.account.cookie_parser import normalize_proxy_url

    raw_proxy = (req.proxy_url or "").strip()
    if raw_proxy and normalize_proxy_url(raw_proxy) is None:
        raise HTTPException(
            status_code=400,
            detail="代理地址无效：需为 http(s):// / socks5:// 完整地址或 host:port 形式",
        )
    account_service.update_account_proxy(account_id, raw_proxy or None)
    account = account_service.get_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    from aistudio_api.api.state import runtime_state
    from aistudio_api.infrastructure.utils.redaction import redact_text

    client = runtime_state.client
    if client and client.session.active_context:
        active_file = client.session.active_context.auth_file
        target_file = account_service.get_account_auth_path(account_id)
        if target_file and active_file == str(target_file):
            await client.session.transition_to_account(active_file, force_refresh=True)
    # 旧代理的客户端池与 turn/sstp 桥接子进程退役：账号级代理变更不走
    # set_default_proxy（全局代理未变），以「全局代理 + 全部账号代理」为
    # 保留集回收其余池（延迟关闭，在途请求安全排空）。
    if client is not None:
        from aistudio_api.config import settings

        keep_proxies: set[str | None] = {settings.proxy_url}
        for acc in account_service.list_accounts():
            keep_proxies.add(getattr(acc, "proxy_url", None))
        with contextlib.suppress(Exception):
            await client.session.retire_unused_clients(keep_proxies)
    logger.info(
        "更新账号代理: id=%s, proxy=%s",
        account_id,
        redact_text(account.proxy_url or "(全局)"),
    )
    return to_account_response(account, request=request)


@router.get("/groups/list", response_model=list[str])
async def list_groups(
    account_service: AccountService = Depends(get_account_service),
) -> list[str]:
    """列出当前存在的所有分组名称。"""
    return account_service.list_groups()


@router.post("/groups/{group_name}/toggle")
async def batch_toggle_group(
    group_name: str,
    enabled: bool = Body(..., embed=True),
    account_service: AccountService = Depends(get_account_service),
) -> dict[str, object]:
    """按分组批量启用或停用账号。"""
    updated = account_service.batch_toggle_group(group_name, enabled)
    active_acc = account_service.get_active_account()
    if active_acc and active_acc.group == group_name and not enabled:
        from aistudio_api.application.account_orchestrator import try_switch_account

        await try_switch_account(failed_account_id=active_acc.id)
    return {"ok": True, "count": len(updated), "group": group_name, "enabled": enabled}


# ─── 预设方案 (Presets) REST 路由 ───
@router.get("/presets/list", response_model=list[PresetResponse])
async def list_presets(
    account_service: AccountService = Depends(get_account_service),
) -> list[PresetResponse]:
    """获取所有可用预设方案列表。"""
    presets = account_service.list_presets()
    return [PresetResponse.model_validate(p.to_dict()) for p in presets]


@router.post("/presets", response_model=PresetResponse)
async def create_preset(
    req: CreatePresetRequest,
    account_service: AccountService = Depends(get_account_service),
) -> PresetResponse:
    """创建自定义预设方案。"""
    import secrets

    from aistudio_api.infrastructure.account.account_store import AccountPreset

    pid = f"preset_{secrets.token_hex(4)}"
    preset = AccountPreset(
        id=pid,
        name=req.name.strip() or "自定义方案",
        description=req.description.strip(),
        action=req.action,
        account_ids=req.account_ids,
        target_tiers=req.target_tiers,
        target_groups=req.target_groups,
        is_system=False,
    )
    saved = account_service.save_preset(preset)
    return PresetResponse.model_validate(saved.to_dict())


@router.post("/presets/snapshot", response_model=PresetResponse)
async def create_snapshot_preset(
    req: SnapshotPresetRequest,
    account_service: AccountService = Depends(get_account_service),
) -> PresetResponse:
    """将当前所有启用账号一键快照保存为新预设方案。"""
    saved = account_service.create_snapshot_preset(req.name, req.description)
    logger.info("已创建预设快照: name=%s, id=%s", saved.name, saved.id)
    return PresetResponse.model_validate(saved.to_dict())


@router.delete("/presets/{preset_id}")
async def delete_preset(
    preset_id: str,
    account_service: AccountService = Depends(get_account_service),
) -> dict[str, bool]:
    """删除自定义预设方案。"""
    try:
        ok = account_service.delete_preset(preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="预设方案不存在")
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/presets/{preset_id}/apply", response_model=ApplyPresetResponse)
async def apply_preset(
    preset_id: str,
    account_service: AccountService = Depends(get_account_service),
) -> ApplyPresetResponse:
    """一键应用预设方案，原子切换账号使能状态。"""
    try:
        preset, en_ids, dis_ids = account_service.apply_preset(preset_id)
        # 检查当前活跃账号是否有效，若失效则顺延切换
        active_acc = account_service.get_active_account()
        from aistudio_api.application.account_orchestrator import try_switch_account

        if not active_acc or not active_acc.enabled or active_acc.id in dis_ids:
            await try_switch_account(
                failed_account_id=active_acc.id if active_acc else None
            )
        else:
            await try_switch_account()

        logger.info(
            "已应用预设: name=%s, 启用=%d, 停用=%d",
            preset.name,
            len(en_ids),
            len(dis_ids),
        )
        return ApplyPresetResponse(
            ok=True,
            preset_id=preset.id,
            preset_name=preset.name,
            enabled_count=len(en_ids),
            disabled_count=len(dis_ids),
            enabled_account_ids=en_ids,
            disabled_account_ids=dis_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
