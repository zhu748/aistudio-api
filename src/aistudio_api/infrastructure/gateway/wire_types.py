"""Typed representations for the reverse-engineered AI Studio wire body."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import IntEnum

from .wire_spec import GenerationConfigIndex, JspbArray, PartIndex

# Playground GenerateContent / CountTokens 接口接受的最长函数名字节数
# （AIStudio2API maxWireFunctionNameLength 同源）。
MAX_WIRE_FUNCTION_NAME_LENGTH = 63


def wire_function_name(name: str) -> str:
    """发往 Playground 的函数名：超长截短并以原名的 sha256 哈希结尾。

    AIStudio2API wireFunctionName 移植。函数名超 63 字节会被上游拒绝，
    截短到 63 - len(suffix) 后追加 "_" + sha256(原名)前 6 字节的 hex，
    保证稳定且概率性唯一；响应侧凭请求内声明的映射换回原名。

    与参考实现的差别：Go 按字节切片，可能切断多字节 UTF-8 序列；
    Python 字符串无法持有半个字符，退到不超字节预算的最大字符前缀，
    产物仍保证 ≤ 63 字节且后缀不变。
    """
    name = str(name)
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_WIRE_FUNCTION_NAME_LENGTH:
        return name
    digest = hashlib.sha256(encoded).digest()
    suffix = "_" + digest[:6].hex()
    budget = MAX_WIRE_FUNCTION_NAME_LENGTH - len(suffix)
    prefix = name
    while len(prefix.encode("utf-8")) > budget:
        prefix = prefix[:-1]
    return prefix + suffix


# wire inlineData filename 的清洗上限：名称过长会无谓放大请求体且
# 对模型无额外信息量，截断到与主流文件系统 basename 同量级。
MAX_WIRE_INLINE_FILENAME_LENGTH = 255


def _wire_inline_filename(value: object) -> str | None:
    """inlineData 槽位 5 文件名的最后防线清洗。

    非 str / 空白串 / 含控制字符 / 超长一律归一：控制字符剔除，超长按
    字符截断（保持 UTF-8 完整性），清洗后为空则视为未提供。
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value.strip() if ch >= " " and ch != "\x7f").strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_WIRE_INLINE_FILENAME_LENGTH:
        cleaned = cleaned[:MAX_WIRE_INLINE_FILENAME_LENGTH].rstrip()
    return cleaned or None


class ThinkingLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    MINIMAL = 4
    # 2026-11 上游 ListModels 目录曾实测 gemini-4-hydrogen thinkingLevels=
    # [1,2,3,5]（默认档 3）。2026-11-15 Reqable 真机抓包（双样本 200）：
    # UI「Extra High」档现发送 [1,null,null,6]，同日对 5 档上游报 400
    # "Thinking level EXTRA_HIGH is not supported"——目录最高档已上移至 6，
    # 5 不再受支持。VERY_HIGH(5) 仅为数值直传与吸附排序保留。
    VERY_HIGH = 5
    EXTRA_HIGH = 6


