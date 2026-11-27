"""Account failover and concurrency orchestration."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from aistudio_api.api.state import runtime_state
from aistudio_api.infrastructure.account.account_store import AccountMeta
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("orchestrator")

# 账号 ID 形态（与 routes_accounts 的路径参数 pattern 同字符集）
_ACCOUNT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.@]+$")


def get_max_retries() -> int:
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_rotator_settings,
    )

    value = get_configured_rotator_settings().get("max_retries", 5)
    try:
        return max(1, min(20, int(str(value))))
    except ValueError:
        return 5


_switch_lock: asyncio.Lock | None = None
_switch_lock_loop: asyncio.AbstractEventLoop | None = None
_last_switch_time: float = 0.0


def _get_switch_lock() -> asyncio.Lock:
    global _switch_lock, _switch_lock_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _switch_lock is None or _switch_lock_loop is not loop:
        _switch_lock = asyncio.Lock()
        _switch_lock_loop = loop
    return _switch_lock


def _session_bound_account_id(client: object) -> str | None:
    """读取网关会话当前绑定的账号 ID（上下文 auth_file 的父目录名）。

    无绑定（纯 Cookie 上下文 / 测试夹具 / 会话未就绪）返回 None。用于
    「注册表活跃账号 vs 会话实际服务账号」的分叉判定：只改注册表不切
    会话的路径（如 get_active_account 的禁用回退重写 active_account_id）
    会让二者分叉，此时凭注册表判定「已切换完成」是错的。

    返回值须通过账号 ID 形态校验（与路由层 pattern 同字符集）：mock 会话
    的 auth_file 属性可能是任意对象，str() 出来的目录名不属于合法账号 ID，
    一律按「无法判定」处理，不参与漂移判定。
    """
    session = getattr(client, "session", None) if client is not None else None
    ctx = getattr(session, "active_context", None) if session is not None else None
    auth_file = getattr(ctx, "auth_file", None) if ctx is not None else None
    if not isinstance(auth_file, (str, Path)) or not auth_file:
        return None
    bound_id = Path(str(auth_file)).parent.name
    if not _ACCOUNT_ID_PATTERN.match(bound_id):
        return None
    return bound_id


async def try_switch_account(
    model: str | None = None,
    failed_account_id: str | None = None,
    required_tiers: tuple[str, ...] | list[str] | None = None,
    *,
    is_session_expired: bool = False,
    is_verification_required: bool = False,
) -> bool:
    """尝试切换到下一个对目标 model 可用的账号。防止并发级联切号。"""
    global _last_switch_time
    entry_time = time.time()
    forbid_in_place = is_session_expired or is_verification_required

    if model and not required_tiers:
        from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

        tiers = model_discovery.get_required_tiers(model)
        if tiers:
            required_tiers = tuple(tiers)

    # 乐观快速预检：若无需记录永久失效/待验证禁用/短时隔离，且当前活跃账号已被并发请求切换至健康状态，直接免锁复用
    if failed_account_id and not forbid_in_place:
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        client = runtime_state.client
        if rotator is not None and account_service is not None:
            cur_active = account_service.get_active_account()
            if (
                cur_active
                and (
                    cur_active.id != failed_account_id
                    or _last_switch_time >= entry_time
                )
                and (not required_tiers or cur_active.tier in required_tiers)
                and rotator.is_account_available(cur_active.id, model=model)
            ):
                # 会话一致性校验：注册表说「已切换」不代表会话真的切了——
                # 禁用回退等只改注册表的路径会造成分叉，此处凭注册表放行
                # 会让请求继续用旧（可能已禁用）账号发出。
                bound_id = _session_bound_account_id(client)
                if bound_id is None or bound_id == cur_active.id:
                    return True

    async with _get_switch_lock():
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        client = runtime_state.client

        if (
            rotator is None
            or account_service is None
            or client is None
            or client.session is None
        ):
            return False

        current_active = account_service.get_active_account()
        current_id = current_active.id if current_active else None

        # 遇到 Google 账号待验证限制时，立刻持久化禁用并打标待验证（幂等防并发重复写盘/记罚）
        if failed_account_id and is_verification_required:
            failed_acc = (
                account_service.get_account(failed_account_id)
                if hasattr(account_service, "get_account")
                else None
            )
            already_marked = bool(
                failed_acc is not None
                and not failed_acc.enabled
                and getattr(failed_acc, "verification_required", False)
            )
            if not already_marked:
                if hasattr(account_service, "mark_account_verification_required"):
                    account_service.mark_account_verification_required(
                        failed_account_id, required=True, disable=True
                    )
                elif hasattr(account_service, "toggle_account_enabled"):
                    account_service.toggle_account_enabled(failed_account_id, False)
                rotator.record_auth_error(failed_account_id, model=model)
                logger.warning(
                    "账号 %s 需要完成 Google 验证，已自动禁用并标记为待验证: model=%s",
                    failed_account_id,
                    model,
                )

        # 会话漂移校正：注册表活跃账号可能被「只改注册表不切会话」的路径
        # 改写（get_active_account 的禁用回退重写 active_account_id、
        # apply_preset 清空活跃标记后的回退等），此时下方双重检查只看注册表
        # 就会误判「已切换完成」而让请求继续用旧账号（甚至已禁用账号）发出。
        # 先让会话追上注册表，再走既有的复用/切换判定。
        if (
            current_active is not None
            and (not failed_account_id or current_active.id != failed_account_id)
            and (not required_tiers or current_active.tier in required_tiers)
        ):
            bound_id = _session_bound_account_id(client)
            if bound_id is not None and bound_id != current_active.id:
                corrected = await account_service.activate_account(
                    current_active.id,
                    client.session,
                )
                if corrected is not None:
                    _last_switch_time = time.time()
                    logger.info(
                        "会话漂移校正: session=%s → active=%s, model=%s",
                        bound_id,
                        current_active.id,
                        model,
                    )
                    return True
                # 校正失败（账号目录缺失等）：落入既有切换链路兜底

        # 双重检查 1：如果已有并发协程在 entry_time 之后完成切号或在位自愈刷新，且当前账号对该模型可用，则直接复用
        if (
            _last_switch_time >= entry_time
            and current_id
            and (not forbid_in_place or current_id != failed_account_id)
            and (
                not required_tiers
                or (current_active and current_active.tier in required_tiers)
            )
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "复用并发已切换账号: account=%s, model=%s",
                current_id,
                model,
            )
            return True

        # 遇到登录态失效时，永久禁用该账号并拒绝原地重试
        if failed_account_id and is_session_expired:
            rotator.record_session_expired(failed_account_id, model=model)

        # 双重检查 2：如果已经由并发协程切换到了新账号，且新账号对当前 model 可用，则直接复用
        if (
            failed_account_id
            and current_id
            and current_id != failed_account_id
            and (
                not required_tiers
                or (current_active and current_active.tier in required_tiers)
            )
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "复用并发已切换账号: account=%s, model=%s",
                current_id,
                model,
            )
            return True

        next_account = await rotator.get_next_account(
            model=model,
            current_account_id=current_id,
            failed_account_id=failed_account_id,
            required_tiers=required_tiers,
            is_session_expired=forbid_in_place,
        )
        if next_account is None:
            if is_verification_required:
                logger.error(
                    "账号 %s 待验证已禁用且无备用账号可用: model=%s",
                    failed_account_id,
                    model,
                )
            elif is_session_expired:
                logger.error(
                    "账号 %s 登录态失效且无备用账号可用",
                    failed_account_id,
                )
            else:
                logger.warning(
                    "无可用备用账号: model=%s, failed=%s",
                    model,
                    failed_account_id,
                )
            return False

        if current_id is None or next_account.id != current_id:
            result = await account_service.activate_account(
                next_account.id,
                client.session,
            )
            if result is not None:
                rotator.clear_cooldown(next_account.id, model=model)
                _last_switch_time = time.time()
                logger.info(
                    "切换账号: %s -> %s, model=%s",
                    current_id,
                    next_account.id,
                    model,
                )
            return result is not None
        # 登录态失效或待验证禁用严禁在原账号原地重试
        if forbid_in_place and (
            failed_account_id == current_id or next_account.id == failed_account_id
        ):
            logger.error(
                "账号 %s %s，跳过原地重试",
                failed_account_id,
                "需要验证已禁用" if is_verification_required else "登录态已失效",
            )
            return False

        # 单账号模式或所有其他账号均不可用时（非登录态失效，如偶发 403 鉴权波动），如果指定了 failed_account_id，强制刷新当前会话
        if failed_account_id and failed_account_id == current_id:
            logger.info(
                "无其他备用账号，重置当前会话: account=%s",
                current_id,
            )
            result = await account_service.activate_account(
                current_id,
                client.session,
            )
            if result is not None:
                rotator.clear_cooldown(current_id, model=model)
                _last_switch_time = time.time()
            return result is not None
        return bool(
            current_id
            and (
                not required_tiers
                or (current_active and current_active.tier in required_tiers)
            )
            and rotator.is_account_available(current_id, model=model)
        )


async def ensure_active_account(attempt: int, model: str | None = None) -> None:
    """确保在初次尝试时存在活跃账号，且若请求付费模型而当前账号权益不足时主动抢切。"""
    if attempt != 0:
        return
    account_svc = runtime_state.account_service
    rotator = runtime_state.rotator
    current = account_svc.get_active_account() if account_svc else None

    # 核心拦截：检测目标模型是否为 Paid 模型且当前账号层级不满足要求
    if model:
        from fastapi import HTTPException

        from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

        if model_discovery.is_paid_model(model):
            req_tier = model_discovery.get_required_tier(model)
            req_tiers = model_discovery.get_required_tiers(model)
            cur_tier = (current.tier if current else None) or "Free"
            allowed_tiers = (
                set(req_tiers)
                if req_tiers
                else (
                    {"Pro", "Ultra", "Ultra_20X"}
                    if req_tier == "Pro"
                    else {"Ultra", "Ultra_20X"}
                )
            )
            if not current or cur_tier not in allowed_tiers:
                logger.info(
                    "模型 %s 要求 %s 订阅，当前账号 %s (%s)，切换至对应等级账号",
                    model,
                    req_tier,
                    current.id if current else "none",
                    cur_tier,
                )
                switched = await try_switch_account(
                    model=model,
                    required_tiers=tuple(req_tiers)
                    if req_tiers
                    else tuple(allowed_tiers),
                )
                if not switched:
                    logger.warning(
                        "模型 %s 要求 %s 订阅，当前无可用对应等级账号",
                        model,
                        req_tier,
                    )
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "message": f"模型 '{model}' 需要 Google AI Studio {req_tier} 订阅，当前账号池中无就绪的付费账号。",
                            "type": "permission_denied",
                        },
                    )
                return

    if not current:
        await try_switch_account(model=model)
    elif (
        rotator and model and not rotator.is_account_available(current.id, model=model)
    ):
        await try_switch_account(model=model, failed_account_id=current.id)


def record_rotator_event(
    event: str,
    model: str | None = None,
    usage: dict[str, object] | None = None,
    account_id: str | None = None,
) -> None:
    """记录调度器事件（成功、限流、错误、鉴权异常）。

    记账串号修复（3.9）：account_id 为实际服务本次请求的账号；
    旧实现在请求收尾时重新读取全局活跃账号，并发场景下若其他请求
    已触发切号，本次的成功/429/403 会被记到切换后的账号头上，
    健康度与冷却状态全部失真。account_id 缺省时回退全局活跃账号
    （兼容存量调用点）。
    """
    rotator = runtime_state.rotator
    account_service = runtime_state.account_service
    account: AccountMeta | None = None
    if account_id and account_service and hasattr(account_service, "get_account"):
        account = account_service.get_account(account_id)
    if account_id:
        # The original account may have been deleted during an in-flight
        # request. Never attribute its result to the next active account.
        if account is None:
            return
    elif account_service:
        account = account_service.get_active_account()
    if not rotator or account is None:
        return
    if event == "success":
        rotator.record_success(account.id, model=model, usage=usage)
    elif event == "rate_limited":
        rotator.record_rate_limited(account.id, model=model)
    elif event == "auth_error":
        rotator.record_auth_error(account.id, model=model)
    elif event == "error":
        rotator.record_error(account.id, model=model)


__all__ = [
    "ensure_active_account",
    "get_max_retries",
    "record_rotator_event",
    "try_switch_account",
]
