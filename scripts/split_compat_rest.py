"""Task 2-4: compat_service 拆分——compat_errors / compat_openai / compat_claude + 门面收缩。

单脚本三阶段（任务书允许合并为一个多阶段脚本）：
- 阶段2: compat_errors.py（错误类型映射 + 签名占位 + 错误载荷/路径判定）
- 阶段3: compat_openai.py（_openai_* 族 + 请求/响应/流适配 + reasoning effort 族）
- 阶段4: compat_claude.py（_claude_* 族 + 请求/响应/流适配；_claude_base64_part/_claude_usage_payload 自 compat_shared 归位到 Claude 侧）
最后 compat_service.py 收缩为纯聚合门面，并把三处深链 monkeypatch 测试的
patch 点改指 compat_shared（_MAX_MEDIA_BYTES/_MAX_SSE_EVENT_BYTES/get_compat_settings）。

全部锚点 assert 通过后才写任何文件；失败即退出不落盘。
用法:
    uv run python /home/z/my-project/aistudio-api/scripts/split_compat_rest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path("/home/z/my-project/aistudio-api")
APP = BASE / "src/aistudio_api/application"
SRC = APP / "compat_service.py"
SHARED = APP / "compat_shared.py"
ERR = APP / "compat_errors.py"
OPENAI = APP / "compat_openai.py"
CLAUDE = APP / "compat_claude.py"

text = SRC.read_text(encoding="utf-8")
shared = SHARED.read_text(encoding="utf-8")


def cut(src: str, start: str, end: str, *, label: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    assert src.count(start) == 1, f"{label}: start not unique"
    return src[i:j]


BANNER = "# " + "-" * 75 + "\n"

# ============ 阶段2：compat_errors 素材 ============
E1 = cut(
    text,
    "# Gemini finishReason → OpenAI finish_reason\n",
    BANNER + "# 服务端工具调用防护",
    label="E1 错误常量",
)
assert "_OPENAI_ERROR_TYPE" in E1 and "_CLAUDE_DUMMY_SIGNATURE" in E1

E2 = cut(
    shared,
    "def _claude_signature(signature: object) -> str:\n",
    "def _as_int(value: object) -> int | None:\n",
    label="E2 签名辅助(shared→errors)",
)
assert E2.count("def _claude_signature") == 1
assert E2.count("def _read_claude_signature") == 1

ERR2 = cut(
    text,
    "def openai_error_payload(status: int, message: str) -> dict[str, object]:\n",
    "__all__ = [\n",
    label="ERR2 错误载荷/路径判定",
)
assert "build_compat_error_response" in ERR2 and "is_compat_request" in ERR2

# ============ 阶段3：compat_openai 素材 ============
O1 = cut(
    text,
    "def _openai_content_text(content: object) -> str:\n",
    BANNER + "# Claude 请求 → Gemini 请求\n",
    label="O1 OpenAI 请求转换",
)
assert "async def openai_to_gemini_request" in O1 and "@media_session()" in O1

O2 = cut(
    text,
    "# 思考强度词表：兼容大小写 / 连字符 / 下划线 / 别名，统一归一化为\n",
    "def _claude_thinking_to_level(thinking: dict[str, object] | None) -> str | None:\n",
    label="O2 reasoning effort 族",
)
assert "def _openai_reasoning_effort" in O2 and "_EFFORT_ALIASES" in O2

O3 = cut(
    text,
    "def _openai_finish_reason(finish_reason: str | None, *, has_tool_calls: bool) -> str:\n",
    "def _coerce_tool_args(args: object) -> dict[str, object]:\n",
    label="O3 OpenAI 响应构建",
)
assert "def build_openai_completion" in O3

O4 = cut(
    text,
    "def _openai_usage_from_frame(usage_metadata: object) -> dict[str, object]:\n",
    BANNER + "# 流式适配：Gemini SSE → Claude 事件流\n",
    label="O4 OpenAI 流适配",
)
assert "async def openai_stream_adapter" in O4

# ============ 阶段4：compat_claude 素材 ============
C1 = cut(
    text,
    "async def _claude_blocks_to_parts(\n",
    "# 思考强度词表：兼容大小写 / 连字符 / 下划线 / 别名，统一归一化为\n",
    label="C1 Claude 请求转换",
)
assert "def _claude_tools_to_gemini" in C1

C2 = cut(
    text,
    "def _claude_thinking_to_level(thinking: dict[str, object] | None) -> str | None:\n",
    BANNER + "# Gemini 响应 → OpenAI / Claude（非流式）\n",
    label="C2 claude_to_gemini_request",
)
assert "async def claude_to_gemini_request" in C2 and "@media_session()" in C2

C3 = cut(
    text,
    "def _coerce_tool_args(args: object) -> dict[str, object]:\n",
    BANNER + "# Gemini SSE 帧解析\n",
    label="C3 build_claude_message",
)
assert "def build_claude_message" in C3 and "def _claude_stop_reason" in C3

C4 = cut(
    text,
    "class _ClaudeStreamEmitter:\n",
    "async def claude_stream_adapter(\n",
    label="C4 Claude emitter",
)
assert C4.count("class _ClaudeStreamEmitter") == 1

C5 = cut(
    text,
    "async def claude_stream_adapter(\n",
    BANNER + "# 错误格式化（供 app.py 全局异常处理器按路径分流）\n",
    label="C5 claude_stream_adapter",
)
assert C5.count("async def claude_stream_adapter") == 1

S1 = cut(
    shared,
    "def _claude_base64_part(\n",
    "def _claude_usage_payload(usage: dict[str, int]) -> dict[str, object]:\n",
    label="S1 base64 part 归位",
)
S2 = cut(
    shared,
    "def _claude_usage_payload(usage: dict[str, int]) -> dict[str, object]:\n",
    "@contextlib.asynccontextmanager\nasync def _stream_adapter_lifecycle(\n",
    label="S2 usage payload 归位",
)
assert S1.count("def _claude_base64_part") == 1
assert S2.count("def _claude_usage_payload") == 1

# ============ 生成 compat_errors.py ============
ERR.write_text(
    '''"""兼容层错误映射与协议错误载荷。

