"""OpenAI / Anthropic(Claude) 协议兼容转换层（聚合门面）。

在既有 Gemini 管线（``GeminiGenerateContentRequest`` →
``normalize_gemini_request`` → ``AIStudioClient`` → Gemini SSE / 响应模型）
之上做双向转换，完整复用账号轮换、403 阶梯重试、429 冷却与抗截断传输：

- 请求侧：OpenAI Chat Completions / Claude Messages → Gemini 请求模型
- 响应侧：Gemini 响应 / SSE 帧 → OpenAI / Claude 格式（含流式）
- 模型映射：``gpt-*`` / ``claude-*`` 等家族名 → 配置的默认 Gemini 模型

实现按关注点拆分（本文件仅做单向聚合 re-export，import 路径零变化）：

- ``compat_shared``：双侧共用工具（媒体 / 签名 / 数值 coerce / SSE 帧解析 /
  ``resolve_compat_model``）
- ``compat_errors``：错误类型映射与协议错误载荷
- ``compat_openai``：OpenAI 请求 / 响应 / 流式适配与思考强度归一化
- ``compat_claude``：Claude 请求 / 响应 / 流式适配与 thinking 等级映射
"""

from aistudio_api.application.compat_claude import (
    _claude_stop_reason,
    _claude_thinking_to_level,
    _claude_tools_to_gemini,
    build_claude_message,
    claude_stream_adapter,
    claude_to_gemini_request,
)
from aistudio_api.application.compat_errors import (
    build_compat_error_response,
    claude_error_payload,
    is_claude_compat_request,
    is_compat_request,
    openai_error_payload,
)
from aistudio_api.application.compat_openai import (
    _openai_finish_reason,
    _openai_parts_from_content,
    _openai_reasoning_effort,
    _openai_tool_choice_to_config,
    _openai_tools_to_gemini,
    build_openai_completion,
    normalize_reasoning_effort_word,
    openai_stream_adapter,
    openai_to_gemini_request,
)
from aistudio_api.application.compat_shared import (
    MEDIA_CONCURRENCY,
    _as_dict,
    _as_str,
    _convert_items_concurrently,
    _file_block_to_part,
    _frame_candidate,
    _frame_parts,
    _iter_gemini_sse_frames,
    _media_part_from_url,
    _parse_gemini_sse_frames,
    _read_signature,
    _signature_extra,
    _stream_adapter_lifecycle,
    _usage_from_gemini,
    resolve_compat_model,
)

__all__ = [
    "MEDIA_CONCURRENCY",
    "_as_dict",
    "_as_str",
    "_claude_stop_reason",
    "_claude_thinking_to_level",
    "_claude_tools_to_gemini",
    "_convert_items_concurrently",
    "_file_block_to_part",
    "_frame_candidate",
    "_frame_parts",
    "_iter_gemini_sse_frames",
    "_media_part_from_url",
    "_openai_finish_reason",
    "_openai_parts_from_content",
    "_openai_reasoning_effort",
    "_openai_tool_choice_to_config",
    "_openai_tools_to_gemini",
    "_parse_gemini_sse_frames",
    "_read_signature",
    "_signature_extra",
    "_stream_adapter_lifecycle",
    "_usage_from_gemini",
    "build_claude_message",
    "build_compat_error_response",
    "build_openai_completion",
    "claude_error_payload",
    "claude_stream_adapter",
    "claude_to_gemini_request",
    "is_claude_compat_request",
    "is_compat_request",
    "normalize_reasoning_effort_word",
    "openai_error_payload",
    "openai_stream_adapter",
    "openai_to_gemini_request",
    "resolve_compat_model",
]
