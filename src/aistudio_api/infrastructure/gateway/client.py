"""AI Studio client gateway."""

from __future__ import annotations

import contextvars
import hashlib
import json
from dataclasses import dataclass

from aistudio_api.config import DEFAULT_TEXT_MODEL
from aistudio_api.domain.errors import (
    RequestError,
    SessionExpiredError,
    TransportError,
    classify_error,
    is_html_payload,
    is_login_redirect_payload,
)
from aistudio_api.domain.models import ModelOutput, ResponsePart
from aistudio_api.infrastructure.account.cookie_parser import get_visit_id
from aistudio_api.infrastructure.gateway.model_turns import (
    HistorySignatureMigration,
    ModelTurnContext,
    ModelTurnHistory,
)
from aistudio_api.infrastructure.gateway.session import (
    COUNT_TOKENS_URL,
    DEFAULT_UPSTREAM_HEADERS,
    GENERATE_CONTENT_URL,
    GatewaySession,
    SessionContext,
)
from aistudio_api.infrastructure.gateway.wire_codec import (
    encode_request_body,
)
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.request_context import current_request
from aistudio_api.infrastructure.signature_trace import get_signature_trace
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("client")


@dataclass(slots=True)
class PreparedRequest:
    url: str
    headers: dict[str, str]
    body: str
    model: str = ""
    snapshot: str = ""
    # 当前用户轮文本（页面提示词）：在 prepare_request 从 typed contents
    # 一次性提取，供 CountTokens 补发复用，避免事后重新解析 MB 级 body。
    page_prompt: str = ""
    ctx: SessionContext | None = None
    model_turn: ModelTurnContext | None = None


# Per-task storage: AIStudioClient is a process-wide singleton, so a plain
# instance attribute would let concurrent requests overwrite each other's
# signed request and replay the wrong user's body/proof on a 403 retry.
_LAST_PREPARED_REQUEST: contextvars.ContextVar[PreparedRequest | None] = (
    contextvars.ContextVar("aistudio_last_prepared_request", default=None)
)


def _page_prompt_from_contents(contents: list[AistudioContent]) -> str:
    """从已还原的请求 contents 提取当前用户轮文本（typed 版 page prompt）。

    与 wire 形态解析语义一致：最后一个 user 轮的首个 part 为文本 part
    时返回该文本，否则继续向前找；找不到返回空串（跳过后续补发）。
    """
    for content in reversed(contents):
        if content.role != "user" or not content.parts:
            continue
        text = content.parts[0].text
        if text is not None:
            return text
    return ""


def _apply_function_name_restore(
    calls: object,
    mapping: dict[str, str] | None,
) -> None:
    """把上游回传的截短函数名换回客户端声明原名（原地修改）。

    AIStudio2API restoreFunctionNames 移植：映射仅含本请求内超长截短
    过的声明，普通名不命中、零开销。session 快照（raw_parts）保存的是
    上游原始 wire 形态，不经过本函数，恢复后回放仍与声明侧截短名一致。
    """
    if not mapping or not isinstance(calls, list):
        return
    for call in calls:
        if isinstance(call, dict):
            original = mapping.get(str(call.get("name") or ""))
            if original is not None:
                call["name"] = original