自 compat_service.py 拆出：OpenAI / Claude 两协议的错误类型码表、
Gemini finishReason 终结语义映射，以及全局异常处理器使用的
路径判定与协议格式化函数。无内部依赖（仅引用 compat_shared 的 coerce）。

- ``_GEMINI_TO_OPENAI_FINISH`` / ``_CLAUDE_REFUSAL_FINISH``：异常终结语义
- ``_OPENAI_ERROR_TYPE`` / ``_CLAUDE_ERROR_TYPE``：HTTP 状态 → 协议错误类型
- ``_CLAUDE_DUMMY_SIGNATURE`` 与 ``_claude_signature`` / ``_read_claude_signature``：
  Claude thinking 占位签名的写入/回读（与占位常量直接绑定）
- ``openai_error_payload`` / ``claude_error_payload`` / ``build_compat_error_response``
  等错误格式化入口（供 app.py / request_middleware.py 按路径分流）
"""

from __future__ import annotations

import base64
import json

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aistudio_api.application.compat_shared import _as_dict, _as_str
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.errors")


'''
    + E1.rstrip("\n")
    + "\n\n\n"
    + E2.rstrip("\n")
    + "\n\n\n"
    + ERR2.rstrip("\n")
    + "\n",
    encoding="utf-8",
)
print("compat_errors.py written")

# ============ 生成 compat_openai.py ============
OPENAI.write_text(
    '''"""OpenAI Chat Completions 协议兼容转换层。

自 compat_service.py 拆出：请求侧（Chat Completions → Gemini 请求模型）、
响应侧（Gemini 响应 / SSE 帧 → Chat Completion 对象与 chunk 流），以及
各生态客户端思考强度参数的归一化族（``reasoning_effort`` / ``reasoning.effort``
/ ``thinking`` / ``enable_thinking`` / ``chat_template_kwargs``）。

媒体管线、签名缓存、SSE 帧解析等双侧共用工具见 compat_shared；
错误码表与终结语义见 compat_errors。
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterable, Callable

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.api.schemas_compat import OpenAIChatCompletionRequest
from aistudio_api.application.compat_errors import (
    _GEMINI_TO_OPENAI_FINISH,
    _OPENAI_ERROR_TYPE,
)
from aistudio_api.application.compat_shared import (
    _as_dict,
    _as_int,
    _as_str,
    _assert_media_size,
    _convert_items_concurrently,
    _file_block_to_part,
    _frame_candidate,
    _frame_parts,
    _iter_gemini_sse_frames,
    _iter_response_parts,
    _media_part_from_url,
    _read_signature,
    _signature_extra,
    _usage_from_gemini,
    resolve_compat_model,
)
from aistudio_api.application.media_session import has_remote_media, media_session
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    lookup_function_signature as _lookup_function_signature,
    valid_thought_signature as _valid_thought_signature,
)
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.openai")


