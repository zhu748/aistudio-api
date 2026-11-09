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

import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    encode_function_declaration_to_wire,
)
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_anti_truncation_settings,
    resolve_model_defaults,
)
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioPart,
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
            "transport": "active" if self.active else (
                self.skip_reason if self.skip_reason in AUDIT_SKIP_REASONS else "unknown"
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
        if _is_function_declarations_container(tool):
            for decl in tool[1]:
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
    if isinstance(tool_config, list) and len(tool_config) > 1 and isinstance(
        tool_config[1], list
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

    # 注入合成函数声明。
    synthetic_decl = _build_synthetic_declaration(tool_name)
    if tools:
        appended = False
        for tool in tools:
            if _is_function_declarations_container(tool):
                tool[1].append(synthetic_decl)
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


def _extract_call_content(call: dict[str, object]) -> str:
    """从单个合成函数调用中提取 content 文本，兼容 dict args 与 JSON 字符串。"""
    args = call.get("args")
    if isinstance(args, dict):
        content = args.get("content")
        return content if isinstance(content, str) else ""
    raw_arguments = call.get("arguments")
    if isinstance(raw_arguments, str) and raw_arguments.strip():
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return ""
        if isinstance(parsed, dict):
            content = parsed.get("content")
            return content if isinstance(content, str) else ""
    return ""


def restore_anti_truncation_output(output: ModelOutput, ctx: AntiTruncationContext) -> None:
    """非流式响应还原：把合成工具调用参数中的正文搬回 ``candidate.text``。"""
    if not ctx.active or not output.candidates:
        return
    candidate = output.candidates[0]
    if not candidate.function_calls:
        return

    restored_parts: list[str] = []
    real_calls: list[dict[str, object]] = []
    for call in candidate.function_calls:
        if str(call.get("name") or "") == ctx.tool_name:
            ctx.synthetic_seen = True
            content = _extract_call_content(call)
            if content:
                restored_parts.append(content)
        else:
            real_calls.append(call)

    if not ctx.synthetic_seen:
        return  # 模型未走工具通道，纯文本原样透传。

    if len(restored_parts) > 1:
        ctx.multiple_calls = True
        logger.warning(
            "抗截断传输出现多次合成调用，已按顺序拼接: tool=%s, calls=%d",
            ctx.tool_name,
            len(restored_parts),
        )

    if restored_parts:
        # 与上游 JS 语义一致：工具通道的正文整体替换纯文本通道内容。
        candidate.text = "".join(restored_parts)
        ctx.restored = True
    candidate.function_calls = real_calls

    logger.info(
        "抗截断非流式还原完成: restored=%s, real_calls=%d, text_len=%d",
        ctx.restored,
        len(real_calls),
        len(candidate.text),
    )


# ---------------------------------------------------------------------------
# 流式增量 JSON 参数识别器（移植自 JS ContentDecoder）
# ---------------------------------------------------------------------------

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


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
                out.append(chr(0x10000 + ((high_code - 0xD800) << 10) + (code - 0xDC00)))
                return
            out.append(chr(high_code))
        if 0xD800 <= code <= 0xDBFF:
            self.high = ch
        else:
            out.append(ch)

    def _start_value(self, ch: str, content: bool) -> None:
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
            if len(self.stack) >= 64:
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

    def feed(self, text: str) -> str:
        """喂入一段参数字符串碎片，返回本次新识别出的 content 增量文本。"""
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
                if ch != "{":
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
                is_root = len(self.stack) == 1 and ftype == "{" and frame["key"] == "content"
                self._start_value(ch, is_root)
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


@dataclass
class _StreamCallState:
    """单个合成调用的累积状态（自适应"全量重发 / 增量追加"两种上游行为）。"""

    call_id: str | None = None
    accumulated: str = ""
    decoder: ContentDecoder = field(default_factory=ContentDecoder)
    signature_emitted: bool = False
    # 上游一旦下发过可完整解析的 dict 参数，后续字符串分片视为冗余重发。
    dict_seen: bool = False


def _merge_content(state: _StreamCallState, new: str) -> str:
    """合并新到的 content 片段，返回应作为正文增量下发的新文本。"""
    prev = state.accumulated
    if not prev:
        state.accumulated = new
        return new
    if new.startswith(prev) and len(new) > len(prev):
        # 上游重发了"到目前为止的全量"：只补发后缀。
        state.accumulated = new
        return new[len(prev) :]
    if new == prev:
        return ""
    # 增量追加模式。
    state.accumulated = prev + new
    return new


async def restore_anti_truncation_stream(
    events: AsyncIterator[tuple[str, object | None]],
    ctx: AntiTruncationContext,
) -> AsyncIterator[tuple[str, object | None]]:
    """流式事件管道：把合成工具调用的参数增量实时还原为 ``body`` 事件。

    频道仲裁与上游 JS 实现一致：先到的非空正文频道胜出，后到的重复文本被
    抑制，避免"纯文本 + 工具参数"双通道重复。
    """
    if not ctx.active:
        async for event in events:
            yield event
        return

    channel: str | None = None  # None | "plain" | "synthetic"
    call_state: _StreamCallState | None = None
    real_calls_seen = False

    def handle_synthetic_call(call: dict[str, object]) -> str | None:
        """处理一个合成调用分片，返回待下发的正文增量（可能为空）。"""
        nonlocal call_state

        ctx.synthetic_seen = True
        call_id = call.get("call_id") or call.get("id")
        call_id_str = str(call_id) if call_id is not None else None
        if call_state is None or (
            call_id_str is not None
            and call_state.call_id is not None
            and call_state.call_id != call_id_str
        ):
            if call_state is not None and call_state.accumulated:
                ctx.multiple_calls = True
            call_state = _StreamCallState(call_id=call_id_str)
        elif call_state.call_id is None and call_id_str is not None:
            call_state.call_id = call_id_str

        delta: str
        args = call.get("args")
        if isinstance(args, dict):
            call_state.dict_seen = True
            content = args.get("content")
            delta = _merge_content(
                call_state, content if isinstance(content, str) else ""
            )
        else:
            raw_fragment = (
                args if isinstance(args, str) else call.get("arguments")
            )
            if not isinstance(raw_fragment, str) or not raw_fragment:
                delta = ""
            elif call_state.dict_seen:
                # dict 参数已完整收到，字符串分片是冗余重发。
                delta = ""
            else:
                # 上游以 JSON 字符串形式分片下发参数。
                delta = call_state.decoder.feed(raw_fragment)
                if call_state.decoder.invalid:
                    logger.warning("抗截断流式参数 JSON 识别失败，跳过后续解码")
                    return None
                if delta:
                    # 与 dict 路径共享累积状态：若上游随后补发完整
                    # dict 参数帧，_merge_content 可据此去重。
                    call_state.accumulated += delta
        return delta if delta else None

    async for event_type, payload in events:
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
            signature: str | None = None
            for call in payload:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "")
                if name == ctx.tool_name:
                    delta = handle_synthetic_call(call)
                    if delta:
                        text_delta = (text_delta or "") + delta
                    sig = call.get("thought_signature")
                    if isinstance(sig, str) and sig:
                        signature = sig
                elif (
                    not name
                    or ctx.tool_name.startswith(name)
                ) and "args" not in call and "arguments" not in call:
                    # 名称尚为合成名前缀且未带参数：疑似名称分片，暂缓透传。
                    continue
                else:
                    real_calls_seen = True
                    passthrough.append(call)
            if text_delta is not None:
                if channel == "plain":
                    logger.info("抗截断：纯文本频道已胜出，抑制工具通道重复正文")
                else:
                    channel = "synthetic"
                    if signature and call_state is not None and not call_state.signature_emitted:
                        call_state.signature_emitted = True
                        yield "thought_signature", signature
                    yield "body", text_delta
            if passthrough:
                yield "tool_calls", passthrough
            continue

        if event_type == "finish_reason":
            if (
                channel == "synthetic"
                and not real_calls_seen
                and isinstance(payload, int)
                and payload in (10, 12, 13)
            ):
                # 参数已被成功解码：MALFORMED / UNEXPECTED / TOO_MANY 属于
                # 工具协议视角的收尾码，对正文通道应归一为 STOP。
                payload = 1
            yield event_type, payload
            continue

        if event_type == "done":
            ctx.stream_done = True
            if channel == "synthetic":
                ctx.restored = True
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
