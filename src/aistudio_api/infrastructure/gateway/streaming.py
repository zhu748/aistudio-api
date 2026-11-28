"""Streaming workflow for chat completions."""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncGenerator, Callable

from aistudio_api.domain.errors import (
    RequestError,
    SessionExpiredError,
    TransportError,
    classify_error,
    is_html_payload,
    is_login_redirect_payload,
)
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.session import (
    GatewaySession,
)
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.wire_parser import (
    WIRE_RESPONSE_ID_INDEX,
    parse_chunk_usage,
    parse_response_chunk,
)
from aistudio_api.infrastructure.gateway.wire_spec import (
    WIRE_FINISH_REASON_CONTINUING,
    WIRE_FINISH_REASON_STOP,
)
from aistudio_api.infrastructure.signature_trace import get_signature_trace
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("streaming")


def _summarize_error_body(raw_response: str, limit: int = 500) -> str:
    text = raw_response.strip()
    if not text:
        return ""

    try:
        payload = json.loads(text)
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = " ".join(text.split())

    if len(compact) > limit:
        return compact[:limit] + "..."
    return compact


class StreamingGateway:
    def __init__(self, session: GatewaySession | None = None):
        self._session = session

    async def stream_chat(
        self,
        *,
        request: PreparedRequest,
        model: str,
        on_usage: Callable[[dict[str, object]], None] | None = None,
    ) -> AsyncGenerator[tuple[str, object | None], None]:
        if not request:
            raise ValueError("prepared request is required")
        if self._session is None:
            raise RuntimeError("gateway session is required for streaming")
        request_body = request.body
        logger.debug("开始流式解析: model=%s, body_len=%d", model, len(request_body))
        capture = (
            request.model_turn.capture() if request.model_turn is not None else None
        )
        trace = get_signature_trace()
        if trace is not None:
            trace.reset_upstream_in()
        # 真实页面行为：GenerateContent 发出约 2.2s 后补一次 CountTokens
        # （当前用户轮次；Go 签名器 SendProtected 同源行为）。page_prompt
        # 在 prepare_request 已从 typed contents 提取，无需重新解析 body。
        # 延迟到首个 2xx 数据块后再调度：429/403/立即断连的失败尝试
        # （含账号轮换重试）不应对同一受限账号再补一发 CountTokens，
        # 限流风暴下会放大上游压力（非流式 generate_content 已是
        # "确认 200 后才调度"的对称语义）。
        counttokens_scheduler = None
        if request.url.rstrip("/").endswith("/GenerateContent"):
            scheduler = getattr(
                self._session, "schedule_browser_counttokens_followup", None
            )
            if callable(scheduler):
                counttokens_scheduler = scheduler
        parser = IncrementalJSONStreamParser()
        utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        latest_usage: dict[str, object] | None = None
        latest_finish_reason: int | None = None
        latest_response_id: str | None = None
        raw_parts: list[str] = []
        raw_parts_total = 0  # 累计长度计数器：避免每个 chunk 都 sum() 全量重算（O(n²)）
        # 头部样本窗口与 is_login_redirect_payload 的扫描窗口（2000）对齐：
        # 若只保留 512 字符，落在 512~2000 区间的 "servicelogin" 特征会漏检，
        # 登录重定向被误判为普通 TransportError 而不触发账号轮换。
        head_sample: str = ""
        HEAD_SAMPLE_LIMIT = 2000
        emitted_any_chunk = False
        tool_interrupted = False
        has_received_chunks = False
        status_code = 0

        def emit_parsed_chunk(
            parsed_chunk: list[object],
            *,
            trusted_terminal: bool = True,
        ) -> list[tuple[str, object | None]]:
            nonlocal latest_usage, latest_finish_reason
            nonlocal latest_response_id, emitted_any_chunk, tool_interrupted
            events: list[tuple[str, object | None]] = []
            usage = parse_chunk_usage(parsed_chunk)
            if usage:
                latest_usage = usage
                if on_usage is not None:
                    on_usage(usage)
            if (
                isinstance(parsed_chunk, list)
                and len(parsed_chunk) > WIRE_RESPONSE_ID_INDEX
                and isinstance(parsed_chunk[WIRE_RESPONSE_ID_INDEX], str)
                and parsed_chunk[WIRE_RESPONSE_ID_INDEX]
            ):
                latest_response_id = str(parsed_chunk[WIRE_RESPONSE_ID_INDEX])
                events.append(("response_id", latest_response_id))
            candidate = parse_response_chunk(parsed_chunk)
            if capture is not None:
                capture.add(candidate.raw_parts, latest_response_id)
            if trace is not None and candidate.ordered_parts:
                trace.record_upstream_in_parts(candidate.ordered_parts)
            finish = candidate.finish_reason if trusted_terminal else None
            if finish not in (
                None,
                0,
                WIRE_FINISH_REASON_CONTINUING,
                WIRE_FINISH_REASON_STOP,
            ):
                # Do not let a STOP in a later chunk certify an earlier
                # interrupted tool set. These chunks do not expose separate
                # candidate identities to downstream protocols.
                tool_interrupted = True
                events.append(("tool_interruption", None))
            parts = candidate.ordered_parts or []
            if (
                len(parts) == 1
                and (part := parts[0]).text
                and not (
                    part.image
                    or part.function_call
                    or part.function_response
                    or part.thought_signature
                    or part.executable_code is not None
                    or part.code_execution_result is not None
                )
            ):
                events.append(("thinking" if part.thought else "body", part.text))
            elif parts:
                # Don't emit an aggregate candidate signature: only the owner
                # Part (or an actual standalone carrier) is sent downstream.
                events.append(("ordered_parts", parts))
            if (
                candidate.has_content
                or candidate.thinking
                or candidate.reasoning_images
            ):
                emitted_any_chunk = True
                # New output needs its own terminal reason; an earlier STOP
                # cannot certify a later truncated continuation.
                latest_finish_reason = None
            if (
                trusted_terminal
                and candidate.finish_reason is not None
                and candidate.finish_reason
                not in (
                    0,
                    WIRE_FINISH_REASON_CONTINUING,
                )
            ):
                latest_finish_reason = candidate.finish_reason
            return events

        # aclosing：客户端断连/上层重试丢弃本生成器时，async for 不会自动
        # 关闭被迭代的异步生成器，上游 httpx 连接只能等 GC 释放；显式关闭
        # 触发 transport 层 finally 的 resp.aclose()，确定性归还连接。
        upstream = self._session.send_streaming_request(
            body=request_body,
            url=request.url,
            headers=request.headers,
            ctx=request.ctx,
        )
        try:
            async for event_type, payload in upstream:
                if event_type == "error":
                    err_msg = (
                        str(payload).strip()
                        if payload
                        else "unknown streaming transport error"
                    )
                    raise TransportError(f"Streaming request failed: {err_msg}")
                if event_type == "status" and payload and not status_code:
                    status_code = int(str(payload))
                elif event_type == "chunk" and payload:
                    has_received_chunks = True
                    if isinstance(payload, bytes):
                        text_payload = (
                            utf8_decoder.decode(payload, final=False)
                            if status_code == 200
                            else payload.decode("utf-8", errors="replace")
                        )
                    else:
                        text_payload = str(payload)
                    if not text_payload:
                        continue
                    if status_code != 200:
                        # 错误响应体可能巨大（整页 HTML），截断累积上限避免内存被
                        # 单个异常响应打爆（诊断只需头部样本）。
                        if raw_parts_total < 65536:
                            raw_parts.append(text_payload)
                            raw_parts_total += len(text_payload)
                    elif len(head_sample) < HEAD_SAMPLE_LIMIT:
                        head_sample += text_payload[
                            : HEAD_SAMPLE_LIMIT - len(head_sample)
                        ]
                    if status_code == 200:
                        # 首个 2xx 数据块到达：此刻才调度浏览器行为补发
                        # （上游确认服务中，补发不再路空；首块一般远早于
                        # 2.2s 的补发时刻，时序仍贴近真实页面）。
                        if counttokens_scheduler is not None:
                            counttokens_scheduler(
                                request.ctx, model, request.page_prompt
                            )
                            counttokens_scheduler = None
                        # 非 200 的错误体无需逐块解析（解析器只为正常流服务，
                        # 白扫 64KB 错误页纯浪费 CPU，且理论上可能把凑巧结构
                        # 下发为事件）；错误分类走末尾的 raw_response 路径。
                        for parsed_chunk in parser.feed(text_payload):
                            for event in emit_parsed_chunk(parsed_chunk):
                                yield event
        except ValueError as exc:
            raise TransportError(f"Invalid upstream stream: {exc}") from exc
        finally:
            # 正常结束 / 异常 / 客户端断连（GeneratorExit）都走这里：
            # 确定性关闭 transport 生成器，触发其 finally 的 resp.aclose()。
            await upstream.aclose()
        try:
            trailing_text = utf8_decoder.decode(b"", final=True)
            if trailing_text and status_code == 200:
                for parsed_chunk in parser.feed(trailing_text):
                    for event in emit_parsed_chunk(parsed_chunk):
                        yield event
            if parser.chunk_start is not None:
                # EOF repair can salvage display text but cannot certify a
                # complete candidate or authorize publishing tool calls.
                latest_finish_reason = None
            for parsed_chunk in parser.finish():
                for event in emit_parsed_chunk(parsed_chunk, trusted_terminal=False):
                    yield event
        except ValueError as exc:
            raise TransportError(f"Invalid upstream stream: {exc}") from exc
        logger.debug("流式读取结束: model=%s, 收到分块=%s", model, has_received_chunks)
        raw_response = "".join(raw_parts)
        if status_code == 0:
            raise TransportError(
                "stream ended before HTTP response status arrived (connection failed)"
            )
        if status_code != 200:
            detail = _summarize_error_body(raw_response)
            if (
                status_code in (401, 403, 429)
                or "[,[" in raw_response
                or "[null,[" in raw_response
                or raw_response.lstrip().startswith("[,")
            ):
                raise classify_error(status_code, raw_response)
            if detail:
                raise RequestError(status_code, detail)
            raise RequestError(status_code, "")
        if not has_received_chunks:
            raise TransportError(
                "stream ended before any data arrived (connection truncated)"
            )
        if not emitted_any_chunk and head_sample:
            if is_login_redirect_payload(head_sample):
                raise SessionExpiredError("登录态已失效: 被重定向至 Google 登录页")
            if is_html_payload(head_sample):
                raise TransportError("upstream returned HTML error page on HTTP 200")
        if not emitted_any_chunk and latest_finish_reason is None:
            error = classify_error(200, head_sample)
            if not isinstance(error, RequestError) or error.status != 200:
                raise error
            raise TransportError(
                "upstream returned no usable content or finish reason on HTTP 200"
            )
        if capture is not None and not tool_interrupted:
            capture.finish(latest_finish_reason)
        elif trace is not None:
            trace.record_cache_store(
                cached_warm=False,
                cached_cold=False,
                status="skipped",
                note="tool_interrupted" if tool_interrupted else "scope_incompatible",
            )
        if latest_finish_reason is not None:
            yield ("finish_reason", latest_finish_reason)
        elif emitted_any_chunk:
            # 流正常结束但从未收到上游 finish_reason：典型场景是连接在
            # 最后一个数据块之前被截断。若仍伪装成 STOP，下游会把被截断的
            # 回答当成完整回答（静默丢字）。此处下发 OTHER（wire 5）让
            # Gemini 协议客户端可感知异常，并记录 WARNING 供告警统计。
            logger.warning(
                "流式响应结束但未收到上游 finish_reason（疑似截断）: "
                "model=%s, 已输出内容=%s, 响应头样本=%r",
                model,
                emitted_any_chunk,
                head_sample[:120],
            )
            yield ("finish_reason", 5)
        yield ("usage", latest_usage)
        yield ("done", None)
