"""HTTP request schemas."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
)


class GeminiInlineData(BaseModel):
    mimeType: str = Field(validation_alias=AliasChoices("mimeType", "mime_type"))
    data: str
    # 2026-11 AI Studio 真机抓包：inlineData wire 槽位 5 携带上传文件名
    # （[mime, b64, null, null, null, filename]）。普通 inlineData 不映射
    # displayName；仅 functionResponse 的子附件复用这个上传文件名槽位。
    filename: str | None = Field(
        default=None, validation_alias=AliasChoices("filename", "file_name")
    )


class GeminiFileData(BaseModel):
    mimeType: str | None = Field(
        default=None, validation_alias=AliasChoices("mimeType", "mime_type")
    )
    fileUri: str = Field(validation_alias=AliasChoices("fileUri", "file_uri"))


class GeminiFunctionCall(BaseModel):
    name: str
    args: dict[str, object] | list[object] | object | None = Field(
        default=None, validation_alias=AliasChoices("args", "arguments")
    )
    id: str | None = None
    # Set only by a compatibility adapter. A client cannot mark an HTTP
    # request as exact or supply this provenance through either JSON schema.
    _compat_args_rewritten: bool = PrivateAttr(default=False)
    _compat_name_rewritten: bool = PrivateAttr(default=False)


def _function_result_part_filename(part: object) -> object:
    """Map FunctionResponseBlob.displayName without changing the general Blob."""
    if not isinstance(part, Mapping):
        return part
    # Match GeminiPart's alias order, even for an explicitly null inlineData.
    key = "inlineData" if "inlineData" in part else "inline_data"
    blob = part.get(key)
    if not isinstance(blob, Mapping) or "filename" in blob or "file_name" in blob:
        return part
    for alias in ("displayName", "display_name"):
        if alias in blob:
            return {**part, key: {**blob, "filename": blob[alias]}}
    return part


@dataclass(slots=True)
class _ChatOwnerHint:
    # Request-local evidence, not payload. Pydantic compares private attributes
    # too, so exclude hints from equality as well as the public JSON schema.
    legacy_name: str | None = field(default=None, compare=False)


@dataclass(slots=True)
class _ResultOwnerHint(_ChatOwnerHint):
    exact_id_only: bool = field(default=False, compare=False)


class GeminiFunctionResponse(BaseModel):
    name: str = ""
    response: dict[str, object] | list[object] | object | None = Field(
        default=None,
        validation_alias=AliasChoices("response", "content", "output"),
    )
    id: str | None = None
    parts: (
        list[Annotated[GeminiPart, BeforeValidator(_function_result_part_filename)]]
        | None
    ) = None
    _compat_owner: _ResultOwnerHint = PrivateAttr(default_factory=_ResultOwnerHint)


class GeminiExecutableCode(BaseModel):
    code: str
    language: str = Field(default="PYTHON")
    id: str | None = None


class GeminiCodeExecutionResult(BaseModel):
    outcome: str
    output: str
    id: str | None = None


class GeminiPart(BaseModel):
    text: str | None = None
    inlineData: GeminiInlineData | None = Field(
        default=None, validation_alias=AliasChoices("inlineData", "inline_data")
    )
    fileData: GeminiFileData | None = Field(
        default=None, validation_alias=AliasChoices("fileData", "file_data")
    )
    thought: bool | None = None
    thoughtSignature: str | None = Field(
        default=None,
        validation_alias=AliasChoices("thoughtSignature", "thought_signature"),
    )
    functionCall: GeminiFunctionCall | None = Field(
        default=None, validation_alias=AliasChoices("functionCall", "function_call")
    )
    functionResponse: GeminiFunctionResponse | None = Field(
        default=None,
        validation_alias=AliasChoices("functionResponse", "function_response"),
    )
    executableCode: GeminiExecutableCode | None = Field(
        default=None,
        validation_alias=AliasChoices("executableCode", "executable_code"),
    )
    codeExecutionResult: GeminiCodeExecutionResult | None = Field(
        default=None,
        validation_alias=AliasChoices("codeExecutionResult", "code_execution_result"),
    )


GeminiFunctionResponse.model_rebuild()


class GeminiContent(BaseModel):
    role: str | None = None
    parts: list[GeminiPart]
    _compat_owner: _ChatOwnerHint = PrivateAttr(default_factory=_ChatOwnerHint)


class GeminiTool(BaseModel):
    codeExecution: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("codeExecution", "code_execution")
    )
    googleSearch: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("googleSearch", "google_search")
    )
    googleSearchRetrieval: dict[str, object] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "googleSearchRetrieval", "google_search_retrieval"
        ),
    )
    googleMaps: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("googleMaps", "google_maps")
    )
    urlContext: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("urlContext", "url_context")
    )
    functionDeclarations: list[dict[str, object]] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "functionDeclarations",
            "function_declarations",
            "functionDeclaration",
            "function_declaration",
        ),
    )

    @field_validator("functionDeclarations", mode="before")
    @classmethod
    def _coerce_function_declarations(cls, v: object) -> object:
        if isinstance(v, dict):
            return [v]
        return v


class GeminiGenerationConfig(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    stopSequences: list[str] | None = Field(
        default=None, validation_alias=AliasChoices("stopSequences", "stop_sequences")
    )
    temperature: float | None = None
    topP: float | None = Field(
        default=None, validation_alias=AliasChoices("topP", "top_p")
    )
    topK: int | None = Field(
        default=None, validation_alias=AliasChoices("topK", "top_k")
    )
    maxOutputTokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("maxOutputTokens", "max_output_tokens"),
    )
    responseModalities: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("responseModalities", "response_modalities"),
    )
    responseMimeType: str | None = Field(
        default=None,
        validation_alias=AliasChoices("responseMimeType", "response_mime_type"),
    )
    responseSchema: list[object] | dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("responseSchema", "response_schema")
    )
    presencePenalty: float | None = Field(
        default=None,
        validation_alias=AliasChoices("presencePenalty", "presence_penalty"),
    )
    frequencyPenalty: float | None = Field(
        default=None,
        validation_alias=AliasChoices("frequencyPenalty", "frequency_penalty"),
    )
    responseLogprobs: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("responseLogprobs", "response_logprobs"),
    )
    logprobs: int | None = None
    seed: int | None = None
    mediaResolution: list[object] | int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("mediaResolution", "media_resolution"),
    )
    thinkingConfig: list[object] | dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("thinkingConfig", "thinking_config")
    )
    reasoningEffort: str | None = Field(
        default=None,
        validation_alias=AliasChoices("reasoningEffort", "reasoning_effort"),
    )
    imageConfig: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("imageConfig", "image_config")
    )


class GeminiSafetySetting(BaseModel):
    category: str
    threshold: str


class GeminiGenerateContentRequest(BaseModel):
    contents: list[GeminiContent]
    systemInstruction: GeminiContent | None = Field(
        default=None,
        validation_alias=AliasChoices("systemInstruction", "system_instruction"),
    )
    tools: list[GeminiTool] | None = None
    generationConfig: GeminiGenerationConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("generationConfig", "generation_config"),
    )
    safetySettings: list[GeminiSafetySetting] | None = Field(
        default=None, validation_alias=AliasChoices("safetySettings", "safety_settings")
    )
    toolConfig: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("toolConfig", "tool_config")
    )
    reasoningEffort: str | None = Field(
        default=None,
        validation_alias=AliasChoices("reasoningEffort", "reasoning_effort"),
    )


class GeminiCountTokensRequest(BaseModel):
    """Gemini v1beta ``models.countTokens`` 请求体。

    官方协议允许三种形态：顶层 ``contents``、顶层 ``model``，或嵌套的
    ``generateContentRequest``（把 generateContent 请求整个塞进来）。
    上游 MakerSuiteService/CountTokens 只消费 contents（2026-11 HAR 实测），
    这里三种来源统一展开成 contents 列表。
    """

    contents: list[GeminiContent] | None = None
    model: str | None = None
    generateContentRequest: GeminiGenerateContentRequest | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "generateContentRequest", "generate_content_request"
        ),
    )
