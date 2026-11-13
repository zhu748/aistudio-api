"""Streaming workflow for chat completions."""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncGenerator

from aistudio_api.domain.errors import (
    RequestError,
    SessionExpiredError,
    TransportError,
    classify_error,
    is_html_payload,
    is_login_redirect_payload,
)
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.session import GatewaySession
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
)
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
    ) -> AsyncGenerator[tuple[str, object | None], None]:
        if not request:
            raise ValueError("prepared request is required")
        if self._session is None:
            raise RuntimeError("gateway session is required for streaming")
        request_body = request.body
        logger.debug("开始流式解析: model=%s, body_len=%d", model, len(request_body))
        parser = IncrementalJSONStreamParser()
        utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
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
        has_received_chunks = False
        status_code = 0

        def emit_parsed_chunk(
            parsed_chunk: list[object],
        ) -> list[tuple[str, object | None]]:
            nonlocal latest_usage, latest_finish_reason, latest_response_id
            events: list[tuple[str, object | None]] = []
            usage = parse_chunk_usage(parsed_chunk)
            if usage:
                latest_usage = usage
            if (
                isinstance(parsed_chunk, list)
                and len(parsed_chunk) > WIRE_RESPONSE_ID_INDEX
                and isinstance(parsed_chunk[WIRE_RESPONSE_ID_INDEX], str)
                and parsed_chunk[WIRE_RESPONSE_ID_INDEX]
            ):
                latest_response_id = str(parsed_chunk[WIRE_RESPONSE_ID_INDEX])
                events.append(("response_id", latest_response_id))
            candidate = parse_response_chunk(parsed_chunk)
            if candidate.finish_reason is not None and candidate.finish_reason not in (
                0,
                WIRE_FINISH_REASON_CONTINUING,
            ):
                latest_finish_reason = candidate.finish_reason
            if candidate.thinking:
                events.append(("thinking", candidate.thinking))
            if candidate.reasoning_images:
                events.append(("reasoning_images", candidate.reasoning_images))
            if candidate.function_calls:
                events.append(("tool_calls", candidate.function_calls))
            if candidate.images:
                events.append(("images", candidate.images))
            if candidate.code_output:
                # 代码执行输出（executableCode/codeExecutionResult 文本）：
                # 缺此分支时纯代码 chunk 的 events 为空，内容静默丢失
                # （非流式 parse_text_output 保留，两路径不对称）
                events.append(("body", candidate.code_output))
            if candidate.text:
                events.append(("body", candidate.text))
            if candidate.thought_signature:
                events.append(("thought_signature", candidate.thought_signature))
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
                        text_payload = utf8_decoder.decode(payload, final=False)
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
                        # 非 200 的错误体无需逐块解析（解析器只为正常流服务，
                        # 白扫 64KB 错误页纯浪费 CPU，且理论上可能把凑巧结构
                        # 下发为事件）；错误分类走末尾的 raw_response 路径。
                        for parsed_chunk in parser.feed(text_payload):
                            for event in emit_parsed_chunk(parsed_chunk):
                                emitted_any_chunk = True
                                yield event
        finally:
            # 正常结束 / 异常 / 客户端断连（GeneratorExit）都走这里：
            # 确定性关闭 transport 生成器，触发其 finally 的 resp.aclose()。
            await upstream.aclose()
        trailing_text = utf8_decoder.decode(b"", final=True)
        if trailing_text:
            if status_code != 200 and raw_parts_total < 65536:
                raw_parts.append(trailing_text)
                raw_parts_total += len(trailing_text)
            if status_code == 200:
                for parsed_chunk in parser.feed(trailing_text):
                    for event in emit_parsed_chunk(parsed_chunk):
                        emitted_any_chunk = True
                        yield event

        for parsed_chunk in parser.finish():
            for event in emit_parsed_chunk(parsed_chunk):
                emitted_any_chunk = True
                yield event
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
        else:
            logger.warning(
                "流式响应结束但未收到上游 finish_reason（无内容输出）: model=%s", model
            )
            yield ("finish_reason", 1)
        yield ("usage", latest_usage)
        yield ("done", None)
