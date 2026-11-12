"""Gemini-compatible application service handlers."""

from __future__ import annotations

import contextlib
import json
import re
import time

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse

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
    _close_stream_quietly,
    prepare_anti_truncation,
    resolve_model_variant,
    restore_anti_truncation_output,
    restore_anti_truncation_stream,
)
from aistudio_api.application.chat_service import (
    normalize_gemini_request,
)
from aistudio_api.domain.errors import (
    AistudioError,
    AuthError,
    RequestError,
    SessionExpiredError,
    TransportError,
    UsageLimitExceeded,
    is_transient_rpc_error,
    is_transport_error,
    parse_upstream_grpc_error,
)
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.gateway.client import (
    AIStudioClient,
    PreparedRequest,
)
from aistudio_api.infrastructure.gateway.wire_spec import (
    WIRE_FINISH_REASON_CONTINUING,
)
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
    4. 签名器 attestation 质量降级（spinsnow 服务级挑战过期实测）——
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
                if target_acc is None and account_svc:
                    target_acc = account_svc.get_active_account()
                if (
                    account_svc is not None
                    and target_acc is not None
                    and hasattr(account_svc, "rotate_account_cookies")
                ):
                    try:
                        session = getattr(client, "session", None)
                        await account_svc.rotate_account_cookies(target_acc.id, session)
                    except Exception:
                        pass
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
                logger.debug("401 Cookie 轮换探测异常: %s", rot_err)

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
    if is_transport_error(exc):
        logger.warning(
            "传输或签名异常，切换账号重试 (%d/%d): %s",
            attempt + 1,
            get_max_retries(),
            exc,
        )
        if not has_yielded_data and await try_switch_account(
            model=target_model, failed_account_id=failed_id
        ):
            logger.info("已切换账号重试 (%d/%d)", attempt + 1, get_max_retries())
            return True
        if is_transport_error(exc):
            runtime_state.record(target_model, "errors")
            record_rotator_event(
                "error", model=target_model, account_id=serving_account_id
            )
            logger.warning("上游传输中断 (502): %s", exc)
            raise HTTPException(
                502, detail={"message": str(exc), "type": "upstream_error"}
            ) from exc
    if isinstance(exc, RequestError):
        clean_msg = clean_upstream_error_message(str(exc))
        status_code = exc.status if exc.status > 0 else 500
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
    total_attempts = get_max_retries() + AUTH_403_MAX_RETRIES
    # 本次请求实际服务的账号（3.9）：在每次尝试发送前刷新，收尾记账用它，
    # 避免并发请求切号后把结果记到无关账号头上。
    serving_account_id: str | None = None

    for attempt in range(total_attempts):
        await ensure_active_account(attempt, model=model_path)
        _svc = runtime_state.account_service
        _acc = _svc.get_active_account() if _svc else None
        serving_account_id = _acc.id if _acc else None
        auth_manager.serving_account_id = serving_account_id
        normalized = None
        try:
            normalized = normalize_gemini_request(req, model_path)
            at_ctx = prepare_anti_truncation(normalized, override=anti_truncation)
            logger.info(
                "非流式请求: model=%s, 轮数=%d, 尝试=%d/%d",
                normalized.model,
                len(req.contents),
                attempt + 1,
                total_attempts,
            )
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
            if await handle_attempt_exception(
                exc,
                attempt=attempt,
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


def format_sse_event(
    event_type: str,
    text: object,
    *,
    model_version: str | None = None,
    response_id: str | None = None,
    thought_signature: str | None = None,
) -> str | None:
    """Format a single Gemini streaming event into SSE chunk string."""
    if not text:
        return None

    parts: list[dict[str, object]] = []
    if event_type == "body":
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
        if thought_signature and parts:
            parts[0]["thoughtSignature"] = thought_signature
    elif event_type == "images":
        img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", images=img_list)
        ]
        if thought_signature and parts:
            parts[0]["thoughtSignature"] = thought_signature
    elif event_type == "reasoning_images":
        r_img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", reasoning_images=r_img_list)
        ]
        if thought_signature and parts:
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
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def format_sse_usage(
    final_usage: dict[str, object] | None,
    *,
    finish_reason: str = "STOP",
    model_version: str | None = None,
    response_id: str | None = None,
) -> str:
    """Format final completion usage metadata into SSE chunk string."""
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
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def format_sse_error(exc: Exception) -> str:
    """Format exception into standard Gemini SSE error chunk."""
    code, msg, status_str = classify_gemini_error_payload(exc)
    return (
        "data: "
        + json.dumps(
            {
                "error": {
                    "code": code,
                    "message": msg,
                    "status": status_str,
                }
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )


_ERROR_TEXT_PREFIX = "[aistudio-api 请求失败"


def format_sse_error_text(exc: Exception) -> str | None:
    """把失败原因格式化为可见文本 part 的 SSE 事件。

    背景：Chatbox 等客户端的 Gemini 流式解析器只读
    ``candidates[].content.parts[].text``，会直接忽略 ``{"error": ...}`` 事件，
    导致失败时表现为“空回啥都没有”。此事件把具体错误原因作为正常文本
    part 下发，任何客户端都能看到，避免静默空回。
    """
    code, msg, status_str = classify_gemini_error_payload(exc)
    if not msg:
        msg = type(exc).__name__
    text = f"{_ERROR_TEXT_PREFIX} {code} {status_str}] {msg}"
    return format_sse_event("body", text)


def _build_gemini_streaming_response(
    *,
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
    anti_truncation: bool | None = None,
    force_buffer: bool = False,
) -> StreamingResponse:
    async def stream_response():
        normalized = None
        stream_start_time = time.perf_counter()
        has_yielded_any = False
        try:
            logger.info(
                "流式请求: model=%s, 轮数=%d",
                model_path,
                len(req.contents),
            )
            final_usage: dict[str, object] | None = None
            final_finish_reason: str = "STOP"
            latest_response_id: str | None = None
            buffered_thought_sig: str | None = None
            stream_completed = False
            last_stream_error: Exception | None = None
            # 预先初始化，避免循环零次执行时下方引用被判 possibly-unbound
            has_yielded_data = False
            auth_manager = Auth403RetryManager()
            total_attempts = get_max_retries() + AUTH_403_MAX_RETRIES
            # 本次请求实际服务的账号（3.9）：同非流式路径
            serving_account_id: str | None = None
            for stream_attempt in range(total_attempts):
                has_yielded_data = False
                last_stream_error = None
                final_usage = None
                final_finish_reason = "STOP"
                latest_response_id = None
                buffered_thought_sig = None
                await ensure_active_account(stream_attempt, model=model_path)
                _svc = runtime_state.account_service
                _acc = _svc.get_active_account() if _svc else None
                serving_account_id = _acc.id if _acc else None
                auth_manager.serving_account_id = serving_account_id
                normalized = normalize_gemini_request(req, model_path)
                at_ctx = prepare_anti_truncation(normalized, override=anti_truncation)
                try:
                    if force_buffer:
                        # 非流式抗截断变体：上游改走缓冲（非流式）传输，
                        # 整体还原后打包为单条完整 SSE 事件下发。
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
                        )
                        auth_manager.current_prepared = None
                        restore_anti_truncation_output(output, at_ctx)
                        final_usage = (
                            output.usage if isinstance(output.usage, dict) else None
                        )
                        final_finish_reason = to_gemini_finish_reason(
                            output.candidates[0].finish_reason
                            if output.candidates
                            else None
                        )
                        latest_response_id = output.response_id
                        buffered_resp = _build_gemini_response_from_output(
                            output, normalized.model
                        )
                        # 与非流式路径（空回防护）对齐（3.8）：-at 缓冲变体
                        # 仅在真正产出内容时才算 has_yielded_data，否则上游
                        # 200 但零内容的空回会被记为 success 且跳过诊断注入，
                        # 被风控软封的账号健康度虚高。
                        buffered_has_output = bool(
                            output.text
                            or output.function_calls
                            or output.function_responses
                            or output.images
                            or output.reasoning_images
                        )
                        has_yielded_data = buffered_has_output
                        has_yielded_any = True
                        # 空回防护（3.8）：缓冲变体零内容时同样注入可见诊断文本
                        if not buffered_has_output:
                            logger.warning(
                                "抗截断缓冲变体返回 200 但内容为空（可能是风控拦截）: "
                                "model=%s, finish=%s",
                                normalized.model,
                                final_finish_reason,
                            )
                            if buffered_resp.candidates:
                                buffered_resp.candidates[0].content.parts.append(
                                    GeminiPartResponse(
                                        text=(
                                            "[aistudio-api 空回诊断] 上游返回 200 但内容为空 "
                                            f"(finish={final_finish_reason})。"
                                            "可能为账号风控拦截或安全策略，请在面板“运行日志”页查看详情。"
                                        )
                                    )
                                )
                        yield (
                            "data: "
                            + json.dumps(
                                buffered_resp.model_dump(
                                    mode="json", exclude_none=True
                                ),
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        stream_completed = True
                        break
                    upstream_events = client.stream_generate_content(
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
                    )
                    # 重试 continue / 客户端断连时，async for 不会自动关闭被迭代
                    # 的异步生成器，只能等 GC 的 asyncgen finalizer 释放上游连接；
                    # 高断连/重试率下会积压未释放连接。这里持引用并用
                    # try/finally 确定性关闭（与 compat 适配器同标准）。
                    anti_stream = restore_anti_truncation_stream(
                        upstream_events, at_ctx
                    )
                    try:
                        async for event_type, text in anti_stream:
                            if event_type == "usage":
                                final_usage = text if isinstance(text, dict) else None
                                observation = current_request.get()
                                if observation is not None and final_usage is not None:
                                    observation.usage = dict(final_usage)
                            elif event_type == "finish_reason":
                                if text not in _IGNORED_WIRE_FINISH_REASONS:
                                    final_finish_reason = (
                                        to_gemini_finish_reason(text)
                                        if isinstance(text, int)
                                        else str(text)
                                    )
                            elif event_type == "response_id":
                                latest_response_id = str(text)
                            elif event_type == "thought_signature":
                                buffered_thought_sig = str(text)
                            else:
                                chunk = format_sse_event(
                                    event_type,
                                    text,
                                    model_version=normalized.model,
                                    response_id=latest_response_id,
                                    thought_signature=buffered_thought_sig,
                                )
                                buffered_thought_sig = None
                                if chunk:
                                    record_output_timing(event_type)
                                    if not has_yielded_data:
                                        ttft_ms = (
                                            time.perf_counter() - stream_start_time
                                        ) * 1000
                                        logger.info(
                                            "流式首包到达: model=%s, ttft=%.1fms",
                                            normalized.model,
                                            ttft_ms,
                                        )
                                    has_yielded_data = True
                                    has_yielded_any = True
                                    yield chunk
                        auth_manager.current_prepared = None
                        stream_completed = True
                        break
                    finally:
                        # 正常完成 / 异常重试 / 客户端断连部走这里，
                        # 确定性释放上游连接（关闭包装器会连带关闭内层流）。
                        await _close_stream_quietly(anti_stream)
                except Exception as exc:
                    last_stream_error = exc
                    observation = current_request.get()
                    if observation and observation.account_id:
                        serving_account_id = observation.account_id
                        auth_manager.serving_account_id = serving_account_id
                    if (
                        isinstance(exc, AuthError)
                        and not isinstance(exc, SessionExpiredError)
                        and not has_yielded_data
                    ):
                        target_m = normalized.model if normalized else model_path
                        if await auth_manager.handle_403(
                            exc,
                            model=target_m,
                            client=client,
                            has_yielded_data=has_yielded_data,
                        ):
                            continue
                        raise
                    if await handle_attempt_exception(
                        exc,
                        attempt=stream_attempt,
                        model_path=model_path,
                        normalized_model=normalized.model if normalized else None,
                        has_yielded_data=has_yielded_data,
                        serving_account_id=serving_account_id,
                    ):
                        continue
                    raise

            if not stream_completed and last_stream_error is not None:
                raise last_stream_error
            target_model = normalized.model if normalized else model_path
            # 空回防护：流式完成但未产出任何内容时，注入可见诊断文本，
            # 避免客户端收到 200 却“空回啥都没有”无从排查
            if stream_completed and not has_yielded_data and not force_buffer:
                logger.warning(
                    "流式完成但未产生任何内容（可能是上游风控拦截或安全策略）: "
                    "model=%s, finish=%s",
                    target_model,
                    final_finish_reason,
                )
                diagnostic = (
                    "[aistudio-api 空回诊断] 上游返回 200 但内容为空 "
                    f"(finish={final_finish_reason})。"
                    "可能为账号风控拦截或安全策略，请在面板“运行日志”页查看详情。"
                )
                diagnostic_chunk = format_sse_event("body", diagnostic)
                if diagnostic_chunk:
                    has_yielded_any = True
                    yield diagnostic_chunk
            # 与空回防护一致：零内容不能记为 success（见上方流式空回诊断）
            observation = current_request.get()
            if observation and observation.account_id:
                serving_account_id = observation.account_id
            stream_event = "success" if has_yielded_data else "error"
            record_rotator_event(
                stream_event,
                model=target_model,
                usage=final_usage,
                account_id=serving_account_id,
            )
            # 签名器健康信号：已产出数据即当次 proof 通过了上游
            # attestation，解除熔断（半开探测语义，见 waa/health.py）。
            if has_yielded_data:
                get_signer_health().record_proof_accepted(serving_account_id)
            if normalized is not None:
                runtime_state.record(
                    normalized.model,
                    stream_event,
                    usage=final_usage,
                    finish_reason=final_finish_reason,
                )
            elapsed_total_ms = (time.perf_counter() - stream_start_time) * 1000
            pt = final_usage.get("prompt_tokens") if final_usage else 0
            ct = final_usage.get("completion_tokens") if final_usage else 0
            logger.info(
                "流式完成: model=%s, 耗时=%.1fms, finish=%s, in=%s, out=%s",
                target_model,
                elapsed_total_ms,
                final_finish_reason,
                pt,
                ct,
            )
            yield format_sse_usage(
                final_usage,
                finish_reason=final_finish_reason,
                model_version=target_model,
                response_id=latest_response_id,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - stream_start_time) * 1000
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
            # 错误透传：尚未输出任何内容时，把具体失败原因作为可见文本 part
            # 下发，避免 Chatbox 等客户端只忽略 {"error":...} 事件而表现为空回；
            # 已输出部分内容时不追加，保持正文纯净（仅下发结构化错误事件）。
            if not has_yielded_any:
                error_text_chunk = format_sse_error_text(exc)
                if error_text_chunk:
                    yield error_text_chunk
            yield format_sse_error(exc)

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    if not effective_contents and req.generateContentRequest is not None:
        effective_contents = req.generateContentRequest.contents
    if not effective_contents:
        raise HTTPException(
            400, detail={"message": "contents is required", "type": "bad_request"}
        )

    normalized_req = GeminiGenerateContentRequest(contents=effective_contents)

    for attempt in range(2):
        await ensure_active_account(attempt, model=clean_model)
        try:
            normalized = normalize_gemini_request(normalized_req, clean_model)
            total = await client.count_tokens(
                model=normalized.model,
                contents=normalized.contents,
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
