"""OpenAI / Anthropic(Claude) 协议兼容层请求模型。

字段设计保持宽松容错：未知字段一律忽略、content 允许字符串或块数组，
以最大化第三方客户端（OpenAI SDK / Anthropic SDK / one-api / Claude Code 等）
的兼容性。
"""

from __future__ import annotations

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# OpenAI Chat Completions (/v1/chat/completions)
# ---------------------------------------------------------------------------


class OpenAIChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, object]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, object]] | None = None
    tool_call_id: str | None = None


class OpenAIChatCompletionRequest(BaseModel):
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


# ---------------------------------------------------------------------------
# OpenAI Responses (/v1/responses)
# ---------------------------------------------------------------------------


class OpenAIResponsesRequest(BaseModel):
    """OpenAI Responses API 请求（宽松容错：未知字段一律忽略）。

    兼容 openai-python / Codex CLI 等 Responses 客户端。``input`` 支持
    字符串或 item 数组（message / function_call / function_call_output /
    reasoning）；``instructions`` 独立于 system 消息映射。
    """

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
    metadata: dict[str, object] | None = None
