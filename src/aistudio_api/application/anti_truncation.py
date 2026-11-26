"""抗截断传输 (Anti-Truncation Transport)。

移植自 ken050210/vertex-streaming-anti-truncation 的核心机制并适配本项目
的 AI Studio JSPB 线路协议：

1. 请求侧注入：追加一个仅用于"运货"的合成函数声明 (默认 ``router_emit``)，
   并在对话末尾追加一条用户指令，要求模型把全部正文写进该函数调用的
   ``content`` 字符串参数里。上游对工具调用参数不做流式截断，从而绕开
   正文文本通道的吞字 / 截断问题。
2. 响应侧还原：非流式直接从 ``function_calls`` 中提取 ``content`` 还原为
   正文文本；流式则通过增量识别器把分片到达的参数字符串实时还原成
   ``body`` 增量事件，客户端仍可逐段看到正文。
3. 优雅降级：AI Studio MakerSuite 协议没有 tool_choice 强制字段（仅 AUTO
   语义），若模型未遵守指令而直接输出纯文本，则原样透传，不破坏请求。

与原实现对齐的安全跳过条件：工具历史轮次、结构化输出、图片输出模型、
内置服务端工具、强制 tool_config、停止序列等场景一律旁路。
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    encode_function_declaration_to_wire,
)
from aistudio_api.domain.models import ModelOutput, ResponsePart
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_anti_truncation_settings,
    resolve_model_defaults,
)
from aistudio_api.infrastructure.gateway.wire_spec import WIRE_FINISH_REASON_STOP
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioPart,
    wire_function_name,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("anti_truncation")

DEFAULT_TOOL_NAME = "router_emit"

# 模型变体后缀：选中模型在模型列表中暴露的两个抗截断变体。
# ``<model>-at-stream`` —— 流式抗截断：SSE 端点走流式增量还原；
# ``<model>-at``      —— 非流式抗截断：任意端点都走缓冲上游，整体还原后
#                        以单条完整响应下发（SSE 端点则打包为一条事件）。
# 注意 ``-at`` 是 ``-at-stream`` 的后缀后缀，解析时必须先长后短。
STREAM_VARIANT_SUFFIX = "-at-stream"
BUFFER_VARIANT_SUFFIX = "-at"
# 中文括号别名后缀（Mooece/ais-pg-upgrade 补丁同款，酒馆等客户端在模型名
# 里写 ``<model>[防截断]`` 选中）：语义等价 ``-at``——非流式缓冲 + 强制
# 抗截断。仍受「基础模型须命中面板选中列表」约束，避免误吞本名就含
# 该字样的模型；匹配前同样先长后短（互不为彼此后缀，顺序无歧义）。
ALIAS_VARIANT_SUFFIX = "[防截断]"


@dataclass(frozen=True)
class ModelVariantResolution:
    """模型变体解析结果：去掉后缀的基础模型与强制行为。"""

    model_path: str
    # 变体选中即强制开启抗截断（仍可被 X-Anti-Truncation: 0 请求头压低）。
    anti_truncation: bool
    # 是否强制缓冲（非流式）上游传输。
    buffered: bool


def get_selected_variant_models(
    settings: dict[str, object] | None = None,
) -> set[str]:
    """读取面板选中的变体基础模型集合（小写、去 ``models/`` 前缀）。"""
    if settings is None:
        settings = get_anti_truncation_settings()
    variants = settings.get("model_variants")
    if not isinstance(variants, dict) or not variants.get("enabled"):
        return set()
    raw_models = variants.get("models")
    if not isinstance(raw_models, list):
        return set()
    return {
        str(m).removeprefix("models/").strip().lower()
        for m in raw_models
        if str(m).strip()
    }


def resolve_model_variant(
    model_path: str,
    settings: dict[str, object] | None = None,
) -> ModelVariantResolution | None:
    """解析模型路径末尾的抗截断变体后缀。

    仅当去掉后缀的基础模型命中面板选中列表时才生效，避免误吞本名
    就以 ``-at`` 结尾的常规模型；未命中或变体功能关闭时返回 None，
    请求按原模型处理。
    """
    selected = get_selected_variant_models(settings)
    if not selected or not model_path:
        return None
    prefix = "models/" if model_path.startswith("models/") else ""
    clean = model_path.removeprefix("models/").strip().lower()
    for suffix, buffered in (
        (STREAM_VARIANT_SUFFIX, False),
        (BUFFER_VARIANT_SUFFIX, True),
        (ALIAS_VARIANT_SUFFIX, True),
    ):
        if clean.endswith(suffix):
            base = clean[: -len(suffix)]
            if base and base in selected:
                # 保留原始大小写：直接在原字符串上截取，避免改写模型名。
                raw_clean = model_path.removeprefix("models/")
                return ModelVariantResolution(
                    model_path=prefix + raw_clean[: len(raw_clean) - len(suffix)],
                    anti_truncation=True,
                    buffered=buffered,
                )
            return None
    return None


# 与上游 JS 实现一致的审计白名单：仅固定元数据可进入日志。
AUDIT_SKIP_REASONS = frozenset(
    {
        "disabled",
        "header-off",
        "image-model",
        "image-output",
        "structured-output",
        "stop-sequences",
        "tool-history",
        "builtin-tools",
        "tool-mode-none",
        "tool-choice",
    }
)


@dataclass
class AntiTruncationContext:
    """单次请求的抗截断传输上下文与审计信息。"""

    tool_name: str = ""
    active: bool = False
    skip_reason: str = "disabled"
    # 运行期审计（不落盘，仅日志）
    restored: bool = False
    synthetic_seen: bool = False
    plain_seen: bool = False
    stream_done: bool = False
    multiple_calls: bool = False

    def audit_fields(self) -> dict[str, object]:
        return {
            "transport": "active"
            if self.active
            else (
                self.skip_reason
                if self.skip_reason in AUDIT_SKIP_REASONS
                else "unknown"
            ),
            "restored": self.restored,
            "syntheticSeen": self.synthetic_seen,
            "streamDone": self.stream_done,
        }


def _random_tool_name(prefix: str = "router_") -> str:
    """客户端工具名与合成名前缀冲突时生成随机名（"m" 非十六进制，前缀必不重叠）。"""
    return prefix + secrets.token_hex(6)


def _is_function_declarations_container(tool: object) -> bool:
    """判断 tools 容器是否为自定义函数声明集合 ``[None, [decl, ...]]``。"""
    if not isinstance(tool, list) or len(tool) < 2 or not isinstance(tool[1], list):
        return False
    for decl in tool[1]:
        if not isinstance(decl, list) or not decl or not isinstance(decl[0], str):
            return False
    return True


def _iter_declared_function_names(tools: list[list[object]] | None) -> list[str]:
    names: list[str] = []
    for tool in tools or []:
        if not _is_function_declarations_container(tool):
            continue
        decls = tool[1]
        if not isinstance(decls, list):
            continue
        for decl in decls:
            if isinstance(decl, list) and decl:
                names.append(str(decl[0]))
    return names


def _has_tool_history(contents: list[AistudioContent]) -> bool:
    for content in contents:
        for part in content.parts:
            if part.function_call is not None or part.function_response is not None:
                return True
    return False


def _build_synthetic_declaration(tool_name: str) -> list[object]:
    return encode_function_declaration_to_wire(
        {
            "name": tool_name,
            "description": (
                "Deliver the complete user-visible reply as text; end the call "
                "when the requested content is complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The requested reply itself, preserving its language, "
                            "sections, formatting and markup."
                        ),
                    }
                },
                "required": ["content"],
            },
        }
    )


def _build_instruction_message(tool_name: str, *, callable_tools: bool) -> str:
    if callable_tools:
        return (
            f"Transport format for this reply: put all user-visible text, if any, in "
            f"exactly one call of {tool_name}; its content string holds the entire "
            f"user-visible answer, preserving all requested formatting, markup, "
            f"language and sections. {tool_name} only transports text and performs no "
            f"external action. Call the other tools whenever the task needs them, "
            f"with or without {tool_name}. Do not output any user-visible text "
            f"outside of tool calls. This transport does not change the task. Do not "
            f"add unsolicited commentary about this transport, tool selection or "
            f"serialization. Once the requested content is complete, end the call "
            f"without repeating finished content or adding filler. Preserve "
            f"repetition explicitly requested by the task."
        )
    return (
        f"Transport format for this reply: call {tool_name} exactly once. Put the "
        f"entire user-visible answer in its content string, preserving all requested "
        f"formatting, markup, language and sections. The function only transports "
        f"text and performs no external action. Do not output any user-visible text "
        f"outside of the {tool_name} call. This transport does not change the task. "
        f"Do not add unsolicited commentary about this transport, tool selection or "
        f"serialization. Once the requested content is complete, end the call "
        f"without repeating finished content or adding filler. Preserve repetition "
        f"explicitly requested by the task."
    )


def prepare_anti_truncation(
    normalized: NormalizedGeminiRequest,
    *,
    override: bool | None = None,
) -> AntiTruncationContext:
    """在规范化请求上原地启用抗截断传输，返回本次上下文。

    ``override`` 来自请求头 ``X-Anti-Truncation``：True 强制开启、False 强制
    关闭、None 跟随 config.yaml 全局配置。命中任一安全跳过条件时旁路并记录
    原因，绝不改变请求语义。
    """
    ctx = AntiTruncationContext()

    settings = get_anti_truncation_settings()
    enabled = bool(settings.get("enabled", False))
    if override is not None:
        enabled = override
    if not enabled:
        ctx.skip_reason = "disabled" if override is None else "header-off"
        return ctx

    model_defaults = resolve_model_defaults(normalized.model)
    if model_defaults.is_image_model:
        ctx.skip_reason = "image-model"
        return ctx

    overrides = normalized.generation_config_overrides or {}
    if overrides.get("image_output_mode") is not None:
        ctx.skip_reason = "image-output"
        return ctx
    if overrides.get("response_schema") is not None:
        ctx.skip_reason = "structured-output"
        return ctx
    response_mime = str(overrides.get("response_mime_type") or "").strip().lower()
    if response_mime and response_mime != "text/plain":
        ctx.skip_reason = "structured-output"
        return ctx
    if overrides.get("stop_sequences"):
        ctx.skip_reason = "stop-sequences"
        return ctx

    if _has_tool_history(normalized.contents):
        ctx.skip_reason = "tool-history"
        return ctx

    # tools 容器分类：函数声明集合可共存；内置服务端工具混入有 400 风险，旁路。
    tools = normalized.tools
    has_builtin = bool(tools) and not all(
        _is_function_declarations_container(t) for t in tools if isinstance(t, list)
    )
    if has_builtin:
        ctx.skip_reason = "builtin-tools"
        return ctx

    # tool_config 检查：NONE / ANY(强制) 均不接管。
    tool_config = normalized.tool_config
    if (
        isinstance(tool_config, list)
        and len(tool_config) > 1
        and isinstance(tool_config[1], list)
    ):
        fcc = tool_config[1]
        mode_code = fcc[0] if fcc else 1
        if mode_code == 3:
            ctx.skip_reason = "tool-mode-none"
            return ctx
        if mode_code == 2:
            ctx.skip_reason = "tool-choice"
            return ctx

    declared_names = _iter_declared_function_names(tools)
    configured_name = str(settings.get("tool_name") or DEFAULT_TOOL_NAME).strip()
    tool_name = configured_name or DEFAULT_TOOL_NAME
    if any(name.startswith(tool_name) for name in declared_names):
        tool_name = _random_tool_name()

    # 配置的合成名超长时同样会被 wire_function_name 截短；注册进恢复
    # 映射，保证流式还原的 tool_name 前缀匹配不受截短影响
    synthetic_wire_name = wire_function_name(tool_name)
    if synthetic_wire_name != tool_name:
        restore = dict(normalized.function_name_restore or {})
        restore[synthetic_wire_name] = tool_name
        normalized.function_name_restore = restore

    # 注入合成函数声明。
    synthetic_decl = _build_synthetic_declaration(tool_name)
    if tools:
        appended = False
        for tool in tools:
            if _is_function_declarations_container(tool):
                decls = tool[1]
                if isinstance(decls, list):
                    decls.append(synthetic_decl)
                    appended = True
                    break
        if not appended:
            tools.append([None, [synthetic_decl]])
    else:
        normalized.tools = [[None, [synthetic_decl]]]

    # 追加传输指令（对话末尾、user 角色）。
    instruction = _build_instruction_message(
        tool_name, callable_tools=bool(declared_names)
    )
    normalized.contents.append(
        AistudioContent(role="user", parts=[AistudioPart(text=instruction)])
    )

    ctx.tool_name = tool_name
    ctx.active = True
    logger.info("抗截断传输已启用: model=%s, tool=%s", normalized.model, tool_name)
    return ctx


# ---------------------------------------------------------------------------
# 非流式还原
# ---------------------------------------------------------------------------


def _presentation_id(call: dict[str, object]) -> str:
    primary = call.get("call_id")
    alternate = call.get("id")
    if isinstance(primary, str) and primary:
        if isinstance(alternate, str) and alternate and alternate != primary:
            return ""
        return primary
    return alternate if isinstance(alternate, str) else ""


def restore_anti_truncation_output(
    output: ModelOutput, ctx: AntiTruncationContext
) -> None:
    """Decode completed presentation calls in their original output positions."""
    if not ctx.active or not output.candidates:
        return
    candidate = output.candidates[0]
    if not any(call.get("name") == ctx.tool_name for call in candidate.function_calls):
        return

    original = candidate.ordered_parts
    source = (
        original
        if original is not None
        else [ResponsePart(function_call=call) for call in candidate.function_calls]
    )
    tracker = _PresentationTracker(ctx.tool_name)
    # Keep the original delta positions, but buffer only deltas whose
    # display argument object eventually completes.
    positions: dict[int, tuple[_PresentationCallState, str]] = {}
    real_calls: list[dict[str, object]] = []
    stripped: list[ResponsePart] | None = None
    for index, part in enumerate(source):
        call = part.function_call
        if call is None:
            continue
        update = tracker.feed(call)
        if update is None:
            real_calls.append(call)
            continue
        state, delta = update
        if state is not None and delta:
            positions[index] = state, delta
        if original is not None:
            if stripped is None:
                stripped = original.copy()
            # An internal call's signature is not a signature for its text.
            stripped[index] = replace(part, function_call=None, thought_signature="")

    ctx.synthetic_seen = tracker.seen
    if not ctx.synthetic_seen:
        return
    ctx.multiple_calls = tracker.count > 1
    if ctx.multiple_calls:
        logger.warning(
            "抗截断传输出现多次合成调用，已按顺序拼接: tool=%s, calls=%d",
            ctx.tool_name,
            tracker.count,
        )
    candidate.function_calls = real_calls
    restored = {i: delta for i, (state, delta) in positions.items() if state.complete}
    ctx.restored = bool(restored)
    if candidate.finish_reason == WIRE_FINISH_REASON_STOP and tracker.incomplete(
        plain_won=bool(candidate.text) and not ctx.restored
    ):
        candidate.finish_reason = _INCOMPLETE_PRESENTATION_FINISH
    if ctx.restored:
        candidate.text = "".join(restored.values())

    if stripped is not None:
        ordered: list[ResponsePart] = []
        for index, part in enumerate(stripped):
            if delta := restored.get(index):
                ordered.append(
                    ResponsePart(
                        text=delta, text_present=True, wire_chunk=part.wire_chunk
                    )
                )
            if ctx.restored and part.text_present and not part.thought:
                part = _without_plain_text(part)
            ordered.append(part)
        candidate.ordered_parts = ordered

    logger.info(
        "抗截断非流式还原完成: restored=%s, real_calls=%d, text_len=%d",
        ctx.restored,
        len(real_calls),
        len(candidate.text),
    )


# ---------------------------------------------------------------------------
# 流式增量 JSON 参数识别器（移植自 JS ContentDecoder）
# ---------------------------------------------------------------------------

_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


_MAX_CONTENT_DECODER_DEPTH = 64


class ContentDecoder:
    """流式 JSON 识别器：仅跟踪语法状态、短对象键与至多一个转义。

    用于把分片到达的函数参数 JSON 字符串（如 ``{"content": "..."`` 的碎片）
    实时解码出 ``content`` 值的增量文本，绝不整段缓冲原始参数。
    """

    def __init__(self) -> None:
        self.stack: list[dict[str, object]] = []
        self.mode = "normal"
        self.root_started = False
        self.complete = False
        self.invalid = False
        self.found = False
        self.closed_content = False
        self.string_kind = ""
        self.key = ""
        self.escape = False
        self.hex: str | None = None
        self.token = ""
        self.expected = ""
        self.high = ""

    def _fail(self) -> None:
        self.invalid = True

    def _value_done(self) -> None:
        if self.stack:
            self.stack[-1]["state"] = "commaOrEnd"
        else:
            self.complete = True

    def _emit_char(self, ch: str, out: list[str]) -> None:
        if self.string_kind == "key":
            if len(self.key) < 32:
                self.key += ch
            return
        if self.string_kind != "content":
            return
        code = ord(ch[0])
        if self.high:
            high_code = ord(self.high[0])
            self.high = ""
            if 0xDC00 <= code <= 0xDFFF:
                # 合并 UTF-16 代理对为单一码点（Python 字符串为码点序列）。
                out.append(
                    chr(0x10000 + ((high_code - 0xD800) << 10) + (code - 0xDC00))
                )
                return
            out.append(chr(high_code))
        if 0xD800 <= code <= 0xDBFF:
            self.high = ch
        else:
            out.append(ch)

    def _start_value(self, ch: str, content: bool, stack_limit: int) -> None:
        if content:
            if self.found or ch != '"':
                self._fail()
                return
            self.found = True
        if ch == '"':
            self.mode = "string"
            self.string_kind = "content" if content else "value"
            self.escape = False
            self.hex = None
        elif ch in "{[":
            if len(self.stack) >= stack_limit:
                self._fail()
                return
            self.stack.append(
                {
                    "type": ch,
                    "state": "keyOrEnd" if ch == "{" else "valueOrEnd",
                    "key": "",
                }
            )
        elif ch in "tfn":
            self.mode = "literal"
            self.token = ch
            self.expected = {"t": "true", "f": "false", "n": "null"}[ch]
        elif ch == "-" or ch.isdigit():
            self.mode = "number"
            self.token = ch
        else:
            self._fail()

    def feed(self, text: str, *, stack_limit: int = _MAX_CONTENT_DECODER_DEPTH) -> str:
        """喂入一段参数字符串碎片，返回本次新识别出的 content 增量文本。"""
        stack_limit = min(stack_limit, _MAX_CONTENT_DECODER_DEPTH)
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n and not self.invalid:
            ch = text[i]
            if self.mode == "string":
                if self.hex is not None:
                    if ch not in "0123456789abcdefABCDEF":
                        self._fail()
                        break
                    self.hex += ch
                    if len(self.hex) == 4:
                        self._emit_char(chr(int(self.hex, 16)), out)
                        self.hex = None
                elif self.escape:
                    self.escape = False
                    if ch == "u":
                        self.hex = ""
                    elif ch in _ESCAPES:
                        self._emit_char(_ESCAPES[ch], out)
                    else:
                        self._fail()
                elif ch == "\\":
                    self.escape = True
                elif ch == '"':
                    self.mode = "normal"
                    if self.string_kind == "key":
                        frame = self.stack[-1] if self.stack else None
                        if frame is not None:
                            frame["key"] = self.key
                            frame["state"] = "colon"
                    else:
                        if self.string_kind == "content":
                            if self.high:
                                out.append(self.high)
                                self.high = ""
                            self.closed_content = True
                        self._value_done()
                elif ord(ch[0]) < 32:
                    self._fail()
                else:
                    self._emit_char(ch, out)
                i += 1
                continue
            if self.mode == "literal":
                self.token += ch
                if not self.expected.startswith(self.token):
                    self._fail()
                elif self.token == self.expected:
                    self.mode = "normal"
                    self._value_done()
                i += 1
                continue
            if self.mode == "number":
                if ch in "0123456789eE+.-":
                    self.token += ch
                    if len(self.token) > 128:
                        self._fail()
                    i += 1
                    continue
                if not _is_valid_number(self.token):
                    self._fail()
                    break
                self.mode = "normal"
                self._value_done()
                continue  # 回退当前字符重新走普通模式
            if ch in " \t\r\n":
                i += 1
                continue
            if self.complete:
                self._fail()
                break
            if not self.root_started:
                self.root_started = True
                if ch != "{" or stack_limit <= 0:
                    self._fail()
                    break
                self.stack.append({"type": "{", "state": "keyOrEnd", "key": ""})
                i += 1
                continue
            frame = self.stack[-1] if self.stack else None
            if frame is None:
                self._fail()
                break
            state = frame["state"]
            ftype = frame["type"]
            if ftype == "{" and state in ("keyOrEnd", "key"):
                if ch == "}" and state == "keyOrEnd":
                    self.stack.pop()
                    self._value_done()
                elif ch == '"':
                    self.mode = "string"
                    self.string_kind = "key"
                    self.key = ""
                    self.escape = False
                    self.hex = None
                else:
                    self._fail()
            elif state == "colon":
                if ch != ":":
                    self._fail()
                else:
                    frame["state"] = "value"
            elif state == "commaOrEnd":
                closer = "}" if ftype == "{" else "]"
                if ch == closer:
                    self.stack.pop()
                    self._value_done()
                elif ch == ",":
                    frame["state"] = "key" if ftype == "{" else "value"
                else:
                    self._fail()
            elif ftype == "[" and state == "valueOrEnd" and ch == "]":
                self.stack.pop()
                self._value_done()
            else:
                is_root = (
                    len(self.stack) == 1 and ftype == "{" and frame["key"] == "content"
                )
                self._start_value(ch, is_root, stack_limit)
            i += 1
        return "".join(out)

    @property
    def valid(self) -> bool:
        return self.complete and self.found and self.closed_content and not self.invalid


def _is_valid_number(token: str) -> bool:
    import re

    return bool(re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", token))


# ---------------------------------------------------------------------------
# 流式还原
# ---------------------------------------------------------------------------


# Limits on response-local display decoder state, independent of the
# executable-call assembler and any provider/request limits.
_MAX_PRESENTATION_CALLS = 256
_MAX_PRESENTATION_IDS = 512
_MAX_PRESENTATION_ID_CHARS = 1024
_MAX_PRESENTATION_DELTAS = 16_384
_MAX_PRESENTATION_PARSER_FRAMES = 4096
_MAX_PRESENTATION_PREFIX_BYTES = 64 * 1024
_MAX_PRESENTATION_CALL_BYTES = 16 * 1024 * 1024
_MAX_PRESENTATION_TOTAL_BYTES = 64 * 1024 * 1024
_INCOMPLETE_PRESENTATION_FINISH = 10  # MALFORMED_FUNCTION_CALL


def _presentation_raw(call: dict[str, object]) -> object:
    args = call.get("args")
    return args if isinstance(args, (dict, str)) else call.get("arguments")


def _presentation_size(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-8", "surrogatepass"))


@dataclass(slots=True)
class _PresentationCallState:
    """One display call's argument decoder and retained prefix."""

    call_id: str
    fragments: list[str] = field(default_factory=list)
    kept_bytes: int = 0
    decoder: ContentDecoder = field(default_factory=ContentDecoder)
    signature_emitted: bool = False
    dict_seen: bool = False
    over_limit: bool = False

    @property
    def complete(self) -> bool:
        return not self.over_limit and (self.dict_seen or self.decoder.valid)

    def decode(self, raw: object, *, stack_limit: int) -> tuple[str, str | None]:
        """Return an uncommitted delta and an optional replacement prefix."""
        if isinstance(raw, dict):
            content = raw.get("content")
            self.dict_seen = isinstance(content, str)
            return _content_delta(self, content if isinstance(content, str) else "")
        if not isinstance(raw, str) or not raw or self.decoder.invalid:
            return "", None
        delta = self.decoder.feed(raw, stack_limit=stack_limit)
        if self.decoder.invalid:
            self.decoder.stack.clear()  # An invalid raw stream cannot resume.
            logger.warning("抗截断流式参数 JSON 识别失败，跳过后续解码")
            return "", None
        return delta, None


def _content_delta(state: _PresentationCallState, new: str) -> tuple[str, str | None]:
    """Keep the existing incremental/snapshot semantics without joining history."""
    offset = 0
    for fragment in state.fragments:
        if not new.startswith(fragment, offset):
            return new, None
        offset += len(fragment)
    if len(new) > offset:
        return new[offset:], new
    return "", None


def _unconfirmed_display_name(call: dict[str, object], tool_name: str) -> bool:
    name = call.get("name")
    return (
        isinstance(name, str)
        and bool(name)
        and tool_name.startswith(name)
        and not {"args", "arguments"} & call.keys()
    )


class _PresentationTracker:
    """Bounded per-response display identities; never infer a nameless ID."""

    def __init__(self, tool_name: str, *, legacy_stream_fallback: bool = False) -> None:
        self.tool_name = tool_name
        self.seen = False
        self._legacy_stream_fallback = legacy_stream_fallback
        self._states: list[_PresentationCallState] = []
        # None marks an explicit ordinary owner: do not borrow its ID for display.
        self._by_id: dict[str, _PresentationCallState | None] = {}
        self._current: _PresentationCallState | None = None
        self._limited = self._conflict = False
        self._bytes = self._deltas = self._parser_frames = 0

    @property
    def count(self) -> int:
        return len(self._states)

    def interrupt(self) -> None:
        # Do not attribute sparse frames across an interrupted boundary.
        self._by_id.clear()
        self._current = None

    def incomplete(self, *, plain_won: bool) -> bool:
        return (
            self.seen
            and not plain_won
            and (
                self._limited
                or self._conflict
                or any(not state.complete for state in self._states)
            )
        )

    def owns(self, call: dict[str, object]) -> bool:
        name = call.get("name")
        if name == self.tool_name:
            return True
        ident = _presentation_id(call)
        if not ident or len(ident) > _MAX_PRESENTATION_ID_CHARS:
            return False
        existing = self._by_id.get(ident)
        if name is None or name == "":
            return existing is not None
        if (
            isinstance(name, str)
            and (
                existing is not None
                or not _unconfirmed_display_name(call, self.tool_name)
            )
            and (ident in self._by_id or len(self._by_id) < _MAX_PRESENTATION_IDS)
        ):
            if self._current is existing:
                self._current = None
            self._by_id[ident] = None
        return False

    def feed(
        self, call: dict[str, object]
    ) -> tuple[_PresentationCallState | None, str] | None:
        # None means "ordinary"; a pair with no state is an internal call
        # which must stay internal even when there is no safe budget/binding.
        if not self.owns(call):
            return None
        self.seen = True
        if self._limited:
            return None, ""

        ident = _presentation_id(call)
        state = self._by_id.get(ident)
        if ident and state is None:
            if ident in self._by_id:
                self._conflict = True
                return None, ""
            if (
                len(ident) > _MAX_PRESENTATION_ID_CHARS
                or len(self._by_id) >= _MAX_PRESENTATION_IDS
            ):
                self._limited = True
                return None, ""
        # Do not extend the existing streaming fallback to nameless frames
        # or change the buffered treatment of independent ID-less objects.
        if (
            state is None
            and self._legacy_stream_fallback
            and self._current is not None
            and (not ident or not self._current.call_id)
        ):
            state = self._current
        if state is None:
            if len(self._states) >= _MAX_PRESENTATION_CALLS:
                self._limited = True
                return None, ""
            state = _PresentationCallState(call_id=ident)
            self._states.append(state)
        if ident:
            state.call_id = ident
            self._by_id[ident] = state
        self._current = state

        raw = _presentation_raw(call)
        if state.dict_seen and isinstance(raw, str):
            # A raw replay cannot amend an already received structured object.
            return state, ""
        text = raw.get("content") if isinstance(raw, dict) else raw
        if isinstance(text, str) and (
            len(text) > _MAX_PRESENTATION_CALL_BYTES
            or _presentation_size(text) > _MAX_PRESENTATION_CALL_BYTES
        ):
            self._limited = state.over_limit = True
            return state, ""
        # A stream can open many JSON objects before yielding its first text.
        # Share a structural budget across those parsers, before they allocate.
        before = len(state.decoder.stack)
        stack_limit = _MAX_PRESENTATION_PARSER_FRAMES - self._parser_frames + before
        delta, snapshot = state.decode(raw, stack_limit=stack_limit)
        self._parser_frames += len(state.decoder.stack) - before
        if delta:
            size = _presentation_size(delta)
            if (
                self._deltas >= _MAX_PRESENTATION_DELTAS
                or state.kept_bytes + size > _MAX_PRESENTATION_CALL_BYTES
                or self._bytes + size > _MAX_PRESENTATION_TOTAL_BYTES
            ):
                self._limited = state.over_limit = True
                return state, ""
            if snapshot is None:
                state.fragments.append(delta)
            else:
                state.fragments = [snapshot]
            state.kept_bytes += size
            self._bytes += size
            self._deltas += 1
        return state, delta


async def _close_stream_quietly(stream: object) -> None:
    """尽力关闭上游异步迭代器，让连接在客户端断连时确定性释放。

    ``async for`` 退出（含被 aclose 打断）时不会自动关闭被迭代的异步生成器，
    只能等事件循环的 asyncgen finalizer；高断连率下会积压未释放的上游连接。
    上游 transport 的流式生成器在 finally 里 ``await resp.aclose()``，因此这里
    显式 close 能让 httpx 连接立即释放。
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:  # pragma: no cover - 关闭失败不应影响主流程
        logger.debug("关闭上游流失败: %s", exc)


async def restore_anti_truncation_stream(
    events: AsyncIterator[tuple[str, object | None]],
    ctx: AntiTruncationContext,
) -> AsyncIterator[tuple[str, object | None]]:
    """流式事件管道：把合成工具调用的参数增量实时还原为 ``body`` 事件。

    频道仲裁与上游 JS 实现一致：先到的非空正文频道胜出，后到的重复文本被
    抑制，避免"纯文本 + 工具参数"双通道重复。
    """
    try:
        async for event in _restore_anti_truncation_stream_inner(events, ctx):
            yield event
    finally:
        await _close_stream_quietly(events)


def _without_plain_text(part: ResponsePart) -> ResponsePart:
    signature = (
        part.thought_signature
        if (
            part.function_call
            or part.function_response
            or part.image
            or part.executable_code is not None
            or part.code_execution_result is not None
        )
        else ""
    )
    return replace(part, text="", text_present=False, thought_signature=signature)


async def _ordered_transport_events(
    events: AsyncIterator[tuple[str, object | None]], tracker: _PresentationTracker
) -> AsyncIterator[tuple[str, object | None]]:
    """Expose only this variant's internal display calls to its decoder."""
    async for kind, payload in events:
        if kind == "tool_interruption":
            tracker.interrupt()
        if kind != "ordered_parts" or not isinstance(payload, list):
            yield kind, payload
            continue
        ordered: list[ResponsePart] = []
        for part in payload:
            if not isinstance(part, ResponsePart):
                continue
            call = part.function_call
            if call is not None and (
                tracker.owns(call) or _unconfirmed_display_name(call, tracker.tool_name)
            ):
                ordered.append(replace(part, function_call=None, thought_signature=""))
                yield kind, ordered
                ordered = []
                yield (
                    "tool_calls",
                    [
                        {
                            key: value
                            for key, value in call.items()
                            if key != "thought_signature"
                        }
                    ],
                )
            else:
                ordered.append(part)
        if ordered:
            yield kind, ordered


async def _restore_anti_truncation_stream_inner(
    events: AsyncIterator[tuple[str, object | None]],
    ctx: AntiTruncationContext,
) -> AsyncIterator[tuple[str, object | None]]:
    if not ctx.active:
        async for event in events:
            yield event
        return

    channel: str | None = None  # None | "plain" | "synthetic"
    tracker = _PresentationTracker(ctx.tool_name, legacy_stream_fallback=True)
    real_calls_seen = False
    # Preserve the existing deferral for explicit, incomplete names; a
    # nameless frame is not evidence of a presentation prefix.
    held_prefix_calls: list[dict[str, object]] = []
    held_prefix_bytes = 0
    prefix_limited = False

    async for event_type, payload in _ordered_transport_events(events, tracker):
        if event_type == "ordered_parts" and isinstance(payload, list):
            ordered = []
            for part in payload:
                if not isinstance(part, ResponsePart):
                    continue
                if part.function_call:
                    real_calls_seen = True
                if part.text and not part.thought:
                    if channel == "synthetic":
                        part = _without_plain_text(part)
                    else:
                        channel = "plain"
                        ctx.plain_seen = True
                ordered.append(part)
            if ordered:
                yield event_type, ordered
            continue
        if event_type == "body":
            if channel == "synthetic":
                # 工具频道已胜出，抑制其后的纯文本回声。
                continue
            if payload:
                channel = "plain"
                ctx.plain_seen = True
            yield event_type, payload
            continue

        if event_type == "tool_calls" and isinstance(payload, list):
            passthrough: list[dict[str, object]] = []
            text_delta: str | None = None
            signature: tuple[_PresentationCallState, str] | None = None
            for call in payload:
                if not isinstance(call, dict):
                    continue
                update = tracker.feed(call)
                if update is not None:
                    state, delta = update
                    ctx.synthetic_seen = tracker.seen
                    ctx.multiple_calls = tracker.count > 1
                    if delta:
                        text_delta = (text_delta or "") + delta
                    sig = call.get("thought_signature")
                    if state is not None and isinstance(sig, str) and sig:
                        signature = state, sig
                elif _unconfirmed_display_name(call, ctx.tool_name):
                    # 名称尚为合成名前缀且未带参数：疑似名称分片，暂缓透传。
                    # 若后续等到带参数的完整帧（或合成调用胜出），该分片
                    # 证明是前缀碎片，丢弃即正确；否则在 done 前冲刷。
                    if channel == "synthetic" or prefix_limited:
                        continue
                    # A deferred name needs only the fields used to serialize
                    # it, not the original provider array or unknown fields.
                    prefix = {"name": str(call["name"])}
                    ident = call.get("call_id") or call.get("id")
                    sig = call.get("thought_signature")
                    if isinstance(ident, str) and ident:
                        prefix["call_id"] = ident
                    if isinstance(sig, str) and sig:
                        prefix["thought_signature"] = sig
                    remaining = _MAX_PRESENTATION_PREFIX_BYTES - held_prefix_bytes
                    size = sum(len(value) for value in prefix.values())
                    if size <= remaining:
                        size = sum(
                            _presentation_size(value) for value in prefix.values()
                        )
                    if (
                        len(held_prefix_calls) >= _MAX_PRESENTATION_CALLS
                        or size > remaining
                    ):
                        prefix_limited = True
                        held_prefix_calls.clear()
                    else:
                        held_prefix_bytes += size
                        held_prefix_calls.append(dict(prefix))
                    continue
                else:
                    real_calls_seen = True
                    # 带参数/完整名的真实调用到达：此前暂缓的名称分片
                    # 确系前缀碎片，安全丢弃
                    held_prefix_calls.clear()
                    held_prefix_bytes = 0
                    prefix_limited = False
                    passthrough.append(call)
            if text_delta is not None:
                if channel == "plain":
                    logger.info("抗截断：纯文本频道已胜出，抑制工具通道重复正文")
                else:
                    channel = "synthetic"
                    if signature is not None:
                        state, value = signature
                        if not state.signature_emitted:
                            state.signature_emitted = True
                            yield "thought_signature", value
                    yield "body", text_delta
            if passthrough:
                yield "tool_calls", passthrough
            continue

        if event_type == "finish_reason":
            if tracker.incomplete(plain_won=channel == "plain") or (
                prefix_limited and channel != "synthetic"
            ):
                # A successful upstream terminal cannot certify invalid local
                # display assembly. Never reinterpret an upstream failure.
                if (
                    isinstance(payload, int)
                    and not isinstance(payload, bool)
                    and payload == WIRE_FINISH_REASON_STOP
                ):
                    payload = _INCOMPLETE_PRESENTATION_FINISH
                elif payload == "STOP":
                    payload = "MALFORMED_FUNCTION_CALL"
            yield event_type, payload
            continue

        if event_type == "done":
            ctx.stream_done = True
            if channel == "synthetic":
                ctx.restored = True
            if held_prefix_calls and channel != "synthetic":
                # 流结束仍未等到证据：按真实调用冲刷，宁透传勿吞调用
                logger.warning(
                    "抗截断：流结束时仍有 %d 个未决名称分片，按真实调用透传",
                    len(held_prefix_calls),
                )
                yield "tool_calls", list(held_prefix_calls)
                held_prefix_calls.clear()
            elif not ctx.synthetic_seen and not ctx.plain_seen and not real_calls_seen:
                logger.info(
                    "抗截断：模型未使用工具通道且无正文输出 (transport=%s)",
                    ctx.skip_reason,
                )
            elif not ctx.synthetic_seen:
                logger.info("抗截断：模型未使用工具通道，正文走纯文本透传")
            yield event_type, payload
            continue

        yield event_type, payload


__all__ = [
    "ALIAS_VARIANT_SUFFIX",
    "BUFFER_VARIANT_SUFFIX",
    "DEFAULT_TOOL_NAME",
    "STREAM_VARIANT_SUFFIX",
    "AntiTruncationContext",
    "ContentDecoder",
    "ModelVariantResolution",
    "get_selected_variant_models",
    "prepare_anti_truncation",
    "resolve_model_variant",
    "restore_anti_truncation_output",
    "restore_anti_truncation_stream",
]