class MediaResolution(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class ImageOutputType(IntEnum):
    IMAGE = 2


@dataclass(frozen=True)
class AistudioThinkingConfig:
    level: ThinkingLevel | None = None
    mode: int = 1
    budget: int | None = None

    def to_wire(self) -> list[object]:
        wire = JspbArray([self.mode])
        if self.budget is not None:
            wire[1] = int(self.budget)
        if self.level is not None:
            wire[3] = int(self.level)
        return wire.compact()


@dataclass(frozen=True)
class AistudioImageOutputMode:
    output_type: ImageOutputType = ImageOutputType.IMAGE
    include_text: bool = False

    def to_wire(self) -> list[int]:
        if self.include_text:
            return [int(self.output_type), 1]
        return [int(self.output_type)]

    @classmethod
    def image_only(cls) -> AistudioImageOutputMode:
        return cls(include_text=False)

    @classmethod
    def text_and_image(cls) -> AistudioImageOutputMode:
        return cls(include_text=True)


@dataclass
class AistudioGenerationConfig:
    values: list = field(default_factory=list)

    @property
    def stop_sequences(self):
        idx = GenerationConfigIndex.STOP_SEQUENCES
        return self.values[idx] if len(self.values) > idx else None

    @stop_sequences.setter
    def stop_sequences(self, value):
        self._set_slot(GenerationConfigIndex.STOP_SEQUENCES, value)

    @property
    def max_tokens(self):
        idx = GenerationConfigIndex.MAX_TOKENS
        return self.values[idx] if len(self.values) > idx else None

    @max_tokens.setter
    def max_tokens(self, value):
        self._set_slot(GenerationConfigIndex.MAX_TOKENS, value)

    @property
    def temperature(self):
        idx = GenerationConfigIndex.TEMPERATURE
        return self.values[idx] if len(self.values) > idx else None

    @temperature.setter
    def temperature(self, value):
        self._set_slot(GenerationConfigIndex.TEMPERATURE, value)

    @property
    def top_p(self):
        idx = GenerationConfigIndex.TOP_P
        return self.values[idx] if len(self.values) > idx else None

    @top_p.setter
    def top_p(self, value):
        self._set_slot(GenerationConfigIndex.TOP_P, value)

    @property
    def top_k(self):
        idx = GenerationConfigIndex.TOP_K
        return self.values[idx] if len(self.values) > idx else None

    @top_k.setter
    def top_k(self, value):
        self._set_slot(GenerationConfigIndex.TOP_K, value)

    @property
    def response_mime_type(self):
        idx = GenerationConfigIndex.RESPONSE_MIME_TYPE
        return self.values[idx] if len(self.values) > idx else None

    @response_mime_type.setter
    def response_mime_type(self, value):
        self._set_slot(GenerationConfigIndex.RESPONSE_MIME_TYPE, value)

    @property
    def response_schema(self):
        idx = GenerationConfigIndex.RESPONSE_SCHEMA
        return self.values[idx] if len(self.values) > idx else None

    @response_schema.setter
    def response_schema(self, value):
        self._set_slot(GenerationConfigIndex.RESPONSE_SCHEMA, value)

    @property
    def presence_penalty(self):
        idx = GenerationConfigIndex.PRESENCE_PENALTY
        return self.values[idx] if len(self.values) > idx else None

    @presence_penalty.setter
    def presence_penalty(self, value):
        self._set_slot(GenerationConfigIndex.PRESENCE_PENALTY, value)

    @property
    def frequency_penalty(self):
        idx = GenerationConfigIndex.FREQUENCY_PENALTY
        return self.values[idx] if len(self.values) > idx else None

    @frequency_penalty.setter
    def frequency_penalty(self, value):
        self._set_slot(GenerationConfigIndex.FREQUENCY_PENALTY, value)

    @property
    def response_logprobs(self):
        idx = GenerationConfigIndex.RESPONSE_LOGPROBS
        return self.values[idx] if len(self.values) > idx else None

    @response_logprobs.setter
    def response_logprobs(self, value):
        self._set_slot(GenerationConfigIndex.RESPONSE_LOGPROBS, value)

    @property
    def logprobs(self):
        idx = GenerationConfigIndex.LOGPROBS
        return self.values[idx] if len(self.values) > idx else None

    @logprobs.setter
    def logprobs(self, value):
        self._set_slot(GenerationConfigIndex.LOGPROBS, value)

    @property
    def unknown_flag_13(self):
        """上游未知开关字段（模型相关：目录 [83]=1 的新代模型恒携 1，透传保真用）。"""
        idx = GenerationConfigIndex.UNKNOWN_FLAG_13
        return self.values[idx] if len(self.values) > idx else None

    @unknown_flag_13.setter
    def unknown_flag_13(self, value):
        self._set_slot(GenerationConfigIndex.UNKNOWN_FLAG_13, value)

    @property
    def unknown_flag_45(self):
        """上游未知开关字段（目录 [85] 非空的 gemini-4 系恒携 1，透传保真用）。"""
        idx = GenerationConfigIndex.UNKNOWN_FLAG_45
        return self.values[idx] if len(self.values) > idx else None

    @unknown_flag_45.setter
    def unknown_flag_45(self, value):
        self._set_slot(GenerationConfigIndex.UNKNOWN_FLAG_45, value)

    @property
    def image_output_mode(self):
        idx = GenerationConfigIndex.IMAGE_OUTPUT_MODE
        return self.values[idx] if len(self.values) > idx else None

    @image_output_mode.setter
    def image_output_mode(self, value):
        if isinstance(value, AistudioImageOutputMode):
            value = value.to_wire()
        self._set_slot(GenerationConfigIndex.IMAGE_OUTPUT_MODE, value)

    @property
    def thinking_config(self):
        idx = GenerationConfigIndex.THINKING_CONFIG
        return self.values[idx] if len(self.values) > idx else None

    @thinking_config.setter
    def thinking_config(self, value):
        self._set_slot(GenerationConfigIndex.THINKING_CONFIG, value)

    @property
    def media_resolution(self):
        idx = GenerationConfigIndex.MEDIA_RESOLUTION
        return self.values[idx] if len(self.values) > idx else None

    @media_resolution.setter
    def media_resolution(self, value):
        if isinstance(value, MediaResolution):
            value = int(value)
        self._set_slot(GenerationConfigIndex.MEDIA_RESOLUTION, value)

    @property
    def seed(self):
        idx = GenerationConfigIndex.SEED
        return self.values[idx] if len(self.values) > idx else None

    @seed.setter
    def seed(self, value):
        self._set_slot(GenerationConfigIndex.SEED, value)

    @property
    def output_resolution(self):
        idx = GenerationConfigIndex.OUTPUT_RESOLUTION
        return self.values[idx] if len(self.values) > idx else None

    @output_resolution.setter
    def output_resolution(self, value):
        self._set_slot(GenerationConfigIndex.OUTPUT_RESOLUTION, value)

    def _set_slot(self, idx: int, value: object) -> None:
        if value is None:
            if len(self.values) > idx:
                self.values[idx] = None
                while self.values and self.values[-1] is None:
                    self.values.pop()
            return
        self._ensure_len(idx + 1)
        self.values[idx] = value

    def _ensure_len(self, size: int):
        while len(self.values) < size:
            self.values.append(None)


@dataclass
class AistudioPart:
    text: str | None = None
    inline_data: tuple[str, str] | None = None
    # wire inlineData 槽位 5 的上传文件名（2026-11 AI Studio 真机抓包：
    # [mime, b64, null, null, null, filename]）。独立字段而非并入二元组，
    # 避免破坏既有 `mime, data = part.inline_data` 解包点。
    inline_filename: str | None = None
    file_id: str | None = None
    function_call: tuple[str, object] | tuple[str, object, str] | None = None
    function_response: tuple[str, object] | tuple[str, object, str] | None = None
    executable_code: tuple[str, str] | None = None
    code_execution_result: tuple[str, str] | None = None
    thought_signature: str | None = None
    thought: bool = False
    raw_wire: list[object] | None = field(default=None, repr=False)

    def to_wire(self) -> list[object]:
        if self.raw_wire is not None:
            # 历史轮回放 part：raw_wire 由 model_turns 注入（每次 prepare
            # 从缓存的 JSON 字符串 loads 出全新树）或 normalize 构建一次后
            # 在 403 重试间复用。全部调用方（encode/json.dumps/摘要计算）
            # 均只读；原先的整树 deepcopy 在多模态请求（MB 级 base64）上
            # 每次编码白付一次全量深拷贝。顶层浅拷贝保留「返回独立列表」
            # 语义（调用方对顶层做 append/下标写不影响本 part），嵌套子
            # 数组按只读契约共享。
            return list(self.raw_wire)
        wire = JspbArray()
        if self.thought:
            wire[PartIndex.THOUGHT_ALT_FLAG] = 1
        if self.thought_signature:
            wire[PartIndex.THOUGHT_SIGNATURE] = self.thought_signature
        # wire 各槽位正交：同一 part 可同时携带 text 与 inline_data /
        # function_call（如"图片+图注"）。旧实现的提前 return 会把
        # self.text 静默丢弃，故所有载荷写入后统一在末尾落 text。
        if self.file_id:
            wire[PartIndex.FILE_DATA] = [self.file_id]
        if self.inline_data:
            mime, b64 = self.inline_data
            inline_wire = JspbArray([mime, b64])
            filename = _wire_inline_filename(self.inline_filename)
            if filename is not None:
                # 槽位 5 = filename；2-4 由 JspbArray 自动补 null，
                # compact 后还原抓包形态 [mime, b64, null, null, null, filename]
                inline_wire[5] = filename
            wire[PartIndex.INLINE_DATA] = inline_wire.compact()
        if self.function_call:
            # 声明侧超长名已截短（wire_function_name），历史回传的调用/
            # 结果名必须同样截短才能与声明配对
            name = wire_function_name(self.function_call[0])
            args = self.function_call[1] if len(self.function_call) > 1 else {}
            call_id = self.function_call[2] if len(self.function_call) > 2 else None
            function_call = [name, _encode_wire_args(args)]
            if call_id:
                function_call.append(call_id)
            wire[PartIndex.FUNCTION_CALL_ALT] = function_call
        if self.function_response:
            name = wire_function_name(self.function_response[0])
            response = (
                self.function_response[1] if len(self.function_response) > 1 else {}
            )
            call_id = (
                self.function_response[2] if len(self.function_response) > 2 else None
            )
            function_response = [name, _encode_wire_args(response)]
            if call_id:
                function_response.append(call_id)
            wire[PartIndex.FUNCTION_RESPONSE_ALT] = function_response
        if self.executable_code:
            lang = self.executable_code[0]
            code = self.executable_code[1] if len(self.executable_code) > 1 else ""
            lang_code = 1 if str(lang).upper() in ("1", "PYTHON") else 0
            wire[PartIndex.EXECUTABLE_CODE] = [lang_code, code]
        # 一条上游 part 常同时带代码与执行结果（下标 8/9），必须分别写入，
        # 用 if/return 串联会丢掉后半部分。
        if self.code_execution_result:
            outcome = self.code_execution_result[0]
            output = (
                self.code_execution_result[1]
                if len(self.code_execution_result) > 1
                else ""
            )
            outcome_map = {
                "OUTCOME_OK": 1,
                "OUTCOME_FAILED": 2,
                "OUTCOME_DEADLINE_EXCEEDED": 3,
            }
            outcome_code = (
                outcome_map.get(str(outcome).upper(), 1)
                if isinstance(outcome, str)
                else int(outcome)
            )
            wire[PartIndex.CODE_RESULT] = [outcome_code, output]
        if self.text is not None:
            wire[PartIndex.TEXT] = self.text
        return wire.compact()


def _encode_wire_args(value):
    if isinstance(value, dict):
        return [_encode_wire_struct_fields(value)]
    return value


def _encode_wire_struct_fields(d: dict):
    return [[key, _encode_wire_value(val)] for key, val in d.items()]


def _encode_wire_value(value):
    if value is None:
        return [0]
    if isinstance(value, bool):
        return [None, None, None, value]
    if isinstance(value, (int, float)):
        return [None, value]
    if isinstance(value, str):
        return [None, None, value]
    if isinstance(value, dict):
        return [None, None, None, None, [_encode_wire_struct_fields(value)]]
    if isinstance(value, list):
        return [
            None,
            None,
            None,
            None,
            None,
            [[_encode_wire_value(item) for item in value]],
        ]
    return [None, None, str(value)]


@dataclass
class AistudioContent:
    role: str
    parts: list[AistudioPart]

    def to_wire(self):
        return [[part.to_wire() for part in self.parts], self.role]

    @classmethod
    def from_user_prompt(
        cls,
        prompt: str,
        images: list[tuple[str, str]] | None = None,
    ) -> AistudioContent:
        parts: list[AistudioPart] = [
            AistudioPart(inline_data=item) for item in (images or [])
        ]
        parts.append(AistudioPart(text=prompt))
        return cls(role="user", parts=parts)


@dataclass
class AistudioRequest:
    model: str
    contents: list[AistudioContent]
    safety_settings: list | None
    generation_config: AistudioGenerationConfig
    snapshot: str | None
    system_instruction: AistudioContent | None
    tools: list[list] | None
    evergreen_model_uri: str | None = None
    request_flag: int | None = None
    session_token: str | None = None
    location: list | None = None
    raw_body: list = field(default_factory=list)
