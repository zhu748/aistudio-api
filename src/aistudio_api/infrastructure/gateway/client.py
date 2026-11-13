"""AI Studio client gateway."""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass

from aistudio_api.config import DEFAULT_TEXT_MODEL
from aistudio_api.domain.errors import (
    SessionExpiredError,
    TransportError,
    classify_error,
    is_html_payload,
    is_login_redirect_payload,
)
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.account.cookie_parser import get_visit_id
from aistudio_api.infrastructure.gateway.session import (
    COUNT_TOKENS_URL,
    DEFAULT_UPSTREAM_HEADERS,
    GENERATE_CONTENT_URL,
    GatewaySession,
    SessionContext,
    extract_page_prompt,
)
from aistudio_api.infrastructure.gateway.session_token import (
    account_session_token_scope,
    get_session_token,
    record_session_token,
)
from aistudio_api.infrastructure.gateway.wire_codec import (
    attach_session_token,
    encode_request_body,
)
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("client")


@dataclass(slots=True)
class PreparedRequest:
    url: str
    headers: dict[str, str]
    body: str
    model: str = ""
    snapshot: str = ""
    ctx: SessionContext | None = None


# Per-task storage: AIStudioClient is a process-wide singleton, so a plain
# instance attribute would let concurrent requests overwrite each other's
# signed request and replay the wrong user's body/proof on a 403 retry.
_LAST_PREPARED_REQUEST: contextvars.ContextVar[PreparedRequest | None] = (
    contextvars.ContextVar("aistudio_last_prepared_request", default=None)
)


class AIStudioClient:
    def __init__(self):
        from aistudio_api.infrastructure.gateway.streaming import StreamingGateway

        self._session = GatewaySession()
        self._streaming_gateway = StreamingGateway(session=self._session)

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

    async def warmup(self) -> None:
        """初始化活跃账号会话。"""
        if self._session is not None:
            await self._session.ensure_ready()
            logger.info("网关会话已就绪")

    async def switch_auth(self, auth_file: str | None) -> None:
        if self._session is not None:
            await self._session.switch_auth(auth_file)

    async def close(self) -> None:
        """关闭会话与传输层连接。"""
        if self._session is not None:
            await self._session.close()

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
    ) -> PreparedRequest:
        ctx = await self._session.ensure_ready()
        effective_contents = contents or [
            AistudioContent.from_user_prompt(prompt=prompt, images=images)
        ]
        snapshot = await self._session.generate_snapshot(
            effective_contents, ctx=ctx, model=model
        )
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
        )
        # 浏览器行为对齐：回传上一轮响应携带的会话令牌（wire 槽 11）。
        # 04 号实验实证——在已签名 body 上追加槽 11 不影响 WAA proof
        # （proof 只绑定 contents 摘要），上游 200 接受。
        body = attach_session_token(
            body, get_session_token(account_session_token_scope(ctx.auth_file))
        )
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_active_waa_server,
        )

        active_waa = get_active_waa_server()
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
            ctx=ctx,
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
            parsed = json.loads(raw_text)
            total = parsed[0] if isinstance(parsed, list) and parsed else None
            if isinstance(total, int):
                return total
        except json.JSONDecodeError:
            pass
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
        )
        # aclosing：上层重试丢弃本生成器时确定性关闭流式网关生成器，
        # 逐层触发 aclose 链（session → transport → resp.aclose）。
        gateway_stream = self._streaming_gateway.stream_chat(
            request=effective_prep,
            model=model,
        )
        try:
            async for event in gateway_stream:
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
                    extract_page_prompt(effective_prep.body),
                )
        output = parse_text_output(raw_text)
        output.model = model
        # 会话令牌（响应槽 7 v1_...）：记录供下一轮请求回传 wire 槽 11
        if output.response_id:
            record_session_token(
                account_session_token_scope(
                    effective_prep.ctx.auth_file if effective_prep.ctx else None
                ),
                output.response_id,
            )
        return output


__all__ = ["AIStudioClient", "PreparedRequest"]
