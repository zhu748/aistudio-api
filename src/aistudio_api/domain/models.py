"""Typed domain response models."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from functools import cached_property


@dataclass
class GeneratedImage:
    mime: str
    # 上游 inlineData 的原始 base64 即主数据源：出站重新编码（SSE / 响应
    # 模型）直接复用，免去每张图一次 b64decode→b64encode 全量往返（流式
    # 图片生成场景图片会随 chunk 反复过这一对编解码）。repr=False 避免
    # 日志/异常信息里内联 MB 级 base64。
    raw_b64: str = field(repr=False)
    thought_signature: str = ""

    @cached_property
    def data(self) -> bytes:
        """惰性解码的原始字节：仅在调用方确实需要 bytes 时才付一次解码成本。"""
        return base64.b64decode(self.raw_b64)


@dataclass(slots=True)
class ResponsePart:
    """One decoded upstream Part, including the owner of any opaque signature."""

    text: str = ""
    text_present: bool = False
    inline_data: tuple[str, str] | None = None
    thought: bool = False
    function_call: dict[str, object] | None = None
    function_response: dict[str, object] | None = None
    thought_signature: str = ""
    executable_code: object | None = None
    code_execution_result: object | None = None
    image: GeneratedImage | None = None
    wire_chunk: int = 0


@dataclass
class Candidate:
    text: str = ""
    thinking: str = ""
    images: list[GeneratedImage] = field(default_factory=list)
    reasoning_images: list[GeneratedImage] = field(default_factory=list)
    function_calls: list[dict[str, object]] = field(default_factory=list)
    function_responses: list[dict[str, object]] = field(default_factory=list)
    thought_signature: str = ""
    sources: list[dict[str, object]] = field(default_factory=list)
    code_output: str = ""
    finish_reason: int | None = None
    finish_message: str = ""
    safety_ratings: list[dict[str, object]] = field(default_factory=list)
    raw_parts: list[list[object]] = field(default_factory=list, repr=False)
    ordered_parts: list[ResponsePart] | None = field(default=None, repr=False)
    tool_interrupted: bool = False

    @property
    def has_content(self) -> bool:
        return bool(
            self.text
            or self.images
            or self.code_output
            or self.function_calls
            or self.function_responses
        )


@dataclass
class ModelOutput:
    candidates: list[Candidate] = field(default_factory=list)
    model: str = ""
    raw_response: str = ""
    response_id: str = ""
    usage: dict[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.candidates[0].text if self.candidates else ""

    @property
    def thinking(self) -> str:
        return self.candidates[0].thinking if self.candidates else ""

    @property
    def images(self) -> list[GeneratedImage]:
        return self.candidates[0].images if self.candidates else []

    @property
    def reasoning_images(self) -> list[GeneratedImage]:
        return self.candidates[0].reasoning_images if self.candidates else []

    @property
    def function_calls(self) -> list[dict[str, object]]:
        return self.candidates[0].function_calls if self.candidates else []

    @property
    def function_responses(self) -> list[dict[str, object]]:
        return self.candidates[0].function_responses if self.candidates else []

    @property
    def sources(self) -> list[dict[str, object]]:
        return self.candidates[0].sources if self.candidates else []

    @property
    def code_output(self) -> str:
        return self.candidates[0].code_output if self.candidates else ""

    @property
    def has_content(self) -> bool:
        return any(c.has_content for c in self.candidates)


__all__ = [
    "Candidate",
    "GeneratedImage",
    "ModelOutput",
    "ResponsePart",
]
