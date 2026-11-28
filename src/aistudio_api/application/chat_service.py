"""Application services for chat/image orchestration."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import groupby
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiFunctionResponse,
        GeminiGenerateContentRequest,
        GeminiPart,
        GeminiTool,
    )
    from aistudio_api.infrastructure.gateway.model_defaults import ModelDefaults

from dataclasses import dataclass

# 编码器区整体迁移至 schema_wire_codec（仅保留 __all__ 中的公开重导出，
# 保持 anti_truncation.py 与既有测试的 import 路径不变）；本模块内部仍需
# 复用声明名归一化辅助。
from aistudio_api.application.schema_wire_codec import (
    SCHEMA_TYPE_CODES,
    _function_declaration_name,
    encode_function_declaration_to_wire,
    encode_response_schema_to_wire,
    encode_schema_to_wire,
)
from aistudio_api.domain.tool_signatures import (
    tool_history_text,
    valid_thought_signature_or_none as _valid_thought_signature_or_none,
)
from aistudio_api.infrastructure.gateway.model_defaults import (
    model_supports_native_prefill as _model_supports_native_prefill,
    resolve_model_defaults,
    snap_thinking_level_to_supported as _snap_thinking_level_to_supported,
    snap_thinking_wire_to_model as _snap_thinking_wire_to_model,
)
from aistudio_api.infrastructure.gateway.wire_codec import build_tools_from_names
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioImageOutputMode,
    AistudioPart,
    AistudioThinkingConfig,
    ThinkingLevel,
)
from aistudio_api.infrastructure.signature_trace import get_signature_trace
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("chat")


@dataclass
class NormalizedGeminiRequest:
    model: str
    contents: list[AistudioContent]
    system_instruction: AistudioContent | None
    tools: list[list[object]] | None
    safety_settings: list[list[object]] | None
    tool_config: list[object] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    generation_config_overrides: dict[str, object] | None = None
    # wire 名 → 客户端声明原名（仅超长截短过的函数）。生成链路据此把
    # 上游回传的截短名换回原名，客户端全程无感（AIStudio2API
    # restoreFunctionNames 移植）。
    function_name_restore: dict[str, str] | None = None


__all__ = [
    "SCHEMA_TYPE_CODES",
    "NormalizedGeminiRequest",
    "encode_function_declaration_to_wire",
    "encode_response_schema_to_wire",
    "encode_schema_to_wire",
    "normalize_gemini_request",
]


def _normalize_gemini_modalities(
    value: object, *, drop_unsupported: bool = True
) -> AistudioImageOutputMode | None:
    if value is None:
        return None
    if not isinstance(value, list):
        if drop_unsupported:
            return None
        raise ValueError("generationConfig.responseModalities must be a list")

    modalities = {str(item).strip().upper() for item in value if str(item).strip()}
    if not modalities:
        return None
    unknown = modalities - {"TEXT", "IMAGE"}
    if unknown:
        if not drop_unsupported:
            raise ValueError(
                f"Unsupported response modalities: {', '.join(sorted(unknown))}"
            )
        modalities = modalities & {"TEXT", "IMAGE"}
        if not modalities:
            return None
    if "IMAGE" not in modalities:
        return None
    if "TEXT" in modalities:
        return AistudioImageOutputMode.text_and_image()
    return AistudioImageOutputMode.image_only()


# 思考「显式关闭」词表：thinkingLevel / reasoningEffort 文本标签统一命中
# （_parse_thinking_level / _user_explicitly_disabled_thinking /
# _normalize_gemini_thinking_config 三处共用；"0" 仅数字档位路径独有）。
_THINKING_OFF_LABELS = frozenset({"NONE", "OFF", "DISABLE", "DISABLED"})


_REASONING_EFFORT_TO_LEVEL: dict[str, ThinkingLevel] = {
    "MINIMAL": ThinkingLevel.MINIMAL,
    "LOW": ThinkingLevel.LOW,
    "MEDIUM": ThinkingLevel.MEDIUM,
    "HIGH": ThinkingLevel.HIGH,
    # 目录最高思考档：2026-11 曾为 5(VERY_HIGH)，2026-11-15 真机抓包确认
    # 上移至 6(EXTRA_HIGH)——UI Extra High 档发送 [1,null,null,6] → 200，
    # 5 → 400 "Thinking level EXTRA_HIGH is not supported"。very_high /
    # xhigh 文本标签（openai/claude 兼容层透传，按 upper 后带下划线命中）
    # 一律指向 6 档，避免落到已被上游移除的 5；数值直传 5 仍解析为
    # VERY_HIGH 枚举，交由档位吸附按目录元数据纠正。
    "VERY_HIGH": ThinkingLevel.EXTRA_HIGH,
    # xhigh：较新 openai 客户端 effort 词汇（GPT-5.1-codex-max 系选择器），
    # 与目录最高档同档；分隔符变体（X-HIGH/X_HIGH/XHIGH）由下方
    # compact 匹配统一命中。
    "XHIGH": ThinkingLevel.EXTRA_HIGH,
    # max：Gemini 原生路径直接传入的顶格词汇（compat 层已归一化为
    # xhigh，此处兜底直传场景），同样指向目录最高档。
    "MAX": ThinkingLevel.EXTRA_HIGH,
}


def _parse_thinking_level(raw_level: object) -> ThinkingLevel | None:
    if raw_level is None:
        return None
    if isinstance(raw_level, ThinkingLevel):
        return raw_level
    # 合法 JSON 数字可能是 2.0 形式的浮点档位：整数值等价对待，
    # 非整浮点（2.5）落入下方标签路径抛 ValueError（400）。
    if (
        isinstance(raw_level, (int, float))
        and not isinstance(raw_level, bool)
        and (not isinstance(raw_level, float) or raw_level.is_integer())
    ):
        return ThinkingLevel(int(raw_level))
    label = str(raw_level).strip().upper()
    if not label or label in _THINKING_OFF_LABELS | {"0"}:
        return None
    if label in _REASONING_EFFORT_TO_LEVEL:
        return _REASONING_EFFORT_TO_LEVEL[label]
    # 分隔符变体兼容：VERY_HIGH / VERY-HIGH / VERYHIGH 等写法统一命中。
    compact = label.replace("_", "").replace("-", "")
    for key, value in _REASONING_EFFORT_TO_LEVEL.items():
        if key.replace("_", "") == compact:
            return value
    try:
        return ThinkingLevel[label]
    except KeyError:
        # 无效档位是客户端参数错误（400），KeyError 会漏到未分类异常
        # 路径变成 500 且误给服务账号记健康度惩罚
        raise ValueError(
            f"未识别的 thinkingLevel/reasoningEffort: {raw_level}"
        ) from None


# 思考档位吸附与浏览器形态默认值的模型自适应已下沉至基础设施层
# （model_defaults.snap_thinking_level_to_supported / snap_thinking_wire_to_model），
# 使 wire 构造层注入的 profile 默认思考档也走同一套档位兼容链路。
# 旧版 chat_service 本地实现已删除，上方以别名方式导入。


def _user_explicitly_disabled_thinking(value: object) -> bool:
    """判断用户是否显式关闭思考（thinkingLevel=NONE/OFF 或 budget=0）。

    与「空 thinkingConfig」区分：空对象（如 {} / includeThoughts 单独出现）
    表示未指定档位，profile 注入的浏览器默认档应照常生效；仅显式关闭
    才需要以 thinking_config=None 抑制默认档。
    """
    if not isinstance(value, dict):
        return False
    raw_level = value.get(
        "thinkingLevel", value.get("thinking_level", value.get("level"))
    )
    if isinstance(raw_level, str) and raw_level.strip().upper() in _THINKING_OFF_LABELS:
        return True
    raw_budget = value.get(
        "thinkingBudget", value.get("thinking_budget", value.get("budget"))
    )
    return bool(
        isinstance(raw_budget, int)
        and not isinstance(raw_budget, bool)
        and raw_budget == 0
        and raw_level is None
        and value.get("mode") is None
    )


def _coerce_json_int(value: object, field: str) -> int:
    """容错整数解析：合法 JSON 数字可能是 8192.0 形式的 float，
    ``int(str(...))`` 会把它当非法字面量抛 ValueError（合法请求被 400）。"""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"{field} must be an integer, got {value}")
    text = str(value).strip()
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer, got {value!r}") from exc


def _normalize_gemini_thinking_config(
    value: object,
) -> list[object] | None:
    if value is None or isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ValueError(
            "generationConfig.thinkingConfig must be an object or wire array"
        )

    raw_level = value.get(
        "thinkingLevel", value.get("thinking_level", value.get("level"))
    )
    raw_budget = value.get(
        "thinkingBudget", value.get("thinking_budget", value.get("budget"))
    )
    raw_mode = value.get("mode")
    raw_include = value.get("includeThoughts", value.get("include_thoughts"))

    if isinstance(raw_level, str) and raw_level.strip().upper() in _THINKING_OFF_LABELS:
        return None
    if (
        raw_include is False
        and raw_level is None
        and raw_budget is None
        and raw_mode is None
    ):
        return None

    level = _parse_thinking_level(raw_level)
    budget: int | None = None
    if raw_budget is not None and not isinstance(raw_budget, bool):
        budget = _coerce_json_int(raw_budget, "thinkingConfig.thinkingBudget")
        if budget == 0 and level is None and raw_mode is None:
            return None

    if level is None and budget is None and raw_mode is None:
        return None

    mode = (
        _coerce_json_int(raw_mode, "thinkingConfig.mode")
        if raw_mode is not None and not isinstance(raw_mode, bool)
        else 1
    )
    return AistudioThinkingConfig(level=level, mode=mode, budget=budget).to_wire()


def _normalize_gemini_image_config(
    value: object, *, drop_unsupported: bool = True
) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        if drop_unsupported:
            return {}
        raise ValueError("generationConfig.imageConfig must be an object")

    aspect_ratio = value.get("aspectRatio")
    image_size = value.get("imageSize")
    if isinstance(aspect_ratio, str) and not aspect_ratio.strip():
        aspect_ratio = None
    if isinstance(image_size, str):
        image_size = image_size.strip() or None
    person_generation = value.get("personGeneration")
    if person_generation not in (None, "") and not drop_unsupported:
        raise ValueError(
            "generationConfig.imageConfig.personGeneration is not supported yet"
        )

    normalized: dict[str, object] = {}
    if aspect_ratio is not None or image_size is not None:
        normalized["output_resolution"] = [aspect_ratio, image_size]
    return normalized


def _normalize_gemini_tool_config(
    value: object,
) -> tuple[list[object] | None, bool, list[str] | None]:
    """toolConfig → (fcc wire, mode==NONE, allowedFunctionNames) 三元组。

    is_mode_none / allowed_names 由本函数统一解析（原主函数的二次
    toolConfig 解析段合并至此），主函数删除重复解析。所有早退路径同样
    返回三元组（wire 为 None / 原值时语义位为 False / None），调用方
    无需判空再解包。

    2026-11 前端 bundle 逆向结论（boq-makersuite jspb 类 _.Rv 全字段）：
    MakerSuite GenerateContentRequest 顶层字段为 1=model, 2=contents,
    3=safety, 4=generation_config, 5=snapshot, 6=system_instruction,
    7=tools, 12=session_token, 13=conversation_tracking_mode,
    14=request_context, 15=api_key, 16=preamble_config —— **不存在
    tool_config 字段**。上游 "Please enable
    tool_config.include_server_side_tool_invocations" 400 是其后端内部
    校验（内置工具与 Function calling 组合的限制），前端 wire 层无法
    控制该开关；本网关的对策是不主动混发该组合（见 tools 注入逻辑）。

    本函数产出 fcc wire 形态供 build_request 的语义过滤（NONE 置空
    tools / allowedNames 过滤）使用；NONE 与 allowedNames 的处理也同步
    在 normalize_gemini_request 中前置执行。
    """
    if value is None:
        return None, False, None
    if isinstance(value, list):
        return value, False, None
    model_dump_fn = getattr(value, "model_dump", None)
    if callable(model_dump_fn):
        value = model_dump_fn(mode="json", exclude_none=True)
    if not isinstance(value, dict):
        return None, False, None

    fcc = value.get("functionCallingConfig") or value.get("function_calling_config")
    if not isinstance(fcc, dict):
        return None, False, None

    mode_map = {
        "MODE_UNSPECIFIED": 0,
        "AUTO": 1,
        "ANY": 2,
        "NONE": 3,
        "VALIDATED": 4,
    }
    raw_mode = fcc.get("mode", "AUTO")
    if isinstance(raw_mode, int):
        mode_code = raw_mode
    else:
        mode_code = mode_map.get(str(raw_mode).strip().upper(), 1)

    raw_allowed = fcc.get("allowedFunctionNames") or fcc.get("allowed_function_names")
    # The declaration filter appends the selected wire names after matching
    # client names. These are different namespaces for long function names.
    fcc_wire: list[object] = [mode_code]
    allowed_names: list[str] | None = None
    if isinstance(raw_allowed, list) and raw_allowed:
        allowed_names = [str(x) for x in raw_allowed]

    # includeServerSideToolInvocations 在 MakerSuite wire 协议中无槽位可
    # 编码（见函数 docstring），仅保留在语义层供观察/后续上游支持。
    include_invocations = value.get("includeServerSideToolInvocations") or value.get(
        "include_server_side_tool_invocations"
    )
    tool_config_wire: list[object] = [None, fcc_wire]
    if include_invocations is not None:
        tool_config_wire.append(bool(include_invocations))
    is_mode_none = mode_code == 3
    return tool_config_wire, is_mode_none, allowed_names


def _extract_google_search_tool_names(
    tool: GeminiTool, *, is_image_model: bool
) -> list[str]:
    if tool.googleSearchRetrieval is not None:
        return ["google_search"]

    config = tool.googleSearch
    if config is None:
        return []
    if not is_image_model or not isinstance(config, dict):
        return ["google_search"]

    search_types = config.get("searchTypes")
    if not isinstance(search_types, dict):
        return ["google_search"]

    web_enabled = search_types.get("webSearch") is not None
    image_enabled = search_types.get("imageSearch") is not None
    if web_enabled and image_enabled:
        return ["google_search_and_image_search"]
    if image_enabled:
        return ["image_search"]
    return ["google_search"]


def _filter_default_tools_for_model(
    tool_names: tuple[str, ...], *, is_image_model: bool
) -> list[str]:
    names = [str(name).strip() for name in tool_names if str(name).strip()]
    if not is_image_model:
        return names
    allowed = {"google_search", "image_search", "google_search_and_image_search"}
    return [name for name in names if name in allowed]


# 内置搜索工具的覆盖关系：复合工具覆盖其组成部分，去重时据此跳过被覆盖的窄工具。
_BUILTIN_TOOL_COVERS = {
    "google_search_and_image_search": ("google_search", "image_search"),
}


def _drop_covered_builtin_tools(names: list[str], seen: set[str]) -> list[str]:
    """从 names 去掉已被 ``seen`` 覆盖或被同批 names 覆盖的内置工具。

    例如 seen 含 google_search_and_image_search 时，google_search 被视为冗余跳过；
    同一批 names 里两者并存时也只保留复合工具。
    """
    have: set[str] = set(seen)
    result: list[str] = []
    for name in names:
        if name in have:
            continue
        if any(
            name in subs and sup in have for sup, subs in _BUILTIN_TOOL_COVERS.items()
        ):
            continue
        result.append(name)
        have.add(name)
    return result


_GEMINI_SAFETY_CATEGORY_MAP = {
    "HARM_CATEGORY_HARASSMENT": 7,
    "HARM_CATEGORY_HATE_SPEECH": 8,
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": 9,
    "HARM_CATEGORY_DANGEROUS_CONTENT": 10,
}

_GEMINI_SAFETY_THRESHOLD_MAP = {
    "BLOCK_LOW_AND_ABOVE": 1,
    "BLOCK_MEDIUM_AND_ABOVE": 2,
    "BLOCK_ONLY_HIGH": 3,
    "BLOCK_NONE": 4,
    "OFF": 5,
}


def _normalize_gemini_safety_settings(
    value: object, *, drop_unsupported: bool = True
) -> list[list[object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        if drop_unsupported:
            return []
        raise ValueError("safetySettings must be a list")

    normalized: list[list[object]] = []
    for item in value:
        raw_cat = (
            getattr(item, "category", None)
            if hasattr(item, "category")
            else (item.get("category") if isinstance(item, dict) else None)
        )
        raw_thresh = (
            getattr(item, "threshold", None)
            if hasattr(item, "threshold")
            else (item.get("threshold") if isinstance(item, dict) else None)
        )

        if raw_cat is None or raw_thresh is None:
            if drop_unsupported:
                continue
            raise ValueError(f"Unsupported safety setting entry: {item!r}")

        category = _GEMINI_SAFETY_CATEGORY_MAP.get(str(raw_cat).strip().upper())
        if category is None:
            if drop_unsupported:
                continue
            raise ValueError(f"Unsupported safety category: {raw_cat}")
        threshold = _GEMINI_SAFETY_THRESHOLD_MAP.get(str(raw_thresh).strip().upper())
        if threshold is None:
            if drop_unsupported:
                continue
            raise ValueError(f"Unsupported safety threshold: {raw_thresh}")
        normalized.append([None, None, category, threshold])
    return normalized


def _is_hollow_part(part: AistudioPart) -> bool:
    """part 无任何有效载荷（纯空文本、无签名/调用/媒体/代码）。"""
    if part.thought_signature:
        # 纯签名 part（纯文本轮的尾部签名信封）是有效载荷，不可丢弃
        return False
    if (
        part.function_call
        or part.function_response
        or part.inline_data
        or part.file_id
        or part.executable_code
        or part.code_execution_result
    ):
        return False
    return not part.text


def _is_hollow_content(content: AistudioContent) -> bool:
    """整轮无任何载荷：剥离残留或空占位（前端从不产生此形态）。"""
    return not content.parts or all(_is_hollow_part(p) for p in content.parts)


def _drop_hollow_and_merge_same_role(
    contents: list[AistudioContent],
) -> list[AistudioContent]:
    """丢弃全空轮并合并相邻同角色轮（前端同构形态）。

    2026-11-14 双错误实测（同一有毒历史，剥离后残留）：

    - 原生 Gemini 路径剥离 functionCall/functionResponse 后残留
      ``[[], role]`` 全空轮 → 上游 WAA 层 403 "The caller does not
      have permission"（同账号同签名器，纯净请求同窗口 200；
      重放 + 重签 + 换账号 6 连 403，确定性拒绝）；
    - 兼容路径空壳 ``[\"\"]`` model 轮结尾 → 过 WAA 后 RPC 400
      "Requests ending with a model turn are not supported"。

    两种形态 AI Studio 前端均不产生。丢弃全空轮 + 合并相邻同角色轮后，
    剥离残留彻底消失；清理不改变 proof 摘要（空轮对 ``" ".join``
    无贡献，合并不改变 part 平铺顺序），铸造与线缆编码天然一致。

    本函数只做纯清理：全部轮被清空时返回空列表，model 结尾兼容
    （单空格占位已废弃）交由 :func:`_rewrite_model_tail_for_continuation`
    按模型分化处理。
    """
    cleaned: list[AistudioContent] = []
    for content in contents:
        if _is_hollow_content(content):
            continue
        if cleaned and cleaned[-1].role == content.role:
            cleaned[-1].parts.extend(content.parts)
        else:
            cleaned.append(content)
    return cleaned


# 末尾 assistant 轮改写时写入系统指令的续写提示词（AIStudio2API
# DefaultContinuationPrompt 同文）：指示模型在前缀之后只续写不重复，
# 逼近原生 assistant prefill 的逐字续写语义。
_CONTINUATION_PROMPT = (
    "Continue the assistant response after the following prefix. "
    "Return only the continuation, without repeating the prefix."
)


def _assistant_prefix(contents: list[AistudioContent]) -> str:
    """按顺序拼接尾部 model 轮的续写前缀（AIStudio2API assistantPrefix）。

    正文直拼；可执行代码与执行结果转 Markdown 代码块（块间保证换行
    分隔，代码尾换行只去一个，TrimSuffix 语义）；思考 part、函数调用、
    函数结果与内联媒体不拼入——续写前缀只要模型可见的输出正文。
    """
    chunks: list[str] = []

    def block(text: str) -> None:
        if chunks and not chunks[-1].endswith("\n"):
            chunks.append("\n")
        chunks.append(text + "\n")

    for content in contents:
        for part in content.parts:
            if part.thought:
                continue
            if part.text:
                chunks.append(part.text)
            elif part.executable_code is not None:
                language, code = part.executable_code
                block(
                    "```"
                    + (language or "").lower()
                    + "\n"
                    + (code or "").removesuffix("\n")
                    + "\n```"
                )
            elif part.code_execution_result is not None:
                _, output = part.code_execution_result
                block("```text\n" + (output or "").removesuffix("\n") + "\n```")
    return "".join(chunks)


def _append_system_text(system: AistudioContent | None, text: str) -> AistudioContent:
    """把文本追加到系统指令末尾（AIStudio2API appendSystem）。

    与参考实现的差别：新建 text part 而非合并进最后一段——本项目的
    system part 可能携带 thought_signature（密码学校验绑定原文），
    往带签名的 part 追加文本会使签名失效，触发上游 400
    "Corrupted thought signature."。上游对连续 text part 与拼接文本
    等价，新建 part 语义不变。
    """
    if system is None:
        return AistudioContent(role="user", parts=[AistudioPart(text=text)])
    return AistudioContent(
        role=system.role, parts=[*system.parts, AistudioPart(text=text)]
    )


def _rewrite_model_tail_for_continuation(
    contents: list[AistudioContent],
    system_instruction: AistudioContent | None,
    *,
    rewrite: bool,
    prompt: str,
) -> tuple[list[AistudioContent], AistudioContent | None]:
    """末尾 model 轮（assistant prefill）兼容改写（AIStudio2API normalizeTurns 移植）。

    ``rewrite``（白名单外模型）时剥离末尾连续 model 轮并取续写前缀：

    - 前缀全空白 → 直接删除（空壳/thought-only 轮不保留，避免
      model-turn 400）；
    - ``prompt`` 非空（默认）→ ``prompt + "\n" + 前缀`` 并入系统指令
      （已有系统指令时前置 ``"\n"`` 分隔），对话不再以 model 轮结尾；
    - ``prompt`` 为空 → 前缀作为最后一轮 user 正文（Go 版可配置
      ContinuationPrompt="" 时的分支，保留以维持完整移植）。

    对话为空时兜底（与 rewrite 无关）：系统指令有载荷则整体转为
    user 轮（role 翻转，系统指令置 None）；否则报错——单空格 user
    占位轮已废弃（真机五形态判别证明其可过 200，但语义为空请求，
    AIStudio2API 同场景直接拒绝）。

    原生 prefill 模型（``rewrite=False``，gemini-3.1/3.0 系白名单 +
    gemma- 前缀）不进入改写分支：model 结尾直接透传，prefill 语义
    完整保留。
    """
    if rewrite:
        tail = len(contents)
        while tail > 0 and contents[tail - 1].role == "model":
            tail -= 1
        prefix = _assistant_prefix(contents[tail:])
        contents = contents[:tail]
        if prefix.strip():
            if prompt:
                instruction = f"{prompt}\n{prefix}"
                if system_instruction is not None and system_instruction.parts:
                    instruction = "\n" + instruction
                system_instruction = _append_system_text(
                    system_instruction, instruction
                )
            else:
                contents.append(
                    AistudioContent(role="user", parts=[AistudioPart(text=prefix)])
                )
    if not contents:
        if system_instruction is not None and not _is_hollow_content(
            system_instruction
        ):
            # system 有载荷（文本/图片）→ 整体转 user 轮；系统指令不再
            # 单独发送（参考实现同构：SystemText(system) 作为 user 正文）。
            return (
                [AistudioContent(role="user", parts=list(system_instruction.parts))],
                None,
            )
        raise ValueError("请求没有系统提示或对话内容")
    return contents, system_instruction


_ToolOwner = tuple[str | None, str]
_UNSUPPORTED_FILE_DATA = "fileData is not supported yet"


def _match_pending_calls(
    parts: list[GeminiPart], pending: list[_ToolOwner], legacy_names: set[str]
) -> dict[int, _ToolOwner]:
    """Resolve exact pairs before considering any ID-less result.

    Remaining same-name results with no reliable ID keep their supplied order
    and do not acquire guessed IDs. Orphans keep their payload as transcript.
    """
    if not pending:
        return {}
    # One exact pair cannot have a duplicate ID; skip the batch indexes.
    if len(pending) == len(parts) == 1:
        fr = parts[0].functionResponse
        if fr is not None and fr.id and fr.id == pending[0][0]:
            return {0: pending[0]}
    results = {
        i: part.functionResponse
        for i, part in enumerate(parts)
        if part.functionResponse is not None
    }
    call_ids = Counter(call_id for call_id, _ in pending if call_id)
    result_ids = Counter(fr.id for fr in results.values() if fr.id)
    call_by_id = {call_id: i for i, (call_id, _) in enumerate(pending) if call_id}
    owners: dict[int, _ToolOwner] = {}
    consumed: set[int] = set()
    named: dict[str, list[tuple[int, GeminiFunctionResponse]]] = defaultdict(list)
    unnamed: list[tuple[int, GeminiFunctionResponse]] = []

    for index, fr in results.items():
        if fr.id and (call_ids[fr.id] > 1 or result_ids[fr.id] > 1):
            continue
        if fr.id and fr.id in call_by_id:
            owner = call_by_id[fr.id]
            owners[index] = pending[owner]
            consumed.add(owner)
        elif fr._compat_owner.exact_id_only:
            # This ID denotes different names in the replayed history. Its
            # display label cannot establish a name-based or positional owner.
            continue
        elif fr._compat_owner.legacy_name in legacy_names:
            # An ID-less Chat function result can belong to a deprecated call
            # in this same turn. Do not consume a modern call by name or order.
            continue
        elif fr.name:
            named[fr.name].append((index, fr))
        else:
            unnamed.append((index, fr))

    by_name: dict[str, list[int]] = defaultdict(list)
    for index, (_, name) in enumerate(pending):
        if index not in consumed:
            by_name[name].append(index)

    for name, outputs in named.items():
        possible = by_name.get(name, [])
        unnamed_ids = [i for i in possible if not pending[i][0]]
        unbound = [(i, fr.id) for i, fr in outputs if fr.id]
        if len(unnamed_ids) == len(unbound) == 1:
            index, result_id = unbound[0]
            owner = unnamed_ids[0]
            owners[index] = (result_id, name)
            consumed.add(owner)
            possible = [i for i in possible if i != owner]

        positional = [index for index, fr in outputs if not fr.id]
        if len(possible) == len(positional) == 1:
            owners[positional[0]] = pending[possible[0]]
            consumed.add(possible[0])
        else:
            for index, owner in zip(positional, possible, strict=False):
                owners[index] = (None, name)
                consumed.add(owner)

    if len(unnamed) == 1 and len(consumed) + 1 == len(pending):
        owner = next(i for i in range(len(pending)) if i not in consumed)
        call_id, name = pending[owner]
        index, fr = unnamed[0]
        if not (fr.id and call_id):
            owners[index] = (fr.id or call_id, name)
    return owners


def _convert_part(
    part: GeminiPart, result_owner: _ToolOwner | None = None
) -> list[AistudioPart]:
    """单个 Gemini part → AistudioPart 列表（functionResponse 附图会展开多部件）。

    纯签名 part（历史快照残留）：签名必须过校验，失效时丢弃整个部件而非
    写入空部件（空 part 会造成全空轮，触发上游 WAA 403，交由
    _drop_hollow_and_merge_same_role 统一清理）。
    """
    parts: list[AistudioPart] = []
    if part.functionResponse is not None:
        fr = part.functionResponse
        # Result attachments are flattened below, even for orphaned results.
        # A URI cannot be represented there; do not forward a partial result.
        if fr.parts and any(child.fileData is not None for child in fr.parts):
            raise ValueError(_UNSUPPORTED_FILE_DATA)
        resp_payload = fr.response if fr.response is not None else {}
        inline = part.inlineData
        result = AistudioPart(
            text=part.text,
            inline_data=(inline.mimeType, inline.data) if inline else None,
            inline_filename=inline.filename if inline else None,
            thought=bool(part.thought),
        )
        if result_owner is None:
            transcript = tool_history_text("tool_result", fr.name, fr.id, resp_payload)
            parts.append(AistudioPart(text=transcript))
            # 没有对应 tool_use 的 tool_result：只能作为文本转写发给模型。
            # 这是客户端历史不一致（例如压缩后保留了 tool_result 却丢了
            # tool_use）的信号，之前全程静默；记一条 warning 便于排查
            # "模型为何看到伪工具结果"，并计入 thoughtSignature 追踪。
            logger.warning(
                "孤儿 tool_result 转为文本：tool=%s id=%s payload=%d 字",
                fr.name,
                fr.id or "-",
                len(transcript),
            )
            if (trace := get_signature_trace()) is not None:
                trace.record_client_orphan_result(
                    call_id=fr.id or "",
                    call_name=fr.name,
                    payload_chars=len(transcript),
                )
            if part.text is not None or inline is not None:
                parts.append(result)
        else:
            result_id, name = result_owner
            result.function_response = (
                (name, resp_payload, result_id) if result_id else (name, resp_payload)
            )
            if (
                name == fr.name
                and result_id == (fr.id or None)
                and not (
                    fr.parts
                    or part.fileData
                    or part.functionCall
                    or part.executableCode
                    or part.codeExecutionResult
                )
            ):
                result.thought_signature = _valid_thought_signature_or_none(
                    part.thoughtSignature
                )
            parts.append(result)
        if fr.parts:
            for subpart in fr.parts:
                child_inline = subpart.inlineData
                if child_inline is not None or subpart.text is not None:
                    parts.append(
                        AistudioPart(
                            text=subpart.text,
                            inline_data=(child_inline.mimeType, child_inline.data)
                            if child_inline
                            else None,
                            inline_filename=child_inline.filename
                            if child_inline
                            else None,
                            thought=bool(subpart.thought),
                            thought_signature=_valid_thought_signature_or_none(
                                subpart.thoughtSignature
                            ),
                        )
                    )
        return parts
    if part.fileData is not None:
        raise ValueError(_UNSUPPORTED_FILE_DATA)

    # Distinguish absent from empty text and keep the orthogonal wire fields
    # together. An opaque signature authenticates the whole Part, not the
    # first field that a converter happens to recognize.
    native = AistudioPart(
        text=part.text,
        thought=bool(part.thought),
        thought_signature=_valid_thought_signature_or_none(part.thoughtSignature),
    )
    if (inline := part.inlineData) is not None:
        native.inline_data = (inline.mimeType, inline.data)
        native.inline_filename = inline.filename
    if (fc := part.functionCall) is not None:
        args = fc.args if fc.args is not None else {}
        native.function_call = (fc.name, args, fc.id) if fc.id else (fc.name, args)
        native._compat_args_rewritten = fc._compat_args_rewritten
        native._compat_name_rewritten = fc._compat_name_rewritten
    if (code := part.executableCode) is not None:
        native.executable_code = (code.language, code.code)
        if code.id is not None or code.language.upper() not in (
            "PYTHON",
            "1",
            "LANGUAGE_UNSPECIFIED",
        ):
            native.thought_signature = None
    if (result := part.codeExecutionResult) is not None:
        native.code_execution_result = (result.outcome, result.output)
        if result.id is not None or result.outcome.upper() not in (
            "OUTCOME_OK",
            "OUTCOME_FAILED",
            "OUTCOME_DEADLINE_EXCEEDED",
        ):
            # The typed wire encoder does not carry code IDs or other outcomes.
            native.thought_signature = None
    return [native] if native.text is not None or not _is_hollow_part(native) else []


def _normalize_contents(req: GeminiGenerateContentRequest) -> list[AistudioContent]:
    """Normalize logical turns without changing call/result or attachment order."""
    contents: list[AistudioContent] = []
    pending: list[_ToolOwner] = []
    pending_legacy_names: set[str] = set()

    def result_run(content: GeminiContent) -> tuple[str, bool]:
        role = content.role or "user"
        return role, role != "model" and any(p.functionResponse for p in content.parts)

    def is_blank_user_content(content: GeminiContent) -> bool:
        # An empty user placeholder is not a new turn. Drop it before
        # detecting result runs; later hollow cleanup cannot restore pairing.
        # Explicit signature carriers and non-text payloads are not blank.
        return (content.role or "user") == "user" and not any(
            part.text
            or part.thoughtSignature
            or part.inlineData
            or part.fileData
            or part.functionCall
            or part.functionResponse
            or part.executableCode
            or part.codeExecutionResult
            for part in content.parts
        )

    trace = get_signature_trace(create=True)
    record_client_in = trace is not None and not trace._client_in_normalized
    if trace is not None and record_client_in:
        trace._client_in_normalized = True
    model_turn_index = 0

    nonblank = (item for item in req.contents if not is_blank_user_content(item))
    for (role, has_results), run in groupby(nonblank, key=result_run):
        incoming: list[GeminiPart] = []
        if role == "model":
            pending_legacy_names.clear()
        for content in run:
            incoming.extend(content.parts)
            if role == "model" and (name := content._compat_owner.legacy_name):
                pending_legacy_names.add(name)
        if role == "model" and trace is not None and record_client_in:
            model_turn_index += 1
            trace.client_model_turns += 1
            for part in incoming:
                stripped_note = ""
                if (
                    (fr := part.functionResponse) is not None
                    and part.thoughtSignature
                    and (
                        fr.parts
                        or part.fileData
                        or part.functionCall
                        or part.executableCode
                        or part.codeExecutionResult
                    )
                ):
                    stripped_note = "compound_part_stripped"
                elif (
                    (code := part.executableCode) is not None
                    and part.thoughtSignature
                    and (
                        code.id is not None
                        or code.language.upper()
                        not in ("PYTHON", "1", "LANGUAGE_UNSPECIFIED")
                    )
                ):
                    stripped_note = "lossy_code_stripped"
                elif (
                    (res := part.codeExecutionResult) is not None
                    and part.thoughtSignature
                    and (
                        res.id is not None
                        or res.outcome.upper()
                        not in (
                            "OUTCOME_OK",
                            "OUTCOME_FAILED",
                            "OUTCOME_DEADLINE_EXCEEDED",
                        )
                    )
                ):
                    stripped_note = "lossy_code_result_stripped"
                fc = part.functionCall
                carrier = (
                    "function_call"
                    if fc is not None
                    else "function_response"
                    if part.functionResponse is not None
                    else "inline_data"
                    if part.inlineData is not None
                    else "code"
                    if (
                        part.executableCode is not None
                        or part.codeExecutionResult is not None
                    )
                    else "thought"
                    if part.thought
                    else "tail_empty"
                    if (part.text == "" or part.text is None)
                    else "text"
                )
                trace.record_client_part(
                    carrier=carrier,
                    raw_signature=part.thoughtSignature,
                    source=trace.lookup_inbound_source(part),
                    call_id=fc.id or "" if fc is not None else "",
                    call_name=fc.name if fc is not None else "",
                    turn_index=model_turn_index,
                    stripped_note=stripped_note,
                )
        if role == "model":
            # These results were emitted by the model alongside its response.
            # Client results are matched against pending calls on user turns.
            owners = {
                i: (fr.id or None, fr.name)
                for i, part in enumerate(incoming)
                if (fr := part.functionResponse) is not None
                and fr.name
                and not fr._compat_owner.exact_id_only
            }
        else:
            owners = (
                _match_pending_calls(incoming, pending, pending_legacy_names)
                if has_results
                else {}
            )
        parts = [
            p
            for i, part in enumerate(incoming)
            for p in _convert_part(part, owners.get(i))
        ]
        if role == "model":
            pending = [
                (call[2] if len(call) > 2 else None, call[0])
                for part in parts
                if (call := part.function_call) is not None
            ]
        elif has_results or role == "user":
            pending = []
            pending_legacy_names.clear()
        contents.append(AistudioContent(role=role, parts=parts))
    return _drop_hollow_and_merge_same_role(contents)


def _normalize_system_instruction(
    req: GeminiGenerateContentRequest,
) -> AistudioContent | None:
    """systemInstruction → AistudioContent（无则 None）。

    systemInstruction 的 part 签名同样过校验（与正文 part 同标准，
    多路径不一致正是 B1 类缺陷的温床）。
    """
    if req.systemInstruction is None:
        return None
    if (trace := get_signature_trace()) is not None:
        for part in req.systemInstruction.parts:
            if (
                part.thoughtSignature
                and _valid_thought_signature_or_none(part.thoughtSignature) is None
            ):
                trace.record_client_drop(
                    carrier="system",
                    raw_signature=part.thoughtSignature,
                    source=trace.lookup_inbound_source(part),
                    note="invalid_base64",
                )
    return AistudioContent(
        role=req.systemInstruction.role or "user",
        parts=[
            AistudioPart(
                text=part.text,
                thought_signature=_valid_thought_signature_or_none(
                    part.thoughtSignature
                ),
            )
            if part.text is not None
            else AistudioPart(
                inline_data=(part.inlineData.mimeType, part.inlineData.data)
                if part.inlineData
                else ("", ""),
                thought_signature=_valid_thought_signature_or_none(
                    part.thoughtSignature
                ),
            )
            for part in req.systemInstruction.parts
            if part.text is not None or part.inlineData is not None
        ],
    )


def _normalize_tools(
    req: GeminiGenerateContentRequest,
    model: str,
    defaults: ModelDefaults,
) -> tuple[list[list[object]] | None, dict[str, str], list[object] | None]:
    """tools → (候选工具容器列表, wire 名→原名还原表, toolConfig fcc wire)。

    toolConfig 的语义处理（mode==NONE 置空 / allowedFunctionNames 过滤）
    合并自 _normalize_gemini_tool_config 的三元组返回值，本函数内完成：

    - 仅当客户端未传 tools 时注入 config.yaml 的 default_tools。若客户端
      显式声明了 tools（如自定义函数声明），绝不自动混入 Google Search 等
      内置工具，避免触发 Google MakerSuite "Please enable
      tool_config.include_server_side_tool_invocations" 400 报错。
    - mode 为 NONE：显式置空 tools 与 tool_config，确保模型仅输出纯文本
      且不调用任何工具。
    - allowedFunctionNames 按客户端原名筛选声明，不影响内置工具。
      编码器的二次过滤使用实际选中声明的 wire 名，而非客户端原名。
    """
    tools: list[list[object]] | None = None
    seen_builtin: set[str] = set()
    tool_config, is_mode_none, allowed_names = _normalize_gemini_tool_config(
        getattr(req, "toolConfig", None)
    )
    allowed = (
        set(allowed_names) if allowed_names is not None and not is_mode_none else None
    )
    selected: dict[str, str] = {}
    # wire 名 → 原名（仅超长截短过的声明）；生成链路据此在响应侧换回
    function_name_restore: dict[str, str] = {}
    if req.tools is not None:
        tools = []
        all_function_declarations: list[list[object]] = []
        for tool in req.tools:
            builtin_tool_names: list[str] = []
            if tool.codeExecution is not None:
                builtin_tool_names.append("code_execution")
            if tool.functionDeclarations:
                for decl in tool.functionDeclarations:
                    encoded = encode_function_declaration_to_wire(decl)
                    decl_name = _function_declaration_name(decl)
                    if allowed is not None and decl_name not in allowed:
                        continue
                    wire_name = encoded[0]
                    if decl_name and isinstance(wire_name, str):
                        if (
                            allowed is not None
                            and selected.setdefault(wire_name, decl_name) != decl_name
                        ):
                            raise ValueError(
                                "Selected functions have the same wire name; rename one of them"
                            )
                        if wire_name != decl_name:
                            function_name_restore[wire_name] = decl_name
                    all_function_declarations.append(encoded)
            builtin_tool_names.extend(
                _extract_google_search_tool_names(
                    tool, is_image_model=defaults.is_image_model
                )
            )
            if tool.googleMaps is not None:
                builtin_tool_names.append("google_maps")
            if tool.urlContext is not None:
                builtin_tool_names.append("url_context")
            if builtin_tool_names:
                filtered_builtin = _drop_covered_builtin_tools(
                    builtin_tool_names, seen_builtin
                )
                if filtered_builtin:
                    tools.extend(
                        build_tools_from_names(
                            filtered_builtin,
                            model=model,
                            is_image_model=defaults.is_image_model,
                            drop_unsupported=defaults.drop_unsupported_params,
                        )
                    )
                    seen_builtin.update(filtered_builtin)
        if all_function_declarations:
            tools.append([None, all_function_declarations])

    if req.tools is None and defaults.default_tools:
        default_tool_names = _filter_default_tools_for_model(
            defaults.default_tools,
            is_image_model=defaults.is_image_model,
        )
        injected = (
            build_tools_from_names(
                default_tool_names,
                model=model,
                is_image_model=defaults.is_image_model,
            )
            if default_tool_names
            else []
        )
        tools = injected if injected else None

    if is_mode_none:
        return None, function_name_restore, None
    if allowed is not None and tool_config is not None:
        # Only original names of actual declarations can grant their wire alias.
        # Hashing arbitrary allowlist entries could authorize a different literal.
        fcc = tool_config[1]
        if isinstance(fcc, list):
            fcc.append(list(selected))
        if req.tools and not tools:
            tools = None
    return tools, function_name_restore, tool_config


def _normalize_generation_overrides(
    req: GeminiGenerateContentRequest,
    model: str,
    defaults: ModelDefaults,
) -> dict[str, object]:
    """generationConfig → 用户显式指定的生成参数覆盖表（无则空 dict）。

    只透传用户显式指定的生成参数：profile 的浏览器形态默认值
    （max_tokens/temperature/top_p/top_k/thinking/…）由 wire_codec.build_request
    统一注入并做模型自适应（元数据默认档位/flag13/输出上限钳制/清除语义），
    这里不再预先合并 —— 否则 build_request 将无法区分「用户指定」与
    「profile 默认」，钳制与档位自适应会因此失效。

    顶层 reasoningEffort（generationConfig 缺席时仍生效）在函数末尾统一
    处理。返回 dict（可能为空）；主函数以 ``or None`` 归一化无覆盖场景。
    """
    overrides: dict[str, object] = {}
    generation_config = req.generationConfig
    if generation_config is not None:
        if generation_config.stopSequences is not None:
            overrides["stop_sequences"] = generation_config.stopSequences
        if generation_config.maxOutputTokens is not None:
            overrides["max_tokens"] = generation_config.maxOutputTokens
        if generation_config.temperature is not None:
            overrides["temperature"] = generation_config.temperature
        if generation_config.topP is not None:
            overrides["top_p"] = generation_config.topP
        if generation_config.topK is not None:
            overrides["top_k"] = generation_config.topK
        if generation_config.responseMimeType is not None:
            overrides["response_mime_type"] = generation_config.responseMimeType
        if generation_config.responseSchema is not None:
            response_schema = generation_config.responseSchema
            overrides["response_schema"] = (
                encode_response_schema_to_wire(response_schema)
                if isinstance(response_schema, dict)
                else response_schema
            )
        if generation_config.presencePenalty is not None:
            overrides["presence_penalty"] = generation_config.presencePenalty
        if generation_config.frequencyPenalty is not None:
            overrides["frequency_penalty"] = generation_config.frequencyPenalty
        if generation_config.responseLogprobs is not None:
            overrides["response_logprobs"] = generation_config.responseLogprobs
        if generation_config.logprobs is not None:
            overrides["logprobs"] = generation_config.logprobs
        if generation_config.seed is not None:
            overrides["seed"] = generation_config.seed
        if generation_config.mediaResolution is not None:
            # 官方 REST 是字符串枚举，wire 槽位 17 只收 int（1/2/3）；
            # 字符串原样入 wire 会被上游拒绝或静默忽略
            media_res = generation_config.mediaResolution
            if isinstance(media_res, str):
                media_res = media_res.strip().upper().removeprefix("MEDIA_RESOLUTION_")
                media_res = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(media_res)
            if isinstance(media_res, (int, float)) and not isinstance(media_res, bool):
                overrides["media_resolution"] = int(media_res)
            # 无法识别的形态按 drop 策略忽略（不透传非法值）
        if generation_config.thinkingConfig is not None:
            normalized_thinking = _normalize_gemini_thinking_config(
                generation_config.thinkingConfig
            )
            if normalized_thinking is not None:
                # 模型档位兼容：等级不支持时自动升/降档，无思考能力则丢弃
                normalized_thinking = _snap_thinking_wire_to_model(
                    model, normalized_thinking
                )
            if normalized_thinking is not None:
                overrides["thinking_config"] = normalized_thinking
            elif _user_explicitly_disabled_thinking(generation_config.thinkingConfig):
                # 用户显式关闭思考 → 显式置 None，抑制 profile 注入的浏览器默认档
                overrides["thinking_config"] = None
            # 其余（空 thinkingConfig 等）不注入 → profile 浏览器默认档生效
        if generation_config.responseModalities is not None:
            image_output_mode = _normalize_gemini_modalities(
                generation_config.responseModalities,
                drop_unsupported=defaults.drop_unsupported_params,
            )
            if image_output_mode is not None:
                overrides["image_output_mode"] = image_output_mode
        if generation_config.imageConfig is not None:
            overrides.update(
                _normalize_gemini_image_config(
                    generation_config.imageConfig,
                    drop_unsupported=defaults.drop_unsupported_params,
                )
            )

    # reasoningEffort（generationConfig 优先，req 兜底）：仅当未显式给
    # thinkingConfig 时生效，避免双重指定互相覆盖。
    effective_effort = (
        generation_config.reasoningEffort if generation_config else None
    ) or req.reasoningEffort
    if effective_effort is not None and (
        generation_config is None or generation_config.thinkingConfig is None
    ):
        level = _parse_thinking_level(effective_effort)
        if level is not None:
            # 模型档位兼容：等级不支持时自动升/降档，无思考能力则不注入
            snapped = _snap_thinking_level_to_supported(model, level)
            if snapped is not None:
                overrides["thinking_config"] = AistudioThinkingConfig(
                    level=snapped, mode=1
                ).to_wire()
        else:
            # reasoningEffort 显式为 none/off → 抑制 profile 默认档
            overrides["thinking_config"] = None
    return overrides


def normalize_gemini_request(
    req: GeminiGenerateContentRequest, requested_model: str
) -> NormalizedGeminiRequest:
    """Gemini 官方请求 → AIStudio wire 请求（编排层，具体域见各 _normalize_* 辅助）。"""
    if not req.contents:
        raise ValueError("contents is required")

    model = (
        requested_model
        if requested_model.startswith("models/")
        else f"models/{requested_model}"
    )

    contents = _normalize_contents(req)
    system_instruction = _normalize_system_instruction(req)

    # 末尾 model 轮（assistant prefill）兼容按模型分化（AIStudio2API
    # normalizeTurns 移植）：原生 prefill 模型（白名单 + gemma-）透传；
    # 其余模型剥离尾部 model 轮，正文前缀以续写提示词并入系统指令，
    # 对话恒以 user 轮收尾（单空格占位轮已废弃）。
    contents, system_instruction = _rewrite_model_tail_for_continuation(
        contents,
        system_instruction,
        rewrite=not _model_supports_native_prefill(model),
        prompt=_CONTINUATION_PROMPT,
    )

    model_defaults = resolve_model_defaults(model)
    tools, function_name_restore, tool_config = _normalize_tools(
        req, model, model_defaults
    )
    generation_config_overrides = _normalize_generation_overrides(
        req, model, model_defaults
    )
    generation_config = req.generationConfig

    return NormalizedGeminiRequest(
        model=model,
        contents=contents,
        system_instruction=system_instruction,
        tools=tools if tools is not None else None,
        tool_config=tool_config,
        safety_settings=_normalize_gemini_safety_settings(
            req.safetySettings,
            drop_unsupported=model_defaults.drop_unsupported_params,
        )
        if req.safetySettings is not None
        else None,
        temperature=generation_config.temperature if generation_config else None,
        top_p=generation_config.topP if generation_config else None,
        top_k=generation_config.topK if generation_config else None,
        max_tokens=generation_config.maxOutputTokens if generation_config else None,
        generation_config_overrides=generation_config_overrides or None,
        function_name_restore=function_name_restore or None,
    )
