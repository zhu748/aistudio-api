"""OpenAI / Anthropic(Claude) 协议兼容层请求模型。

字段设计保持宽松容错：未知字段一律忽略、content 允许字符串或块数组，
以最大化第三方客户端（OpenAI SDK / Anthropic SDK / one-api / Claude Code 等）
的兼容性。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# OpenAI Chat Completions (/v1/chat/completions)
# ---------------------------------------------------------------------------


class OpenAIChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, object]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, object]] | None = None
    tool_call_id: str | None = None
    # —— 思考历史回传（2026-11-25 thought_signature 全面优化）——
    # DeepSeek 风格 ``reasoning_content`` / OpenRouter ``reasoning``（字符串
    # 形态）是客户端回传网关下发思考摘要的两种主流形态；``extra_content``
    # 为网关在响应消息上原样下发的签名载体（与 tool_calls 同一扩展形态）。
    # 三者一并收进来转为 thought part：思考链历史不再被静默丢弃，签名经
    # 载体精确回读，缺失时由规范化层查 thinking 缓存回注。
    reasoning_content: str | None = None
    reasoning: str | dict[str, object] | None = None
    extra_content: dict[str, object] | None = None
    thought_signature: str | None = None
    thoughtSignature: str | None = None
    provider_specific_fields: dict[str, object] | None = None
    reasoning_details: list[dict[str, object]] | None = None


class OpenAIChatCompletionRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    model: str
    messages: list[OpenAIChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    response_format: dict[str, object] | None = None
    stream: bool = False
    stream_options: dict[str, object] | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: str | dict[str, object] | None = None
    parallel_tool_calls: bool | None = None
    n: int | None = None
    user: str | None = None
    reasoning_effort: str | None = None
    # —— 思考强度的宽松兼容（2026-11：修复各生态客户端形态被静默丢弃）——
    # 不同客户端生态对"思考强度"的传参形态互不兼容，历史上只有顶层
    # ``reasoning_effort`` 被解析，其余形态被 pydantic 静默忽略（表现为
    # "思考强度传不过去"）。这里全部收进来，由 compat_service 统一归一化：
    # - OpenRouter / 新版网关: ``reasoning: {"effort": "high", ...}``
    # - 智谱 GLM: ``thinking: {"type": "enabled", "budget_tokens": N}``
    # - Qwen / DashScope: ``enable_thinking: bool``
    # - vLLM: ``chat_template_kwargs: {"thinking": true}``
    reasoning: dict[str, object] | None = None
    thinking: dict[str, object] | None = None
    enable_thinking: bool | None = None
    chat_template_kwargs: dict[str, object] | None = None


# ---------------------------------------------------------------------------
# OpenAI Responses (/v1/responses)
# ---------------------------------------------------------------------------


class OpenAIResponsesRequest(BaseModel):
    """OpenAI Responses API 请求（宽松容错：未知字段一律忽略）。

    兼容 openai-python / Codex CLI 等 Responses 客户端。``input`` 支持
    字符串或 item 数组（message / function_call / function_call_output /
    reasoning）；``instructions`` 独立于 system 消息映射。
    """

    model_config = ConfigDict(allow_inf_nan=False)

    model: str
    input: str | list[dict[str, object]] | None = None
    instructions: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    tools: list[dict[str, object]] | None = None
    tool_choice: str | dict[str, object] | None = None
    parallel_tool_calls: bool | None = None
    reasoning: dict[str, object] | None = None
    text: dict[str, object] | None = None
    metadata: dict[str, object] | None = None
    store: bool | None = None
    previous_response_id: str | None = None
    truncation: str | None = None
    user: str | None = None
    include: list[str] | None = None


# ---------------------------------------------------------------------------
# Anthropic Messages (/v1/messages)
# ---------------------------------------------------------------------------


class ClaudeMessage(BaseModel):
    role: str
    content: str | list[dict[str, object]]


class ClaudeMessagesRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    model: str
    messages: list[ClaudeMessage]
    max_tokens: int | None = None
    system: str | list[dict[str, object]] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    tools: list[dict[str, object]] | None = None
    tool_choice: dict[str, object] | None = None
    thinking: dict[str, object] | None = None
    # —— Claude effort 协议（2026-11，beta effort-2025-11-24 → 4.6+ 稳定）——
    # Claude Code 的思考强度选择器（Low/Medium/High/Max）不在 thinking 里，
    # 而是走顶层 ``output_config: {"effort": "max"}``（adaptive thinking 模式
    # 下与 ``thinking: {"type": "adaptive"}`` 并发出现）。历史上无此字段时
    # pydantic 静默丢弃，表现为「开了 max 却到不了 xhigh 顶格思考档」。
    # 兼容个别网关把它塞进 thinking.effort 的变体（compat_claude 一并归一化）。
    output_config: dict[str, object] | None = None
    metadata: dict[str, object] | None = None