'''
    + O1.rstrip("\n")
    + "\n\n\n"
    + O2.rstrip("\n")
    + "\n\n\n"
    + O3.rstrip("\n")
    + "\n\n\n"
    + O4.rstrip("\n")
    + "\n",
    encoding="utf-8",
)
print("compat_openai.py written")

# ============ 生成 compat_claude.py ============
CLAUDE.write_text(
    '''"""Anthropic(Claude) Messages 协议兼容转换层。

自 compat_service.py 拆出：请求侧（Messages → Gemini 请求模型，含
tool_use / tool_result / thinking / image / document 块转换）、响应侧
（Gemini 响应 / SSE 帧 → Message 对象与 Messages SSE 事件流），以及
thinking.budget_tokens → 思考等级映射。

媒体管线、签名缓存、SSE 帧解析等双侧共用工具见 compat_shared；
错误码表、refusal 终结集与 thinking 占位签名见 compat_errors。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncGenerator, AsyncIterable, Callable

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application.compat_errors import (
    _CLAUDE_ERROR_TYPE,
    _CLAUDE_REFUSAL_FINISH,
    _claude_signature,
    _read_claude_signature,
)
from aistudio_api.application.compat_shared import (
    _as_dict,
    _as_int,
    _as_str,
    _assert_media_size,
    _convert_items_concurrently,
    _frame_candidate,
    _frame_parts,
    _iter_gemini_sse_frames,
    _iter_response_parts,
    _media_part_from_url,
    _read_signature,
    _signature_extra,
    _usage_from_gemini,
    resolve_compat_model,
)
from aistudio_api.application.media_session import has_remote_media, media_session
from aistudio_api.application.stream_utils import StopSequenceBuffer
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    lookup_function_signature as _lookup_function_signature,
    valid_thought_signature as _valid_thought_signature,
)
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.claude")


'''
    + C1.rstrip("\n")
    + "\n\n\n"
    + S1.rstrip("\n")
    + "\n\n\n"
    + S2.rstrip("\n")
    + "\n\n\n"
    + C2.rstrip("\n")
    + "\n\n\n"
    + C3.rstrip("\n")
    + "\n\n\n"
    + C4.rstrip("\n")
    + "\n\n\n"
    + C5.rstrip("\n")
    + "\n",
    encoding="utf-8",
)
print("compat_claude.py written")

# ============ 收缩 compat_shared.py（移除已归位符号 + 重写 import 头） ============
for blk, label in ((E2, "E2"), (S1, "S1"), (S2, "S2")):
    assert shared.count(blk) == 1, f"shared {label} not unique"
    shared = shared.replace(blk, "", 1)

OLD_SHARED_HEADER = """from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.anti_truncation import _close_stream_quietly
from aistudio_api.application.media_session import (
    current_media,
    has_remote_media,
    media_session,
)"""
NEW_SHARED_HEADER = """from aistudio_api.api.schemas import GeminiInlineData, GeminiPart
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.anti_truncation import _close_stream_quietly
from aistudio_api.application.media_session import (
    current_media,
    media_session,
)"""
assert shared.count(OLD_SHARED_HEADER) == 1, "shared import header"
shared = shared.replace(OLD_SHARED_HEADER, NEW_SHARED_HEADER, 1)
SHARED.write_text(shared, encoding="utf-8")
print("compat_shared.py slimmed (claude-only helpers moved out)")

# ============ compat_service.py 收缩为聚合门面 ============
FACADE = '''"""OpenAI / Anthropic(Claude) 协议兼容转换层（聚合门面）。

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

from aistudio_api.application.compat_claude import (  # noqa: F401
    _claude_stop_reason,
    _claude_thinking_to_level,
    _claude_tools_to_gemini,
    build_claude_message,
    claude_stream_adapter,
    claude_to_gemini_request,
)
from aistudio_api.application.compat_errors import (  # noqa: F401
    build_compat_error_response,
    claude_error_payload,
    is_claude_compat_request,
    is_compat_request,
    openai_error_payload,
)
from aistudio_api.application.compat_openai import (  # noqa: F401
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
from aistudio_api.application.compat_shared import (  # noqa: F401
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
'''
SRC.write_text(FACADE, encoding="utf-8")
print("compat_service.py collapsed to facade")

# ============ 测试 patch 点迁移（深链 monkeypatch → compat_shared） ============
T1 = BASE / "tests/unit/test_compat_audit_fixes.py"
t1 = T1.read_text(encoding="utf-8")
OLD = '"aistudio_api.application.compat_service.get_compat_settings"'
NEW = '"aistudio_api.application.compat_shared.get_compat_settings"'
assert t1.count(OLD) == 1
T1.write_text(t1.replace(OLD, NEW, 1), encoding="utf-8")

T2 = BASE / "tests/unit/test_inline_media_budget.py"
t2 = T2.read_text(encoding="utf-8")
assert t2.count("compat_service") >= 5
t2 = t2.replace("compat_service", "compat_shared")
T2.write_text(t2, encoding="utf-8")

T3 = BASE / "tests/unit/test_stream_protocol_regressions.py"
t3 = T3.read_text(encoding="utf-8")
assert t3.count("from aistudio_api.application import compat_service") == 1
t3 = t3.replace(
    "from aistudio_api.application import compat_service",
    "from aistudio_api.application import compat_shared",
    1,
)
assert t3.count('monkeypatch.setattr(compat_service, "_MAX_SSE_EVENT_BYTES"') == 2
t3 = t3.replace(
    'monkeypatch.setattr(compat_service, "_MAX_SSE_EVENT_BYTES"',
    'monkeypatch.setattr(compat_shared, "_MAX_SSE_EVENT_BYTES"',
)
T3.write_text(t3, encoding="utf-8")
print("test patch points migrated to compat_shared")
sys.exit(0)