class AIStudioClient:
    def __init__(self):
        from aistudio_api.infrastructure.gateway.streaming import StreamingGateway

        self._session = GatewaySession()
        self._streaming_gateway = StreamingGateway(session=self._session)
        self._model_turns = ModelTurnHistory()

    @property
    def last_prepared_request(self) -> PreparedRequest | None:
        """Return the signed request prepared by the *current* asyncio task."""
        return _LAST_PREPARED_REQUEST.get()

    @last_prepared_request.setter
    def last_prepared_request(self, value: PreparedRequest | None) -> None:
        _LAST_PREPARED_REQUEST.set(value)

    @property
    def session(self) -> GatewaySession:
        """Expose active gateway session facade."""
        return self._session

    async def switch_auth(self, auth_file: str | None) -> None:
        if self._session is not None:
            await self._session.switch_auth(auth_file)

    async def close(self) -> None:
        """关闭会话与传输层连接。"""
        try:
            if self._session is not None:
                await self._session.close()
        finally:
            self._model_turns.clear()

    async def prepare_request(
        self,
        prompt: str = "",
        model: str = DEFAULT_TEXT_MODEL,
        images: list[tuple[str, str]] | None = None,
        contents: list[AistudioContent] | None = None,
        system_instruction: str | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        tool_config: list[object] | None = None,
        safety_settings: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        signature_migration: HistorySignatureMigration | None = None,
    ) -> PreparedRequest:
        ctx = await self._session.ensure_ready()
        effective_contents = contents or [
            AistudioContent.from_user_prompt(prompt=prompt, images=images)
        ]
        observation = current_request.get()
        scope = hashlib.sha256(
            json.dumps(
                [
                    observation.key_id if observation else "",
                    ctx.auth_file,
                    ctx.auth_user,
                    model.removeprefix("models/"),
                    system_instruction_content.to_wire()
                    if system_instruction_content is not None
                    else system_instruction,
                    tools,
                    tool_config,
                ],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        trace = get_signature_trace(create=True)
        if trace is not None:
            trace.reset_attempt()
        effective_contents, turn_token, model_turn = self._model_turns.prepare(
            scope, effective_contents, migration=signature_migration
        )
        if trace is not None:
            trace.record_upstream_out(
                effective_contents, continuation_sent=bool(turn_token)
            )
            trace.emit_outbound_log()
        snapshot = await self._session.generate_snapshot(
            effective_contents, ctx=ctx, model=model
        )
        # Only the matched conversation's last model turn supplies slot 11.
        # Restoration precedes WAA signing, so proof and contents stay aligned.
        # token 在唯一一次编码时写入槽 11（不再整体二次 loads/dumps）。
        body = encode_request_body(
            model=model,
            prompt=prompt,
            contents=effective_contents,
            system_instruction=system_instruction,
            system_instruction_content=system_instruction_content,
            tools=tools,
            tool_config=tool_config,
            safety_settings=safety_settings,
            images=images,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            snapshot=snapshot,
            session_token=turn_token,
        )
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_local_waa_server,
        )

        active_waa = get_local_waa_server()
        headers = dict(DEFAULT_UPSTREAM_HEADERS)
        if active_waa.get("user_agent"):
            headers["User-Agent"] = str(active_waa["user_agent"])
        server_hdrs = active_waa.get("headers")
        if isinstance(server_hdrs, dict):
            for k, v in server_hdrs.items():
                headers[str(k)] = str(v)
        dyn_headers = getattr(self._session, "last_signer_headers", None)
        if dyn_headers:
            # 指纹白名单过滤（与 transport 层同一允许集）：签名服务返回的
            # 动态头只保留 AI Studio 逆向指纹头名（User-Agent /
            # X-Goog-Api-Key / X-AIStudio-Visit-Id / X-Goog-Ext-...-bin 等），
            # 其余一律剥除——签名服务若被配置为回显额外头（代理头、自定义
            # 头），不得混入上游指纹集。
            from aistudio_api.infrastructure.gateway.transport import (
                strip_to_aistudio_fingerprint,
            )

            headers.update(strip_to_aistudio_fingerprint(dict(dyn_headers)))
        headers["X-Goog-AuthUser"] = ctx.auth_user
        # X-AIStudio-Visit-Id 会话级复用（HAR 实测：真实浏览器对同账号
        # 每次 GenerateContent 均携带，且同页面会话内恒定不变）。
        headers["X-AIStudio-Visit-Id"] = get_visit_id(
            f"account:{ctx.auth_file or 'default'}"
        )
        prepared = PreparedRequest(
            url=GENERATE_CONTENT_URL,
            headers=headers,
            body=body,
            model=model,
            snapshot=snapshot,
            page_prompt=_page_prompt_from_contents(effective_contents),
            ctx=ctx,
            model_turn=model_turn,
        )
        logger.debug(
            "请求签名完成: model=%s, proof_len=%d, body_len=%d",
            prepared.model,
            len(prepared.snapshot),
            len(prepared.body),
        )
        self.last_prepared_request = prepared
        return prepared

    async def count_tokens(
        self,
        *,
        model: str,
        contents: list[AistudioContent],
    ) -> int:
        """调用上游 CountTokens（无需 WAA snapshot，鉴权链同 GenerateContent）。

        响应数组 [total, [], [[[prompt], 1]], null, null, [[1, total]]] 的
        首位即 AI Studio 界面展示的 token 总数（含 1 个固定开销 token）。
        """
        ctx = await self._session.ensure_ready()
        normalized_model = model if model.startswith("models/") else f"models/{model}"
        body = json.dumps(
            [normalized_model, [c.to_wire() for c in contents]],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        headers = dict(DEFAULT_UPSTREAM_HEADERS)
        headers["X-Goog-AuthUser"] = ctx.auth_user
        status, raw = await self._session.send_request(
            body=body,
            url=COUNT_TOKENS_URL,
            headers=headers,
            ctx=ctx,
        )
        raw_text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise classify_error(status, raw_text)
        if is_login_redirect_payload(raw_text):
            raise SessionExpiredError("登录态已失效: 被重定向至 Google 登录页")
        try:
            parsed = json.loads(raw_text.strip().removeprefix(")]}'").lstrip())
            total = parsed[0] if isinstance(parsed, list) and parsed else None
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                return total
        except json.JSONDecodeError:
            pass
        error = classify_error(200, raw_text)
        if not isinstance(error, RequestError) or error.status != 200:
            raise error
        raise TransportError(
            f"upstream CountTokens returned malformed body: {raw_text[:200]}"
        )

    async def stream_generate_content(
        self,
        *,
        model: str = DEFAULT_TEXT_MODEL,
        contents: list[AistudioContent] | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        tool_config: list[object] | None = None,
        safety_settings: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        prepared: PreparedRequest | None = None,
        function_name_restore: dict[str, str] | None = None,
        signature_migration: HistorySignatureMigration | None = None,
    ):
        effective_prep = prepared or await self.prepare_request(
            model=model,
            contents=contents,
            system_instruction_content=system_instruction_content,
            tools=tools,
            tool_config=tool_config,
            safety_settings=safety_settings,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            signature_migration=signature_migration,
        )
        # aclosing：上层重试丢弃本生成器时确定性关闭流式网关生成器，
        # 逐层触发 aclose 链（session → transport → resp.aclose）。
        gateway_stream = self._streaming_gateway.stream_chat(
            request=effective_prep,
            model=model,
        )
        try:
            async for event in gateway_stream:
                if function_name_restore and event[0] == "tool_calls":
                    _apply_function_name_restore(event[1], function_name_restore)
                elif (
                    function_name_restore
                    and event[0] == "ordered_parts"
                    and isinstance(event[1], list)
                ):
                    _apply_function_name_restore(
                        [
                            part.function_call
                            for part in event[1]
                            if isinstance(part, ResponsePart)
                            and part.function_call is not None
                        ],
                        function_name_restore,
                    )
                yield event
        finally:
            await gateway_stream.aclose()

    async def generate_content(
        self,
        *,
        model: str = DEFAULT_TEXT_MODEL,
        contents: list[AistudioContent] | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        tool_config: list[object] | None = None,
        safety_settings: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        prepared: PreparedRequest | None = None,
        function_name_restore: dict[str, str] | None = None,
        signature_migration: HistorySignatureMigration | None = None,
    ) -> ModelOutput:
        if prepared is not None:
            effective_prep = prepared
            logger.debug("重放已签名请求: model=%s", model)
        else:
            logger.debug("构建生成请求: model=%s", model)
            effective_prep = await self.prepare_request(
                model=model,
                contents=contents,
                system_instruction_content=system_instruction_content,
                tools=tools,
                tool_config=tool_config,
                safety_settings=safety_settings,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_tokens=max_tokens,
                generation_config_overrides=generation_config_overrides,
                signature_migration=signature_migration,
            )

        status, raw = await self._session.send_request(
            body=effective_prep.body,
            url=effective_prep.url,
            headers=effective_prep.headers,
            ctx=effective_prep.ctx,
        )
        raw_text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise classify_error(status, raw_text)
        if not raw_text.strip():
            raise TransportError("empty 200 body (connection truncated)")
        if is_login_redirect_payload(raw_text):
            raise SessionExpiredError("登录态已失效: 被重定向至 Google 登录页")
        if is_html_payload(raw_text):
            raise TransportError("upstream returned HTML error page on HTTP 200")
        # 真实页面行为：GenerateContent 发出 2.2s 后补一次 CountTokens
        if effective_prep.url.rstrip("/").endswith("/GenerateContent"):
            scheduler = getattr(
                self._session, "schedule_browser_counttokens_followup", None
            )
            if callable(scheduler):
                scheduler(
                    effective_prep.ctx,
                    model,
                    effective_prep.page_prompt,
                )
        output = parse_text_output(raw_text)
        output.model = model
        if function_name_restore:
            # 换回截短前的原名：签名缓存 remember/下游协议层全部以客户端
            # 声明名为键，raw_parts（session 快照）保持上游原始形态
            for candidate in output.candidates:
                _apply_function_name_restore(
                    candidate.function_calls, function_name_restore
                )
        trace = get_signature_trace()
        if trace is not None and output.candidates:
            trace.reset_upstream_in()
            if ordered := output.candidates[0].ordered_parts:
                trace.record_upstream_in_parts(ordered)
        if (
            effective_prep.model_turn is not None
            and output.candidates
            and not output.candidates[0].tool_interrupted
        ):
            capture = effective_prep.model_turn.capture()
            capture.add(output.candidates[0].raw_parts, output.response_id)
            capture.finish(output.candidates[0].finish_reason)
        elif trace is not None and output.candidates:
            note = (
                "tool_interrupted"
                if output.candidates[0].tool_interrupted
                else "scope_incompatible"
            )
            trace.record_cache_store(
                cached_warm=False,
                cached_cold=False,
                status="skipped",
                note=note,
            )
        return output


__all__ = ["AIStudioClient", "PreparedRequest"]
