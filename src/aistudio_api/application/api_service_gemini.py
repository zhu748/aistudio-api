"""Gemini-compatible application service handlers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from aistudio_api.api.keepalive import STREAM_HEADERS, HeartbeatStreamingResponse
from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
    GeminiPartResponse,
)
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import (
    GeminiCountTokensRequest,
    GeminiGenerateContentRequest,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    ensure_active_account,
    get_max_retries,
    record_rotator_event,
    try_switch_account,
)
from aistudio_api.application.anti_truncation import (
    AntiTruncationContext,
    _close_stream_quietly,
    prepare_anti_truncation,
    resolve_model_variant,
    restore_anti_truncation_output,
    restore_anti_truncation_stream,
)
from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    normalize_gemini_request,
)
from aistudio_api.domain.errors import (
    AistudioError,
    AuthError,
    RequestError,
    SessionExpiredError,
    TransportError,
    UsageLimitExceeded,
    is_retryable_upstream_error,
    is_transient_rpc_error,
    parse_upstream_grpc_error,
)
from aistudio_api.domain.models import ModelOutput
from aistudio_api.domain.tool_signatures import (
    remember_function_signature as _remember_function_signature,
)
from aistudio_api.infrastructure.gateway.client import (
    AIStudioClient,
    PreparedRequest,
)
from aistudio_api.infrastructure.gateway.wire_spec import (
    WIRE_FINISH_REASON_CONTINUING,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.request_context import (
    current_request,
    record_attempt_error,
    record_output_timing,
    record_requested_parameters,
    record_result,
)
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.waa.health import get_signer_health

logger = get_logger("api.gemini")

# 日志注入防护（L6）：model_path 来自 URL，未过滤时控制字符可注入伪造日志行
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_model_path(model_path: str) -> str:
    """清洗路径参数中的控制字符，避免日志伪造（CRLF 注入）。"""
    return _CONTROL_CHARS_RE.sub("", model_path)


AUTH_403_MAX_RETRIES = (
    5  # 403 阶梯重试机制：前 2 次重放，后 2 次重新签名，最后 1 次切号
)


class Auth403RetryManager:
    """403 阶梯重试管理器：前 2 次重放，后 2 次重新签名，最后 1 次切号。

    403「The caller does not have permission」的四种已知语义
    （2026-11 抓包 + SOCKS5 代理实测，详见 docs/CAPTURE_ALIGNMENT_2026-11.md
    §8.3 与 §9）：
    1. proof 失效/摘要失配 —— 重签有效（阶梯第 3-4 跳）；
    2. 远程签名服务单次使用/质量抖动 —— 重签有效；
    3. 账号级生成限流/风控拦截 —— 会话与鉴权健康（CountTokens/
       CheckUserStatus 同期 200），但原版有效 proof 重放也 403；此时
       重签无效，阶梯最终切号是唯一出路。中间两次重签对该场景是
       无效消耗，维持现阶梯是为覆盖 1/2 两类可自愈场景。
    4. 签名器 attestation 质量降级（历史远程服务挑战过期实测案例）——
       mint 成功但 proof 被上游系统性拒绝，且跨账号全量发生（签名器
       是公共依赖）。此时重签与切号均无效且无消耗账号池价值，由
       SignerHealthTracker 熔断短路（见 waa/health.py 判据与证据链）。
    """

    def __init__(self, max_retries: int = AUTH_403_MAX_RETRIES) -> None:
        self.count: int = 0
        self.max_retries: int = max_retries
        self.current_prepared: PreparedRequest | None = None
        # 本次请求实际服务的账号（3.9 记账串号修复）：由请求循环在每次
        # 尝试发送前刷新，403 记账用它而非收尾时的全局活跃账号。
        self.serving_account_id: str | None = None

    async def handle_403(
        self,
        exc: Exception,
        *,
        model: str,
        client: AIStudioClient,
        has_yielded_data: bool = False,
    ) -> bool:
        record_attempt_error(403, str(exc))
        signer_health = get_signer_health()
        if has_yielded_data:
            # 已产出数据后途断的 403：能产出即证明当次 proof 通过了
            # attestation，记入「接受」信号参与签名器健康判定。
            signer_health.record_proof_accepted(self.serving_account_id)
            raise HTTPException(
                403, detail={"message": str(exc), "type": "auth_error"}
            ) from exc

        self.count += 1
        # 记「签名器产出的 proof 被拒」事件；跨账号关联达阈值时触发熔断
        # （单账号永不触发，语义 1/2/3 照旧走完整阶梯）。
        signer_health.record_proof_rejected(self.serving_account_id)

        # 每次 403 重试都记账会让单个请求放大成 5 次惩罚（auth_errors*200 权重），
        # 同一请求首个 403 已足以让账号降权，后续重试只做恢复动作、不重复记罚。
        # 语义 4（签名器降级）例外：账号无辜，降级期间不记罚色。
        if self.count == 1 and not signer_health.is_degraded():
            record_rotator_event(
                "auth_error", model=model, account_id=self.serving_account_id
            )

        # 语义 4 熔断短路：重签（3-4 跳）与切号（第 5 跳）对签名器级拒绝
        # 均无效 —— 远程 VM 产出的新 proof 同样会被拒，切号只会无差别
        # 烧穿账号池。保留前两跳免费重放（万一为可自愈语义），代价最高
        # 的中后段直接终止并给出可定位的错误类型。
        if signer_health.is_degraded() and self.count >= 3:
            logger.error(
                "HTTP 403 签名器熔断短路 (count=%d): model=%s, 冷却剩余 %.0fs",
                self.count,
                model,
                signer_health.cooldown_remaining(),
            )
            raise HTTPException(
                403,
                detail={
                    "message": (
                        "上游拒绝生成请求（403 permission denied），且近期"
                        f"新铸 proof 被拒已跨账号关联（签名器熔断中，剩余冷却 "
                        f"{signer_health.cooldown_remaining():.0f}s）。账号与会话"
                        "可能完全健康，根因大概率在 WAA 签名服务（如远程 VM "
                        "挑战过期）。请稍后重试、更换签名节点或改用本地 "
                        "waa-signer（浏览器级 attestation）。"
                    ),
                    "type": "signer_degraded",
                },
            ) from exc

        # 1. 前 2 次 403: 重放已签名请求
        if self.count <= 2:
            self.current_prepared = client.last_prepared_request
            logger.warning(
                "HTTP 403 重试 (%d/2 重放): model=%s",
                self.count,
                model,
            )
            return True

        # 2. 后 2 次 403: 尝试轮换 Cookie 并重新生成签名
        if self.count <= 4:
            self.current_prepared = None
            if self.count == 3:
                # 失效 WAA proof 复用缓存：重签阶段必须走真正的签名服务。
                # 否则 prepare_request 命中缓存会拿回刚刚被上游拒绝的同一枚
                # proof，重签重试完全失效（2026-11 逆向验证 proof 与摘要绑定
                # 且长有效期，见 signer.py 缓存说明）。
                session = getattr(client, "session", None)
                if session is not None and hasattr(session, "invalidate_proof_cache"):
                    with contextlib.suppress(Exception):
                        session.invalidate_proof_cache()
                account_svc = runtime_state.account_service
                # 用本管理器记录的“本次请求实际服务账号”而非全局活跃账号：
                # 并发请求下全局活跃账号可能已被其他请求切走，轮换错误的
                # 账号既救不了当前请求还会污染无关账号的 Cookie。
                target_acc = None
                if (
                    self.serving_account_id
                    and account_svc
                    and hasattr(account_svc, "get_account")
                ):
                    target_acc = account_svc.get_account(self.serving_account_id)
                if not self.serving_account_id and account_svc:
                    target_acc = account_svc.get_active_account()
                if (
                    account_svc is not None
                    and target_acc is not None
                    and hasattr(account_svc, "rotate_account_cookies")
                ):
                    try:
                        session = getattr(client, "session", None)
                        await account_svc.rotate_account_cookies(target_acc.id, session)
                    except Exception as rot_err:
                        # 轮换失败必须留痕：重签跳的前提是拿到新滚动 Cookie
                        # （__Secure-1PSIDTS），静默吞掉会让第 3-4 跳“看起来
                        # 在工作”而实际拿旧 Cookie 空转，排障无从下手。
                        logger.warning(
                            "403 重签前 Cookie 轮换失败 (account=%s): %s",
                            target_acc.id,
                            rot_err,
                        )
            logger.warning(
                "HTTP 403 重试 (%d/2 重签): model=%s",
                self.count - 2,
                model,
            )
            return True

        # 3. 第 5 次 403: 切换备用账号
        if self.count == 5:
            self.current_prepared = None
            account_svc = runtime_state.account_service
            # 同上：failed_id 优先取本请求实际服务的账号，避免把无关账号
            # 从候选池里排除（串号修复的 403 路径残留）。
            failed_id = None
            if (
                self.serving_account_id
                and account_svc
                and hasattr(account_svc, "get_account")
            ):
                failed_acc = account_svc.get_account(self.serving_account_id)
                failed_id = failed_acc.id if failed_acc else self.serving_account_id
            if failed_id is None and account_svc:
                active_acc = account_svc.get_active_account()
                failed_id = active_acc.id if active_acc else None
            if await try_switch_account(model=model, failed_account_id=failed_id):
                logger.warning(
                    "HTTP 403 切换备用账号重试: model=%s",
                    model,
                )
                return True
            logger.error("HTTP 403 且无可用备用账号，停止重试: model=%s", model)
            raise HTTPException(
                403, detail={"message": str(exc), "type": "auth_error"}
            ) from exc

        raise HTTPException(
            403, detail={"message": str(exc), "type": "auth_error"}
        ) from exc


# 上游占位/中间态的 finish_reason 编码：不是最终结论，必须跳过，
# 否则续写中间态（22 = WIRE_FINISH_REASON_CONTINUING）会在流里伪造出 STOP。
_IGNORED_WIRE_FINISH_REASONS: tuple[object, ...] = (
    None,
    0,
    WIRE_FINISH_REASON_CONTINUING,
)


WIRE_TO_GEMINI_FINISH_REASON: dict[int, str] = {
    0: "FINISH_REASON_UNSPECIFIED",
    1: "STOP",
    2: "MAX_TOKENS",
    3: "SAFETY",
    4: "RECITATION",
    5: "OTHER",
    6: "LANGUAGE",
    7: "BLOCKLIST",
    8: "PROHIBITED_CONTENT",
    9: "SPII",
    10: "MALFORMED_FUNCTION_CALL",
    11: "IMAGE_SAFETY",
    12: "UNEXPECTED_TOOL_CALL",
    13: "TOO_MANY_TOOL_CALLS",
    14: "IMAGE_PROHIBITED_CONTENT",
    15: "IMAGE_OTHER",
    16: "NO_IMAGE",
    17: "IMAGE_RECITATION",
    18: "MISSING_THOUGHT_SIGNATURE",
}


def to_gemini_finish_reason(wire_code: int | None) -> str:
    if wire_code is None:
        return "STOP"
    return WIRE_TO_GEMINI_FINISH_REASON.get(wire_code, "FINISH_REASON_UNSPECIFIED")


def clean_upstream_error_message(raw_msg: str) -> str:
    """Extract a clean, readable error message from Google/JSPB upstream error strings."""
    if not raw_msg:
        return ""
    text = raw_msg.strip()
    # 若不包含 JSPB/batchexecute 特征结构，直接保留原始错误文本
    if "[,[" not in text and "[null,[" not in text and not text.startswith("[,"):
        return text
    inner = text
    while True:
        m = re.match(r"^HTTP\s+\d+:\s*(.*)$", inner)
        if m:
            inner = m.group(1).strip()
        else:
            break
    _, msg = parse_upstream_grpc_error(inner)
    return msg or inner


def classify_gemini_error_payload(exc: Exception) -> tuple[int, str, str]:
    """Extract standard Gemini HTTP status code, message, and status string from an exception."""
    if isinstance(exc, SessionExpiredError):
        return (
            401,
            "All accounts have expired sessions. Please import fresh cookies.",
            "UNAUTHENTICATED",
        )
    if isinstance(exc, AuthError):
        return 403, str(exc), "PERMISSION_DENIED"
    if isinstance(exc, UsageLimitExceeded):
        return 429, str(exc), "RESOURCE_EXHAUSTED"
    if isinstance(exc, (ValueError, RequestValidationError)):
        return 400, str(exc), "INVALID_ARGUMENT"
    if isinstance(exc, HTTPException):
        status = exc.status_code
        detail = exc.detail
        raw_msg = detail.get("message") if isinstance(detail, dict) else detail
        msg = str(raw_msg) if raw_msg is not None else ""
        clean_msg = clean_upstream_error_message(msg)
        status_map = {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            429: "RESOURCE_EXHAUSTED",
            500: "INTERNAL",
            502: "UNAVAILABLE",
            503: "UNAVAILABLE",
            504: "DEADLINE_EXCEEDED",
        }
        return (
            status,
            clean_msg,
            status_map.get(status, "INTERNAL" if status >= 500 else "UNKNOWN"),
        )
    if isinstance(exc, RequestError):
        status = exc.status if exc.status > 0 else 500
        clean_msg = clean_upstream_error_message(str(exc))
        status_map = {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            429: "RESOURCE_EXHAUSTED",
            500: "INTERNAL",
            502: "UNAVAILABLE",
            503: "UNAVAILABLE",
            504: "DEADLINE_EXCEEDED",
        }
        return (
            status,
            clean_msg,
            status_map.get(status, "INTERNAL" if status >= 500 else "UNKNOWN"),
        )
    if isinstance(exc, TransportError):
        return 502, str(exc), "UNAVAILABLE"
    if isinstance(exc, AistudioError):
        return 500, str(exc), "INTERNAL"
    # 未分类异常（KeyError 等）：str(exc) 可能泄漏内部路径与账号标识，
    # 对外只给固定文案，原始异常进日志（3.15）。
    logger.error("未分类异常转为 500（对外脱敏）: %s: %s", type(exc).__name__, exc)
    return 500, "Internal server error", "INTERNAL"


async def handle_attempt_exception(
    exc: Exception,
    *,
    attempt: int,
    model_path: str,
    normalized_model: str | None,
    has_yielded_data: bool = False,
    serving_account_id: str | None = None,
    transient_attempt: int | None = None,
) -> bool:
    """Handle per-attempt error classification and account switching.

    serving_account_id 为本次尝试实际服务的账号（3.9 记账串号修复）。

    Returns True if the operation should be retried, or raises HTTPException / re-raises.
    """
    target_model = normalized_model or model_path
    error_code, error_message, _ = classify_gemini_error_payload(exc)
    record_attempt_error(error_code, error_message)
    account_svc = runtime_state.account_service
    # 统一解析「本次尝试实际服务的账号」：serving_account_id 存在时用它
    # （避免并发下全局活跃账号串号），否则回退全局活跃账号。
    # 修复：原实现只在 else 分支赋值 active_acc，serving_account_id 非空的
    # 正常路径下 SessionExpiredError 分支引用 active_acc 会 UnboundLocalError，
    # 导致「登录态失效→探活轮换→切号」链路整体失效。
    serving_acc = None
    if serving_account_id and account_svc and hasattr(account_svc, "get_account"):
        serving_acc = account_svc.get_account(serving_account_id)
        failed_id = serving_acc.id if serving_acc else serving_account_id
    else:
        serving_acc = account_svc.get_active_account() if account_svc else None
        failed_id = serving_acc.id if serving_acc else None

    if isinstance(exc, ValueError):
        raise HTTPException(
            400, detail={"message": str(exc), "type": "bad_request"}
        ) from exc

    if isinstance(exc, SessionExpiredError):
        if not has_yielded_data and account_svc is not None and serving_acc is not None:
            # 遇到 401 时先探活会话并尝试轮换滚动 Cookie，若拿到新 PSIDTS 则原地重试
            try:
                from aistudio_api.infrastructure.account.cookie_parser import (
                    load_cookies_from_auth_file,
                    probe_aistudio_session,
                )

                auth_path = account_svc.get_account_auth_path(serving_acc.id)
                cdict = load_cookies_from_auth_file(auth_path) if auth_path else {}
                before_ts = cdict.get("__Secure-1PSIDTS", "")
                if cdict:
                    alive, _ = await probe_aistudio_session(
                        cdict, proxy_url=serving_acc.proxy_url
                    )
                    if alive and hasattr(account_svc, "rotate_account_cookies"):
                        session = (
                            runtime_state.client.session
                            if runtime_state.client
                            else None
                        )
                        rot_res = await account_svc.rotate_account_cookies(
                            serving_acc.id, session
                        )
                        new_cookies = rot_res.get("cookies")
                        after_ts = (
                            new_cookies.get("__Secure-1PSIDTS", "")
                            if isinstance(new_cookies, dict)
                            else ""
                        )
                        if rot_res.get("ok") and after_ts and after_ts != before_ts:
                            logger.info(
                                "账号 %s 触发 401 后完成 Cookie 轮换，原地重试",
                                serving_acc.id,
                            )
                            return True
            except Exception as rot_err:
                logger.warning("401 Cookie 轮换探测异常: %s", rot_err)

        logger.warning("账号登录态已失效: account=%s, %s", failed_id, exc)
        if not has_yielded_data and await try_switch_account(
            model=target_model,
            failed_account_id=failed_id,
            is_session_expired=True,
        ):
            logger.info("已切换账号重试 (%d/%d)", attempt + 1, get_max_retries())
            return True
        raise HTTPException(
            401,
            detail={
                "message": "All accounts encountered login redirect or expired sessions. Please import fresh cookies.",
                "type": "auth_error",
            },
        ) from exc

    if isinstance(exc, AuthError):
        logger.warning("账号权限被拒绝 (HTTP 403): %s", exc)
        record_rotator_event(
            "auth_error", model=target_model, account_id=serving_account_id
        )

        raise HTTPException(
            403, detail={"message": str(exc), "type": "auth_error"}
        ) from exc

    if isinstance(exc, UsageLimitExceeded):
        runtime_state.record(target_model, "rate_limited")
        record_rotator_event(
            "rate_limited", model=target_model, account_id=serving_account_id
        )
        if not has_yielded_data and await try_switch_account(
            model=target_model, failed_account_id=failed_id
        ):
            logger.warning(
                "触发 429 限流，切换账号重试 (%d/%d): model=%s",
                attempt + 1,
                get_max_retries(),
                target_model,
            )
            return True
        logger.warning("模型 %s 所有账号均在冷却中", target_model)
        raise HTTPException(
            429,
            detail={
                "message": f"All accounts reached daily quota for model '{target_model}'. Quota resets at 00:00 PST.",
                "type": "rate_limit_exceeded",
            },
        ) from exc

    if (
        isinstance(exc, RequestError)
        and exc.status == 204
        and attempt == 0
        and not has_yielded_data
    ):
        logger.warning("收到上游 204 空响应，立即重试")
        return True

    # 针对 Google 上游偶发且完全随机的 RPC 路由 404 (Ambiguous request for service '' and method '/GenerativeService.*')：
    # 此报错为上游端点偶发抖动，在未产生输出时当场立即重试（严禁切号以防浪费配额，其他 404 严格按原样报错抛出）
    if (
        is_transient_rpc_error(exc)
        and not has_yielded_data
        and attempt < get_max_retries() - 1
    ):
        logger.warning(
            "上游 404 路由抖动，立即重试 (%d/%d): %s",
            attempt + 1,
            get_max_retries(),
            clean_upstream_error_message(str(exc)),
        )
        return True
    if is_retryable_upstream_error(exc):
        retry_index = attempt if transient_attempt is None else transient_attempt
        # A single-account installation can recover from a transient network
        # failure too. Never replay after exposing content/tool calls.
        if not has_yielded_data and retry_index < min(2, get_max_retries() - 1):
            switched = await try_switch_account(
                model=target_model, failed_account_id=failed_id
            )
            delay = 0.5 * (2**retry_index) * random.uniform(0.8, 1.2)
            logger.warning(
                "上游传输异常，%s，%.1fs 后重试 (%d/2): %s",
                "已切换账号" if switched else "保留当前账号",
                delay,
                retry_index + 1,
                exc,
            )
            await asyncio.sleep(delay)
            return True
        runtime_state.record(target_model, "errors")
        record_rotator_event("error", model=target_model, account_id=serving_account_id)
        code = exc.status if isinstance(exc, RequestError) else 502
        logger.warning("上游请求失败 (%d): %s", code, exc)
        raise HTTPException(
            code, detail={"message": str(exc), "type": "upstream_error"}
        ) from exc
    if isinstance(exc, RequestError):
        clean_msg = clean_upstream_error_message(str(exc))
        status_code = exc.status if exc.status > 0 else 500
        if status_code == 204:
            # 204 是"无内容"的可达信号而非可序列化的错误状态：响应体禁止
            # 携带 body（RFC 9110），带 detail 的 HTTPException(204) 会产出
            # 非法响应。按上游异常终结统一归一为 502 上抛。
            status_code = 502
        error_type = (
            "bad_request"
            if status_code == 400
            else "not_found"
            if status_code == 404
            else "upstream_error"
        )
        logger.warning(
            "上游返回请求错误: status=%d, message=%s", status_code, clean_msg
        )
        raise HTTPException(
            status_code, detail={"message": clean_msg, "type": error_type}
        ) from exc

    if isinstance(exc, AistudioError):
        runtime_state.record(target_model, "errors")
        record_rotator_event("error", model=target_model, account_id=serving_account_id)
        logger.warning("服务端处理异常: %s", exc)
        raise HTTPException(
            500, detail={"message": str(exc), "type": "server_error"}
        ) from exc

    runtime_state.record(target_model, "errors")
    record_rotator_event("error", model=target_model, account_id=serving_account_id)
    logger.error("请求处理发生未捕获异常: %s", exc)
    logger.debug("异常堆栈详情:", exc_info=True)
    raise HTTPException(
        500, detail={"message": str(exc), "type": "server_error"}
    ) from exc


def _build_gemini_response_from_output(
    output: ModelOutput, model_version: str
) -> GeminiGenerateContentResponse:
    """把缓冲完成的 ModelOutput 组装为 Gemini 完整响应（非流式 JSON 与
    缓冲变体的单条 SSE 事件共用）。"""
    # 签名缓存回填：原生路径的 lookup 挂在 normalize_gemini_request，
    # 此前却只在 compat 层 remember（只取不存）——纯原生 /v1beta 用户
    # 丢签名后永远只能走 SKIP 字面量。
    for call in output.function_calls:
        if isinstance(call, dict):
            raw_call_id = call.get("call_id") or call.get("id")
            raw_name = call.get("name")
            _remember_function_signature(
                raw_call_id if isinstance(raw_call_id, str) else None,
                raw_name if isinstance(raw_name, str) else None,
                call.get("args"),
                call.get("thought_signature"),
            )
    if output.thinking or output.reasoning_images:
        record_output_timing("thinking")
    if (
        output.text
        or output.function_calls
        or output.function_responses
        or output.images
    ):
        record_output_timing("body")
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(
                    parts=to_gemini_parts(
                        output.text,
                        function_calls=output.function_calls,
                        function_responses=output.function_responses,
                        thinking=output.thinking,
                        images=output.images,
                        reasoning_images=output.reasoning_images,
                        thought_signature=(
                            output.candidates[0].thought_signature
                            if output.candidates
                            else ""
                        ),
                    ),
                ),
                finishReason=to_gemini_finish_reason(
                    output.candidates[0].finish_reason if output.candidates else None
                ),
                index=0,
            )
        ],
        usageMetadata=to_gemini_usage_metadata(output.usage),
        modelVersion=model_version,
        responseId=output.response_id or None,
    )


async def handle_gemini_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    stream: bool,
    anti_truncation: bool | None = None,
):
    observation = current_request.get()
    if observation is not None:
        observation.model = model_path
        if not observation.request_parameters:
            record_requested_parameters(
                {
                    **(
                        req.generationConfig.model_dump(exclude_none=True)
                        if req.generationConfig
                        else {}
                    ),
                    "reasoningEffort": (
                        req.generationConfig.reasoningEffort
                        if req.generationConfig
                        else None
                    )
                    or req.reasoningEffort,
                }
            )
    # 抗截断模型变体：带后缀的模型强制开启抗截断；请求头 X-Anti-Truncation
    # 显式传值时以请求头为准。非流式变体额外强制缓冲（非流式）上游传输。
    force_buffer = False
    variant = resolve_model_variant(model_path)
    if variant is not None:
        logger.info(
            "抗截断模型变体: requested=%s, base=%s, buffered=%s",
            model_path,
            variant.model_path,
            variant.buffered,
        )
        model_path = variant.model_path
        if anti_truncation is None:
            anti_truncation = True
        force_buffer = variant.buffered

    if stream:
        return _build_gemini_streaming_response(
            client=client,
            req=req,
            model_path=model_path,
            anti_truncation=anti_truncation,
            force_buffer=force_buffer,
        )

    last_error: Exception | None = None
    auth_manager = Auth403RetryManager()
    transient_failures = 0
    total_attempts = get_max_retries() + AUTH_403_MAX_RETRIES
    # 本次请求实际服务的账号（3.9）：在每次尝试发送前刷新，收尾记账用它，
    # 避免并发请求切号后把结果记到无关账号头上。
    serving_account_id: str | None = None
    normalized = None
    at_ctx = None
    for attempt in range(total_attempts):
        await ensure_active_account(attempt, model=model_path)
        _svc = runtime_state.account_service
        _acc = _svc.get_active_account() if _svc else None
        serving_account_id = _acc.id if _acc else None
        auth_manager.serving_account_id = serving_account_id
        dispatched = False
        try:
            # 归一化与抗截断注入只随请求变化、与重试尝试无关（prepare 会
            # 原地改写 normalized：contents 注入指令 / tools 追加声明）。
            # 逐尝试重建会让带大工具 schema 的请求在 403 阶梯重放跳（复用
            # 已签名请求、归一化产物被整体丢弃）白付全量 deepcopy+编码。
            if normalized is None:
                normalized = normalize_gemini_request(req, model_path)
                at_ctx = prepare_anti_truncation(normalized, override=anti_truncation)
            assert at_ctx is not None  # 与 normalized 同步初始化
            logger.info(
                "非流式请求: model=%s, 轮数=%d, 尝试=%d/%d",
                normalized.model,
                len(req.contents),
                attempt + 1,
                total_attempts,
            )
            # 上游请求即将派出：此后任意时刻的取消都视为「上游已受理」
            # （与流式断连兜底同语义），记账兜底见下方 CancelledError 分支。
            dispatched = True
            output = await client.generate_content(
                model=normalized.model,
                contents=normalized.contents,
                system_instruction_content=normalized.system_instruction,
                tools=normalized.tools,
                tool_config=normalized.tool_config,
                safety_settings=normalized.safety_settings,
                temperature=normalized.temperature,
                top_p=normalized.top_p,
                top_k=normalized.top_k,
                max_tokens=normalized.max_tokens,
                generation_config_overrides=normalized.generation_config_overrides,
                prepared=auth_manager.current_prepared,
                function_name_restore=normalized.function_name_restore,
            )
            auth_manager.current_prepared = None
            restore_anti_truncation_output(output, at_ctx)

            final_fr = to_gemini_finish_reason(
                output.candidates[0].finish_reason if output.candidates else None
            )
            # 空回防护：上游 200 但零内容时留下明确日志，便于面板排查
            has_any_output = bool(
                output.text
                or output.function_calls
                or output.function_responses
                or output.images
                or output.reasoning_images
            )
            if not has_any_output:
                logger.warning(
                    "上游返回 200 但内容为空（可能是上游风控拦截或安全策略）: "
                    "model=%s, finish=%s, 尝试次数=%d/%d",
                    normalized.model,
                    final_fr,
                    attempt + 1,
                    total_attempts,
                )
            # 空回不能记为成功：上游 200 但零内容通常意味着风控/安全拦截，
            # 记 success 会让账号健康度虚高、掩盖被软封的账号。
            observation = current_request.get()
            if observation and observation.account_id:
                serving_account_id = observation.account_id
            rotator_event = "success" if has_any_output else "error"
            record_rotator_event(
                rotator_event,
                model=normalized.model,
                usage=output.usage,
                account_id=serving_account_id,
            )
            # 签名器健康信号：能产出即当次 proof 通过了上游 attestation，
            # 解除熔断（半开探测语义，见 waa/health.py）。
            if has_any_output:
                get_signer_health().record_proof_accepted(serving_account_id)
            runtime_state.record(
                normalized.model,
                rotator_event,
                usage=output.usage,
                finish_reason=final_fr,
            )
            pt = output.usage.get("prompt_tokens") if output.usage else 0
            ct = output.usage.get("completion_tokens") if output.usage else 0
            logger.info(
                "非流式完成: model=%s, finish=%s, in=%s, out=%s",
                normalized.model,
                final_fr,
                pt,
                ct,
            )
            response_obj = _build_gemini_response_from_output(output, normalized.model)
            # 空回防护：上游 200 但零内容时注入可见诊断文本，避免客户端空回无从排查
            if not has_any_output:
                diagnostic = (
                    "[aistudio-api 空回诊断] 上游返回 200 但内容为空 "
                    f"(finish={final_fr})。"
                    "可能为账号风控拦截或安全策略，请在面板“运行日志”页查看详情。"
                )
                response_obj.candidates[0].content.parts.append(
                    GeminiPartResponse(text=diagnostic)
                )
            return response_obj
        except asyncio.CancelledError:
            # 客户端断开（_DeferredResponse 断连）/保活 504 取消任务：
            # CancelledError 不是 Exception 子类，原先直接穿透——上游请求
            # 已派出（配额大概率已消耗）却一次都不记账，账号余量持续高估。
            # 与流式断连兑底（_record_stream_disconnect）对称补记一次 success
            # （无 usage 帧可用，usage=None），随后照常传播取消。
            if dispatched and normalized is not None:
                with contextlib.suppress(Exception):
                    record_rotator_event(
                        "success",
                        model=normalized.model,
                        usage=None,
                        account_id=serving_account_id,
                    )
                    logger.info(
                        "非流式请求被取消（上游已受理），已补记调度账: model=%s",
                        normalized.model,
                    )
            raise
        except Exception as exc:
            last_error = exc
            observation = current_request.get()
            if observation and observation.account_id:
                serving_account_id = observation.account_id
                auth_manager.serving_account_id = serving_account_id
            if isinstance(exc, AuthError) and not isinstance(exc, SessionExpiredError):
                target_m = normalized.model if normalized else model_path
                if await auth_manager.handle_403(exc, model=target_m, client=client):
                    continue
                raise
            transient_failure = is_retryable_upstream_error(exc)
            transient_failures += int(transient_failure)
            if transient_failure:
                auth_manager.current_prepared = None
            if await handle_attempt_exception(
                exc,
                attempt=attempt,
                transient_attempt=transient_failures - 1 if transient_failure else None,
                model_path=model_path,
                normalized_model=normalized.model if normalized else None,
                serving_account_id=serving_account_id,
            ):
                continue
            raise

    # 重试耗尽：最后一跳的异常才是真实失败原因。原实现固定报 429
    # “配额耗尽”，但循环退出的原因也可能是传输中断/上游 5xx，统一报
    # 429 会误导客户端与运维排障，这里按真实异常分类返回。
    if last_error is not None and not isinstance(last_error, UsageLimitExceeded):
        status, msg, status_str = classify_gemini_error_payload(last_error)
        raise HTTPException(
            status,
            detail={"message": msg, "status": status_str, "type": "upstream_error"},
        ) from last_error
    raise HTTPException(
        429,
        detail={
            "message": f"All accounts reached daily quota for model '{model_path}'. Quota resets at 00:00 PST.",
            "type": "rate_limit_exceeded",
        },
    ) from last_error


def _encode_sse(payload: dict[str, object]) -> str:
    # separators：SSE 每帧省去 ", " 与 ": " 的空格（高频 token 流可观）
    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


def _event_payload(
    event_type: str,
    text: object,
    *,
    model_version: str | None = None,
    response_id: str | None = None,
    thought_signature: str | None = None,
) -> dict[str, object] | None:
    if not text:
        return None

    parts: list[dict[str, object]] = []
    if event_type == "signature":
        parts.append({"thoughtSignature": str(text)})
    elif event_type == "body":
        p: dict[str, object] = {"text": str(text)}
        if thought_signature:
            p["thoughtSignature"] = thought_signature
        parts.append(p)
    elif event_type == "thinking":
        p = {"text": str(text), "thought": True}
        if thought_signature:
            p["thoughtSignature"] = thought_signature
        parts.append(p)
    elif event_type == "tool_calls":
        fc_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", function_calls=fc_list)
        ]
        # pending_signature 只作兑底：part 自带签名时不得覆盖（覆盖会让
        # 客户端收到的签名与签名缓存记住的不一致，破坏思考链回传）。
        if thought_signature and parts and not parts[0].get("thoughtSignature"):
            parts[0]["thoughtSignature"] = thought_signature
    elif event_type == "images":
        img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", images=img_list)
        ]
        if thought_signature and parts and not parts[0].get("thoughtSignature"):
            parts[0]["thoughtSignature"] = thought_signature
    elif event_type == "reasoning_images":
        r_img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", reasoning_images=r_img_list)
        ]
        if thought_signature and parts and not parts[0].get("thoughtSignature"):
            parts[0]["thoughtSignature"] = thought_signature
    else:
        return None

    payload: dict[str, object] = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "index": 0}]
    }
    if model_version:
        payload["modelVersion"] = model_version
    if response_id:
        payload["responseId"] = response_id
    return payload


def _usage_payload(
    final_usage: dict[str, object] | None,
    *,
    finish_reason: str = "STOP",
    model_version: str | None = None,
    response_id: str | None = None,
) -> dict[str, object]:
    effective_usage = final_usage or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    usage_dict = to_gemini_usage_metadata(effective_usage).model_dump(
        mode="json", exclude_none=True
    )
    payload: dict[str, object] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": []},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": usage_dict,
    }
    if model_version:
        payload["modelVersion"] = model_version
    if response_id:
        payload["responseId"] = response_id
    return payload


def _error_payload(exc: Exception) -> dict[str, object]:
    code, msg, status_str = classify_gemini_error_payload(exc)
    return {"error": {"code": code, "message": msg, "status": status_str}}


_ERROR_TEXT_PREFIX = "[aistudio-api 请求失败"


def _error_text_payload(exc: Exception) -> dict[str, object] | None:
    code, msg, status_str = classify_gemini_error_payload(exc)
    if not msg:
        msg = type(exc).__name__
    text = f"{_ERROR_TEXT_PREFIX} {code} {status_str}] {msg}"
    return _event_payload("body", text)


class GeminiStreamResponse(HeartbeatStreamingResponse):
    """Expose structured frames to adapters; serialize only at the HTTP boundary."""

    # google-genai parses every nonempty line as JSON instead of skipping SSE
    # comments. Empty SSE blocks keep bytes flowing without producing SDK chunks.
    heartbeat = b"\n\n"

    def __init__(self, frames: AsyncGenerator[dict[str, object], None]) -> None:
        self.frames = frames

        async def encode() -> AsyncGenerator[str, None]:
            try:
                async for frame in frames:
                    yield _encode_sse(frame)
            finally:
                await frames.aclose()

        super().__init__(
            encode(),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
        )


# ─── 流式响应拆分出的纯逻辑辅助（无重试编排、无 yield 语义）───


@dataclass
class _StreamAttemptState:
    """单次流式尝试的可变收尾状态（事件循环写入，重试编排层读取）。"""

    has_yielded_data: bool = False
    final_usage: dict[str, object] | None = None
    final_finish_reason: str = "STOP"
    latest_response_id: str | None = None
    pending_signature: str | None = None

    def reset(self) -> None:
        self.has_yielded_data = False
        self.final_usage = None
        self.final_finish_reason = "STOP"
        self.latest_response_id = None
        self.pending_signature = None


def _stream_request_kwargs(normalized: NormalizedGeminiRequest) -> dict[str, Any]:
    """展开归一化请求为上游调用 kwargs（缓冲/流式两条路径共用同一参数表）。

    仅 ``prepared`` 逐尝试变化（403 重放跳复用已签名请求），由调用方追加，
    单一来源避免两处 15 参列表漂移。返回值用于 ``**`` 展开到类型化调用点，
    故值为 Any（构造处来源字段均为强类型，实际类型不受影响）。
    """
    return {
        "model": normalized.model,
        "contents": normalized.contents,
        "system_instruction_content": normalized.system_instruction,
        "tools": normalized.tools,
        "tool_config": normalized.tool_config,
        "safety_settings": normalized.safety_settings,
        "temperature": normalized.temperature,
        "top_p": normalized.top_p,
        "top_k": normalized.top_k,
        "max_tokens": normalized.max_tokens,
        "generation_config_overrides": normalized.generation_config_overrides,
        "function_name_restore": normalized.function_name_restore,
    }


def _empty_stream_diagnostic(finish_reason: str) -> str:
    """空回防护可见诊断文本（流式/缓冲/非流式三处共用同一文案）。"""
    return (
        "[aistudio-api 空回诊断] 上游返回 200 但内容为空 "
        f"(finish={finish_reason})。"
        "可能为账号风控拦截或安全策略，请在面板“运行日志”页查看详情。"
    )


def _empty_stream_diagnostic_payload(
    finish_reason: str,
) -> dict[str, object] | None:
    """空回防护帧：把可见诊断文本编码为候选 SSE 帧。"""
    return _event_payload("body", _empty_stream_diagnostic(finish_reason))


def _finalize_buffered_output(
    output: ModelOutput,
    at_ctx: AntiTruncationContext,
    model: str,
) -> tuple[
    dict[str, object] | None,
    str,
    str,
    GeminiGenerateContentResponse,
    bool,
]:
    """缓冲（非流式上游）尝试的纯收尾：还原抗截断、判定空回并注入诊断。

    与非流式路径（空回防护 3.8）对齐：-at 缓冲变体仅在真正产出内容时才算
    has_yielded_data，否则上游 200 但零内容的空回会被记为 success 且跳过
    诊断注入，被风控软封的账号健康度虚高。
    """
    restore_anti_truncation_output(output, at_ctx)
    final_usage = output.usage if isinstance(output.usage, dict) else None
    final_finish_reason = to_gemini_finish_reason(
        output.candidates[0].finish_reason if output.candidates else None
    )
    buffered_resp = _build_gemini_response_from_output(output, model)
    buffered_has_output = bool(
        output.text
        or output.function_calls
        or output.function_responses
        or output.images
        or output.reasoning_images
    )
    if not buffered_has_output:
        logger.warning(
            "抗截断缓冲变体返回 200 但内容为空（可能是风控拦截）: model=%s, finish=%s",
            model,
            final_finish_reason,
        )
        if buffered_resp.candidates:
            buffered_resp.candidates[0].content.parts.append(
                GeminiPartResponse(text=_empty_stream_diagnostic(final_finish_reason))
            )
    return (
        final_usage,
        final_finish_reason,
        output.response_id,
        buffered_resp,
        buffered_has_output,
    )


def _remember_stream_tool_signatures(
    text: object, pending_signature: str | None
) -> None:
    """签名缓存回填（原生流式路径，与 _build_gemini_response_from_output 同
    标准）：tool_calls 事件载荷是 function_call dict 列表，签名内嵌在 dict
    或前置 pending_signature 中。
    """
    if not isinstance(text, list):
        return
    for call_index, call in enumerate(text):
        if not isinstance(call, dict):
            continue
        signature = call.get("thought_signature") or (
            pending_signature if call_index == 0 else None
        )
        _remember_function_signature(
            call.get("call_id") or call.get("id"),
            call.get("name"),
            call.get("args"),
            signature,
        )


def _record_usage_observation(usage: dict[str, object] | None) -> None:
    """usage 事件到达时同步到请求观测（request_context.usage）。"""
    observation = current_request.get()
    if observation is not None and usage is not None:
        observation.usage = dict(usage)


def _observed_serving_account(current_id: str | None) -> str | None:
    """以请求上下文记录的实际服务账号为准（并发下全局活跃账号可能已切走）。"""
    observation = current_request.get()
    if observation and observation.account_id:
        return observation.account_id
    return current_id


def _log_empty_stream(target_model: str, finish_reason: str) -> None:
    logger.warning(
        "流式完成但未产生任何内容（可能是上游风控拦截或安全策略）: model=%s, finish=%s",
        target_model,
        finish_reason,
    )


def _record_stream_success(
    *,
    target_model: str,
    has_yielded_data: bool,
    final_usage: dict[str, object] | None,
    final_finish_reason: str,
    serving_account_id: str | None,
    normalized: NormalizedGeminiRequest | None,
) -> None:
    """流式成功收尾的调度记账（rotator / 签名器健康 / 运行时统计）。

    与空回防护一致：零内容不能记为 success（记 success 会让账号健康度虚高）。
    """
    stream_event = "success" if has_yielded_data else "error"
    record_rotator_event(
        stream_event,
        model=target_model,
        usage=final_usage,
        account_id=serving_account_id,
    )
    # 签名器健康信号：已产出数据即当次 proof 通过了上游 attestation，解除
    # 熔断（半开探测语义，见 waa/health.py）。
    if has_yielded_data:
        get_signer_health().record_proof_accepted(serving_account_id)
    if normalized is not None:
        runtime_state.record(
            normalized.model,
            stream_event,
            usage=final_usage,
            finish_reason=final_finish_reason,
        )


def _log_stream_completed(
    target_model: str,
    stream_start_time: float,
    finish_reason: str,
    final_usage: dict[str, object] | None,
) -> None:
    elapsed_total_ms = (time.perf_counter() - stream_start_time) * 1000
    pt = final_usage.get("prompt_tokens") if final_usage else 0
    ct = final_usage.get("completion_tokens") if final_usage else 0
    logger.info(
        "流式完成: model=%s, 耗时=%.1fms, finish=%s, in=%s, out=%s",
        target_model,
        elapsed_total_ms,
        finish_reason,
        pt,
        ct,
    )


def _log_stream_failure(
    exc: Exception,
    elapsed_ms: float,
    model_path: str,
    has_yielded_any: bool,
) -> None:
    code, msg, status_str = classify_gemini_error_payload(exc)
    record_result("rate_limited" if code == 429 else "error", error=msg)
    logger.warning(
        "流式生成失败: model=%s, 耗时=%.1fms, code=%d(%s), "
        "已输出内容=%s, 异常类型=%s, 原因=%s",
        model_path,
        elapsed_ms,
        code,
        status_str,
        "是" if has_yielded_any else "否",
        type(exc).__name__,
        msg,
    )


def _record_stream_disconnect(
    normalized: NormalizedGeminiRequest,
    state: _StreamAttemptState,
    serving_account_id: str | None,
) -> None:
    """客户端断连（GeneratorExit/CancelledError）路径的兜底补记。

    成功块与异常块都执行不到该路径；已产出内容按 success 记账（账号确实
    完成了服务）；全部为同步调用，在 GeneratorExit 传播中执行安全。
    """
    record_rotator_event(
        "success",
        model=normalized.model,
        usage=state.final_usage,
        account_id=serving_account_id,
    )
    get_signer_health().record_proof_accepted(serving_account_id)
    runtime_state.record(
        normalized.model,
        "success",
        usage=state.final_usage,
        finish_reason=state.final_finish_reason,
    )
    logger.info(
        "流式因客户端断连提前结束，已按已产出内容补记调度账: model=%s",
        normalized.model,
    )


async def _retry_stream_attempt(
    exc: Exception,
    *,
    stream_attempt: int,
    transient_failures: int,
    auth_manager: Auth403RetryManager,
    client: AIStudioClient,
    model_path: str,
    normalized: NormalizedGeminiRequest | None,
    has_yielded_data: bool,
    serving_account_id: str | None,
) -> tuple[bool, int]:
    """单次流式尝试失败后的重试决策（403 阶梯分流 + 瞬态重试判定）。

    返回 ``(可重试, 新瞬态失败计数)``；不可重试时直接上抛（handle_403 /
    handle_attempt_exception 的既有语义，含 HTTPException 转换）。已 yield
    过数据（has_yielded_data）后不可重试的守卫语义全部保持。
    """
    if isinstance(exc, AuthError) and not isinstance(exc, SessionExpiredError):
        # 403 全量经 handle_403 分流（含已产出数据的中途 403）：其
        # has_yielded_data 分支记 proof-accepted 信号并直接上抛。原先已
        # 产出后绕行 handle_attempt_exception 会对同一请求再记一次
        # auth_error（双重 200 权重惩罚），且令 handle_403 的 post-yield
        # 分支成为死代码。
        target_m = normalized.model if normalized else model_path
        if await auth_manager.handle_403(
            exc,
            model=target_m,
            client=client,
            has_yielded_data=has_yielded_data,
        ):
            return True, transient_failures
        raise exc
    transient_failure = is_retryable_upstream_error(exc)
    increment = int(transient_failure)
    if transient_failure:
        auth_manager.current_prepared = None
    retry = await handle_attempt_exception(
        exc,
        attempt=stream_attempt,
        transient_attempt=transient_failures + increment - 1
        if transient_failure
        else None,
        model_path=model_path,
        normalized_model=normalized.model if normalized else None,
        has_yielded_data=has_yielded_data,
        serving_account_id=serving_account_id,
    )
    return retry, transient_failures + increment


async def _stream_attempt_frames(
    anti_stream: AsyncIterator[tuple[str, object | None]],
    state: _StreamAttemptState,
    model: str,
    stream_start_time: float,
) -> AsyncGenerator[dict[str, object], None]:
    """消费单次尝试的上游事件流，产出对外 SSE 帧（yield 顺序与拆分前一致）。

    usage / finish_reason / response_id 事件不产帧，只更新尝试状态；
    thought_signature 在首个内容帧前缓存（pending_signature），其后独立成
    帧；其余内容事件编码为候选帧，首个内容帧附带 TTFT 日志与签名缓存回填。
    """
    async for event_type, text in anti_stream:
        if event_type == "usage":
            state.final_usage = text if isinstance(text, dict) else None
            _record_usage_observation(state.final_usage)
        elif event_type == "finish_reason":
            if text not in _IGNORED_WIRE_FINISH_REASONS:
                state.final_finish_reason = (
                    to_gemini_finish_reason(text)
                    if isinstance(text, int)
                    else str(text)
                )
        elif event_type == "response_id":
            state.latest_response_id = str(text)
        elif event_type == "thought_signature":
            if state.has_yielded_data:
                signature_frame = _event_payload(
                    "signature",
                    text,
                    model_version=model,
                    response_id=state.latest_response_id,
                )
                if signature_frame:
                    yield signature_frame
            else:
                state.pending_signature = str(text)
        else:
            chunk = _event_payload(
                event_type,
                text,
                model_version=model,
                response_id=state.latest_response_id,
                thought_signature=state.pending_signature,
            )
            if chunk:
                _remember_stream_tool_signatures(text, state.pending_signature)
                state.pending_signature = None
                record_output_timing(event_type)
                if not state.has_yielded_data:
                    logger.info(
                        "流式首包到达: model=%s, ttft=%.1fms",
                        model,
                        (time.perf_counter() - stream_start_time) * 1000,
                    )
                state.has_yielded_data = True
                yield chunk


def _build_gemini_streaming_response(
    *,
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
    anti_truncation: bool | None = None,
    force_buffer: bool = False,
) -> GeminiStreamResponse:
    """构建 Gemini 流式响应（暴露结构化 frames 供适配层消费）。"""

    async def stream_response() -> AsyncGenerator[dict[str, object], None]:
        normalized: NormalizedGeminiRequest | None = None
        at_ctx: AntiTruncationContext | None = None
        stream_start_time = time.perf_counter()
        has_yielded_any = False
        stream_completed = False
        last_stream_error: Exception | None = None
        # 断连/取消路径的记账幂等标记：正常成功块与异常块各自置位，finally
        # 仅在两者都未触达（GeneratorExit/CancelledError）时补记。
        accounting_recorded = False
        # finally 兜底记账引用的循环内状态：在 try 之前初始化，断连或异常
        # 发生在首次赋值之前也不会出现未绑定名。
        serving_account_id: str | None = None
        state = _StreamAttemptState()
        try:
            logger.info(
                "流式请求: model=%s, 轮数=%d",
                model_path,
                len(req.contents),
            )
            auth_manager = Auth403RetryManager()
            transient_failures = 0
            total_attempts = get_max_retries() + AUTH_403_MAX_RETRIES
            for stream_attempt in range(total_attempts):
                state.reset()
                last_stream_error = None
                await ensure_active_account(stream_attempt, model=model_path)
                _svc = runtime_state.account_service
                _acc = _svc.get_active_account() if _svc else None
                serving_account_id = _acc.id if _acc else None
                auth_manager.serving_account_id = serving_account_id
                # 归一化与抗截断注入只随请求变化、与重试尝试无关（同非流式
                # 路径：逐尝试重建在 403 重放跳是纯浪费，且 prepare 会原地
                # 注入、必须只执行一次）。
                if normalized is None:
                    normalized = normalize_gemini_request(req, model_path)
                    at_ctx = prepare_anti_truncation(
                        normalized, override=anti_truncation
                    )
                assert at_ctx is not None  # 与 normalized 同步初始化
                try:
                    if force_buffer:
                        # 非流式抗截断变体：上游改走缓冲（非流式）传输，整体
                        # 还原后打包为单条完整 SSE 事件下发。
                        output = await client.generate_content(
                            **_stream_request_kwargs(normalized),
                            prepared=auth_manager.current_prepared,
                        )
                        auth_manager.current_prepared = None
                        (
                            state.final_usage,
                            state.final_finish_reason,
                            state.latest_response_id,
                            buffered_resp,
                            buffered_has_output,
                        ) = _finalize_buffered_output(output, at_ctx, normalized.model)
                        state.has_yielded_data = buffered_has_output
                        has_yielded_any = True
                        yield buffered_resp.model_dump(mode="json", exclude_none=True)
                        stream_completed = True
                        break
                    upstream_events = client.stream_generate_content(
                        **_stream_request_kwargs(normalized),
                        prepared=auth_manager.current_prepared,
                    )
                    # 重试 continue / 客户端断连时，async for 不会自动关闭被
                    # 迭代的异步生成器；这里持引用并用 try/finally 确定性
                    # 关闭（与 compat 适配器同标准）。
                    anti_stream = restore_anti_truncation_stream(
                        upstream_events, at_ctx
                    )
                    attempt_frames = _stream_attempt_frames(
                        anti_stream,
                        state,
                        normalized.model,
                        stream_start_time,
                    )
                    try:
                        async for frame in attempt_frames:
                            has_yielded_any = True
                            yield frame
                        auth_manager.current_prepared = None
                        stream_completed = True
                        break
                    finally:
                        # 正常完成 / 异常重试 / 客户端断连都走这里，确定性
                        # 释放上游连接（关闭包装器会连带关闭内层流）；帧层
                        # 包装生成器同样确定性收尾，不依赖事件循环的
                        # 异步生成器终结器延迟回收。
                        await _close_stream_quietly(attempt_frames)
                        await _close_stream_quietly(anti_stream)
                except Exception as exc:
                    last_stream_error = exc
                    serving_account_id = _observed_serving_account(serving_account_id)
                    auth_manager.serving_account_id = serving_account_id
                    # 重试决策（403 阶梯分流 + 瞬态判定）见
                    # _retry_stream_attempt；已 yield 过数据后不可重试的守卫
                    # 语义全部保持。
                    retry, transient_failures = await _retry_stream_attempt(
                        exc,
                        stream_attempt=stream_attempt,
                        transient_failures=transient_failures,
                        auth_manager=auth_manager,
                        client=client,
                        model_path=model_path,
                        normalized=normalized,
                        has_yielded_data=state.has_yielded_data,
                        serving_account_id=serving_account_id,
                    )
                    if retry:
                        continue
                    raise

            if not stream_completed and last_stream_error is not None:
                raise last_stream_error
            target_model = normalized.model if normalized else model_path
            # 空回防护：流式完成但未产出任何内容时注入可见诊断文本，避免
            # 客户端收到 200 却“空回啥都没有”无从排查。
            if stream_completed and not state.has_yielded_data and not force_buffer:
                _log_empty_stream(target_model, state.final_finish_reason)
                diagnostic_chunk = _empty_stream_diagnostic_payload(
                    state.final_finish_reason
                )
                if diagnostic_chunk:
                    has_yielded_any = True
                    yield diagnostic_chunk
            # 收尾记账以请求上下文记录的实际服务账号为准（防并发串号）。
            serving_account_id = _observed_serving_account(serving_account_id)
            _record_stream_success(
                target_model=target_model,
                has_yielded_data=state.has_yielded_data,
                final_usage=state.final_usage,
                final_finish_reason=state.final_finish_reason,
                serving_account_id=serving_account_id,
                normalized=normalized,
            )
            accounting_recorded = True
            _log_stream_completed(
                target_model,
                stream_start_time,
                state.final_finish_reason,
                state.final_usage,
            )
            yield _usage_payload(
                state.final_usage,
                finish_reason=state.final_finish_reason,
                model_version=target_model,
                response_id=state.latest_response_id,
            )
        except Exception as exc:
            # 异常路径的记账已由 handle_attempt_exception / handle_403 按
            # 尝试完成，这里只负责错误事件下发；先置位标记，防止断连打断
            # 错误帧下发时 finally 再补记一次。
            accounting_recorded = True
            elapsed_ms = (time.perf_counter() - stream_start_time) * 1000
            _log_stream_failure(exc, elapsed_ms, model_path, has_yielded_any)
            # 错误透传：尚未输出任何内容时把失败原因作为可见文本 part 下发
            # （部分客户端忽略 {"error":...} 表现为空回）；已输出时不追加，
            # 保持正文纯净（仅下发结构化错误事件）。
            if not has_yielded_any:
                error_text_chunk = _error_text_payload(exc)
                if error_text_chunk:
                    yield error_text_chunk
            yield _error_payload(exc)
        finally:
            # 客户端断连（GeneratorExit）/任务取消（CancelledError）都不是
            # Exception 子类，成功块与异常块都执行不到——此处兜底补记：已
            # 产出内容按 success 记账（账号确实完成了服务）；零产出且无上游
            # 异常时不记（客户端主动放弃不应惩罚账号）。全部为同步调用，在
            # GeneratorExit 传播中执行安全（此路径严禁再 yield）。
            if (
                not accounting_recorded
                and state.has_yielded_data
                and normalized is not None
            ):
                with contextlib.suppress(Exception):
                    _record_stream_disconnect(normalized, state, serving_account_id)

    return GeminiStreamResponse(stream_response())


async def handle_gemini_count_tokens(
    model_path: str,
    req: GeminiCountTokensRequest,
    client: AIStudioClient,
) -> dict[str, int]:
    """Gemini ``:countTokens`` 处理器：转发到上游 CountTokens 真实端点。

    上游无需 WAA snapshot（逆向验证 2026-11），失败分类与 generateContent
    一致。CountTokens 不消耗生成配额，因此不进入 403 阶梯重试/账号轮换
    复杂链路，仅在鉴权失败时切换一次账号重试。
    """
    clean_model = sanitize_model_path(model_path)
    effective_contents = req.contents
    system_instruction = None
    if req.generateContentRequest is not None:
        if not effective_contents:
            effective_contents = req.generateContentRequest.contents
        # generateContentRequest 形态携带的系统提示也计入 token 统计
        # （丢弃会让计数偏小，与 generateContent 实际消耗不一致）。
        system_instruction = req.generateContentRequest.systemInstruction
    if not effective_contents:
        raise HTTPException(
            400, detail={"message": "contents is required", "type": "bad_request"}
        )

    normalized_req = GeminiGenerateContentRequest(
        contents=effective_contents, systemInstruction=system_instruction
    )

    for attempt in range(2):
        await ensure_active_account(attempt, model=clean_model)
        try:
            normalized = normalize_gemini_request(normalized_req, clean_model)
            # 系统指令计入统计：CountTokens body 无独立系统指令槽，把
            # system_instruction 作为额外 content 前插（角色词不计入 token
            # 总数，计数与 generateContent 的实际消耗一致）。
            count_contents: list[AistudioContent] = list(normalized.contents)
            if normalized.system_instruction is not None:
                count_contents.insert(0, normalized.system_instruction)
            total = await client.count_tokens(
                model=normalized.model,
                contents=count_contents,
            )
            logger.info(
                "countTokens 完成: model=%s, total=%d",
                normalized.model,
                total,
            )
            return {"totalTokens": total}
        except AuthError as exc:
            # 登录态/权限失败：换账号重试一次，仍失败则如实上抛。
            if attempt == 0 and await try_switch_account(model=clean_model):
                logger.warning("countTokens 鉴权失败，已切换账号重试: %s", exc)
                continue
            status_code, msg, status_str = classify_gemini_error_payload(exc)
            raise HTTPException(
                status_code,
                detail={"message": msg, "status": status_str, "type": "auth_error"},
            ) from exc
        except ValueError as exc:
            # normalize_gemini_request 对全空轮（无系统提示且无有效内容）
            # 抛 ValueError：与 generateContent 路径的 400 语义对齐，而非
            # 让它穿透成 FastAPI 500。
            raise HTTPException(
                400,
                detail={"message": str(exc), "type": "bad_request"},
            ) from exc
        except RequestError as exc:
            clean_msg = clean_upstream_error_message(str(exc))
            raise HTTPException(
                exc.status if exc.status > 0 else 400,
                detail={"message": clean_msg, "type": "bad_request"},
            ) from exc
        except AistudioError as exc:
            raise HTTPException(
                502, detail={"message": str(exc), "type": "upstream_error"}
            ) from exc
    raise HTTPException(
        500,
        detail={"message": "countTokens failed after retries", "type": "server_error"},
    )
