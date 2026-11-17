"""Task 1: compat_service 拆分——抽取 compat_shared.py。

从 compat_service.py 迁出双侧共享的媒体/签名/数值 coerce/SSE 帧解析辅助、
家族正则与 resolve_compat_model；compat_service 保留全部原符号（门面重导出），
内部引用改为从 compat_shared 导入。锚点精确匹配替换，失败即报错退出（不写回）。

用法:
    uv run python /home/z/my-project/aistudio-api/scripts/split_compat_1shared.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path("/home/z/my-project/aistudio-api")
SRC = BASE / "src/aistudio_api/application/compat_service.py"
NEW = BASE / "src/aistudio_api/application/compat_shared.py"

text = SRC.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# 段落提取辅助：按起止锚精确切割原文（含两端锚之间的全部行）
# ---------------------------------------------------------------------------


def cut(start: str, end: str, *, label: str) -> str:
    """取 start 行首到 end 行首之间的原文段（不含 end 行）。"""
    i = text.index(start)
    j = text.index(end, i)
    assert text.count(start) == 1, f"{label}: start anchor not unique"
    return text[i:j]


# —— compat_shared.py 的正文段（自原文件逐段剪切，保证实现零改动）——

# 段A：模块常量 _MAX_MEDIA_BYTES / _MAX_SSE_EVENT_BYTES / 家族正则 /
#       _MAGIC_SNIFFERS / _DATA_URL_RE（原 74-99 行区域，剪切到 _GEMINI_TO_OPENAI_FINISH 注释前）
A_START = "_MAX_MEDIA_BYTES = 20 * 1024 * 1024\n"
A_END = "# Gemini finishReason → OpenAI finish_reason\n"
A_BLOCK = cut(A_START, A_END, label="A 常量区")
assert "_OPENAI_FAMILY_RE" in A_BLOCK and "_DATA_URL_RE" in A_BLOCK

# 段B：共享辅助函数族（原 195-449 行：_as_dict … _media_part_from_url）
B_START = "def _as_dict(value: object) -> dict[str, object]:\n"
B_END = "# ---------------------------------------------------------------------------\n# 模型映射\n"
B_BLOCK = cut(B_START, B_END, label="B 共享辅助区")
assert "_convert_items_concurrently" in B_BLOCK and "_media_part_from_url" in B_BLOCK
assert "_claude_base64_part" in B_BLOCK and "_claude_usage_payload" in B_BLOCK
assert "_stream_adapter_lifecycle" in B_BLOCK and "_signature_extra" in B_BLOCK

# 段C：resolve_compat_model（原 457-486）
C_START = "def resolve_compat_model(requested: str, *, channel: str) -> str:\n"
C_END = "# ---------------------------------------------------------------------------\n# OpenAI 请求 → Gemini 请求\n"
C_BLOCK = cut(C_START, C_END, label="C resolve_compat_model")
assert C_BLOCK.count("def resolve_compat_model") == 1

# 段D：_usage_from_gemini（原 1261-1291）
D_START = "def _usage_from_gemini(\n"
D_END = "def _openai_finish_reason(finish_reason: str | None, *, has_tool_calls: bool) -> str:\n"
D_BLOCK = cut(D_START, D_END, label="D _usage_from_gemini")
assert D_BLOCK.count("def _usage_from_gemini") == 1

# 段E：_iter_response_parts（原 1312-1333；双侧 build_* 共用 → shared）
E_START = "def _iter_response_parts(\n"
E_END = "def build_openai_completion(\n"
E_BLOCK = cut(E_START, E_END, label="E _iter_response_parts")
assert E_BLOCK.count("def _iter_response_parts") == 1

# 段F：SSE 帧解析族（原 1556-1652：_iter_gemini_sse_frames/_remember_frame_tool_signatures/
#       _parse_gemini_sse_frames/_frame_candidate/_frame_parts）
F_START = "async def _iter_gemini_sse_frames(\n"
F_END = "# ---------------------------------------------------------------------------\n# 流式适配：Gemini SSE → OpenAI chunk 流\n"
F_BLOCK = cut(F_START, F_END, label="F SSE 帧解析族")
assert "_parse_gemini_sse_frames" in F_BLOCK and "_frame_candidate" in F_BLOCK
assert "_remember_frame_tool_signatures" in F_BLOCK

# ---------------------------------------------------------------------------
# 生成 compat_shared.py
# ---------------------------------------------------------------------------
HEADER = '''"""兼容层共享辅助（媒体 / 签名 / 数值 coerce / SSE 帧解析 / 模型映射）。

自 compat_service.py 收敛拆出：OpenAI 与 Claude 两侧转换器（及 Responses 层）
共用的基础工具全部集中于此，避免 compat_openai / compat_claude 互相 import：

- 数值与结构 coerce：``_as_dict`` / ``_as_str`` / ``_as_int``
- 媒体管线：data URL 解析、魔数嗅探、体积限额、远程下载与并发分批
- thought signature 透传/回读（OpenAI 与 Claude 两种 wire 形态）
- 流适配器生命周期骨架与 Gemini SSE 帧解析（三协议共用）
- ``resolve_compat_model`` 及 OpenAI / Claude 家族正则
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import httpx
import json
import re
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    Awaitable,
    Callable,
    Coroutine,
)
from typing import cast

from aistudio_api.api.schemas import (
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
)
from aistudio_api.application.stream_utils import (
    SSEEventBuffer,
    coalesce_tool_frames,
)
from aistudio_api.domain.tool_signatures import (
    remember_function_signature as _remember_function_signature,
)
from aistudio_api.infrastructure.gateway.model_defaults import get_compat_settings
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("compat.shared")


'''

NEW_TEXT = (
    HEADER
    + A_BLOCK
    + "\n"
    + B_BLOCK
    + "\n"
    + C_BLOCK.rstrip("\n")
    + "\n\n"
    + D_BLOCK.rstrip("\n")
    + "\n\n"
    + E_BLOCK.rstrip("\n")
    + "\n\n"
    + F_BLOCK.rstrip("\n")
    + '\n\n__all__ = [\n    "MEDIA_CONCURRENCY",\n    "resolve_compat_model",\n]\n'
)

NEW.write_text(NEW_TEXT, encoding="utf-8")
print(f"compat_shared.py written: {NEW_TEXT.count(chr(10))} lines")

# ---------------------------------------------------------------------------
# 收缩 compat_service.py：删除已迁出的段，插入重导出 import
# ---------------------------------------------------------------------------

for blk, label in (
    (A_BLOCK, "A"),
    (B_BLOCK, "B"),
    (C_BLOCK, "C"),
    (D_BLOCK, "D"),
    (E_BLOCK, "E"),
    (F_BLOCK, "F"),
):
    assert text.count(blk) == 1, f"{label}: block not unique in source"
    text = text.replace(blk, "", 1)

# 旧 import 头收缩：不再直接使用的依赖移除，改由 compat_shared 提供
OLD_IMPORTS = """from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    Awaitable,
    Callable,
    Coroutine,
)
from typing import cast

import httpx
from fastapi import Request"""
NEW_IMPORTS = """from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    Callable,
)
from fastapi import Request"""
assert text.count(OLD_IMPORTS) == 1, "import block anchor"
text = text.replace(OLD_IMPORTS, NEW_IMPORTS, 1)

OLD_ANTI = """from aistudio_api.application.anti_truncation import _close_stream_quietly
from aistudio_api.application.media_session import (
    current_media,
    has_remote_media,
    media_session,
)
from aistudio_api.application.stream_utils import (
    SSEEventBuffer,
    StopSequenceBuffer,
    coalesce_tool_frames,
)
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    lookup_function_signature as _lookup_function_signature,
    remember_function_signature as _remember_function_signature,
    valid_thought_signature as _valid_thought_signature,
)
from aistudio_api.infrastructure.gateway.model_defaults import get_compat_settings
from aistudio_api.infrastructure.request_context import record_result
"""
NEW_ANTI = """from aistudio_api.application.compat_shared import (  # noqa: F401
    MEDIA_CONCURRENCY,
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
    _parse_data_url,
    _parse_gemini_sse_frames,
    _read_signature,
    _remember_frame_tool_signatures,
    _signature_extra,
    _sniff_mime,
    _stream_adapter_lifecycle,
    _usage_from_gemini,
    resolve_compat_model,
)
from aistudio_api.application.stream_utils import StopSequenceBuffer
from aistudio_api.application.media_session import media_session
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    lookup_function_signature as _lookup_function_signature,
    valid_thought_signature as _valid_thought_signature,
)
from aistudio_api.infrastructure.request_context import record_result
"""
assert text.count(OLD_ANTI) == 1, "anti/media/stream import anchor"
text = text.replace(OLD_ANTI, NEW_ANTI, 1)

SRC.write_text(text, encoding="utf-8")
print("compat_service.py shrunk; shared symbols re-exported")
sys.exit(0)
