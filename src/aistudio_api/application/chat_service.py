"""Application services for chat/image orchestration."""

from __future__ import annotations

import base64
import os
import re
import uuid
from typing import Optional

import httpx

from aistudio_api.config import DEFAULT_IMAGE_MODEL
from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway.request_rewriter import TOOLS_TEMPLATES
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


SCHEMA_TYPE_CODES = {
    "string": 1,
    "number": 2,
    "integer": 3,
    "boolean": 4,
    "array": 5,
    "object": 6,
}


def data_uri_to_file(uri: str, tmp_dir: str = "/tmp") -> str:
    match = re.match(r"data:(.+?);base64,(.+)", uri, re.DOTALL)
    if not match:
        raise ValueError("Invalid data URI")
    mime, b64 = match.group(1), match.group(2)
    ext = mime.split("/")[-1].replace("jpeg", "jpg")
    path = os.path.join(tmp_dir, f"aistudio_img_{uuid.uuid4().hex[:8]}.{ext}")
    with open(path, "wb") as file:
        file.write(base64.b64decode(b64))
    return path


def url_to_file(url: str, tmp_dir: str = "/tmp") -> str:
    path = os.path.join(tmp_dir, f"aistudio_img_{uuid.uuid4().hex[:8]}.jpg")
    with httpx.Client(timeout=30) as http:
        resp = http.get(url)
        resp.raise_for_status()
        with open(path, "wb") as file:
            file.write(resp.content)
    return path


def normalize_chat_request(messages, requested_model: str, tmp_dir: str = "/tmp") -> tuple[str, str, list[str]]:
    system_instruction = None
    prompt_parts = []
    images = []

    for msg in messages:
        if msg.role == "system":
            if isinstance(msg.content, str):
                system_instruction = msg.content
        elif msg.role == "user":
            if isinstance(msg.content, str):
                prompt_parts.append(msg.content)
            elif isinstance(msg.content, list):
                for part in msg.content:
                    if part.type == "text" and part.text:
                        prompt_parts.append(part.text)
                    elif part.type == "image_url" and part.image_url:
                        url = part.image_url["url"] if isinstance(part.image_url, dict) else part.image_url.url
                        if url.startswith("data:"):
                            images.append(data_uri_to_file(url, tmp_dir=tmp_dir))
                        elif url.startswith("http"):
                            images.append(url_to_file(url, tmp_dir=tmp_dir))

    prompt = "\n".join(prompt_parts) if prompt_parts else "你好"
    model = requested_model
    if model.startswith("gpt-") or model.startswith("openai/"):
        model = DEFAULT_IMAGE_MODEL if images else requested_model
    return system_instruction, prompt, images


def cleanup_files(paths: list[str]):
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def inline_data_to_file(mime_type: str, data: str, tmp_dir: str = "/tmp") -> str:
    ext = mime_type.split("/")[-1].replace("jpeg", "jpg")
    path = os.path.join(tmp_dir, f"aistudio_img_{uuid.uuid4().hex[:8]}.{ext}")
    with open(path, "wb") as file:
        file.write(base64.b64decode(data))
    return path


def encode_schema_to_wire(schema: dict) -> list:
    schema_type = schema.get("type")
    type_code = SCHEMA_TYPE_CODES.get(schema_type, 0)
    wire = [type_code]

    if schema_type == "array" and isinstance(schema.get("items"), dict):
        while len(wire) <= 5:
            wire.append(None)
        wire[5] = encode_schema_to_wire(schema["items"])

    properties = schema.get("properties")
    if isinstance(properties, dict):
        while len(wire) <= 6:
            wire.append(None)
        wire[6] = [[name, encode_schema_to_wire(prop)] for name, prop in properties.items() if isinstance(prop, dict)]

    required = schema.get("required")
    if isinstance(required, list):
        while len(wire) <= 21:
            wire.append(None)
        wire[21] = list(required)

    property_ordering = schema.get("propertyOrdering")
    if isinstance(property_ordering, list):
        while len(wire) <= 22:
            wire.append(None)
        wire[22] = list(property_ordering)

    return wire


def encode_function_declaration_to_wire(declaration: dict) -> list:
    if not declaration.get("name"):
        raise ValueError("functionDeclarations[].name is required")

    wire = [declaration["name"]]
    if declaration.get("description") is not None:
        while len(wire) <= 1:
            wire.append(None)
        wire[1] = declaration["description"]

    parameters = declaration.get("parameters")
    if isinstance(parameters, dict):
        while len(wire) <= 2:
            wire.append(None)
        wire[2] = encode_schema_to_wire(parameters)

    return wire


def normalize_openai_tools(tools) -> list[list] | None:
    if not tools:
        return None

    function_declarations: list[dict] = []
    for tool in tools:
        if tool.type != "function":
            raise ValueError(f"unsupported tool type: {tool.type}")
        if tool.function is None:
            raise ValueError("tools[].function is required when type=function")

        function_declarations.append(
            {
                "name": tool.function.name,
                "description": tool.function.description,
                "parameters": tool.function.parameters,
            }
        )

    if not function_declarations:
        return None

    return [[None, [encode_function_declaration_to_wire(decl) for decl in function_declarations]]]


def normalize_gemini_request(req, requested_model: str, tmp_dir: str = "/tmp") -> dict:
    if not req.contents:
        raise ValueError("contents is required")

    model = requested_model if requested_model.startswith("models/") else f"models/{requested_model}"
    contents: list[AistudioContent] = []
    cleanup_paths: list[str] = []
    capture_prompt = "你好"
    capture_images: list[str] = []

    for content in req.contents:
        role = content.role or "user"
        parts: list[AistudioPart] = []
        text_parts: list[str] = []
        content_images: list[str] = []

        for part in content.parts:
            if part.text is not None:
                parts.append(AistudioPart(text=part.text))
                text_parts.append(part.text)
                continue
            if part.inlineData is not None:
                parts.append(AistudioPart(inline_data=(part.inlineData.mimeType, part.inlineData.data)))
                image_path = inline_data_to_file(part.inlineData.mimeType, part.inlineData.data, tmp_dir=tmp_dir)
                content_images.append(image_path)
                cleanup_paths.append(image_path)
                continue
            if part.fileData is not None:
                raise ValueError("fileData is not supported yet")

        contents.append(AistudioContent(role=role, parts=parts))

        if role == "user":
            if text_parts:
                capture_prompt = "\n".join(text_parts)
            if content_images:
                capture_images = content_images

    system_instruction = None
    if req.systemInstruction is not None:
        system_instruction = AistudioContent(
            role=req.systemInstruction.role or "user",
            parts=[
                AistudioPart(text=part.text)
                if part.text is not None
                else AistudioPart(inline_data=(part.inlineData.mimeType, part.inlineData.data))
                for part in req.systemInstruction.parts
                if part.text is not None or part.inlineData is not None
            ],
        )

    tools = None
    if req.tools:
        tools = []
        for tool in req.tools:
            if tool.codeExecution is not None:
                tools.append(TOOLS_TEMPLATES["code_execution"])
            if tool.functionDeclarations:
                tools.append([None, [encode_function_declaration_to_wire(decl) for decl in tool.functionDeclarations]])
            if tool.googleSearch is not None or tool.googleSearchRetrieval is not None:
                tools.append(TOOLS_TEMPLATES["google_search"])

    generation_config = req.generationConfig
    generation_config_overrides = None
    if generation_config is not None:
        generation_config_overrides = {}
        if generation_config.stopSequences is not None:
            generation_config_overrides["stop_sequences"] = generation_config.stopSequences
        if generation_config.maxOutputTokens is not None:
            generation_config_overrides["max_tokens"] = generation_config.maxOutputTokens
        if generation_config.temperature is not None:
            generation_config_overrides["temperature"] = generation_config.temperature
        if generation_config.topP is not None:
            generation_config_overrides["top_p"] = generation_config.topP
        if generation_config.topK is not None:
            generation_config_overrides["top_k"] = generation_config.topK
        if generation_config.responseMimeType is not None:
            generation_config_overrides["response_mime_type"] = generation_config.responseMimeType
        if generation_config.responseSchema is not None:
            generation_config_overrides["response_schema"] = (
                encode_schema_to_wire(generation_config.responseSchema)
                if isinstance(generation_config.responseSchema, dict)
                else generation_config.responseSchema
            )
        if generation_config.presencePenalty is not None:
            generation_config_overrides["presence_penalty"] = generation_config.presencePenalty
        if generation_config.frequencyPenalty is not None:
            generation_config_overrides["frequency_penalty"] = generation_config.frequencyPenalty
        if generation_config.responseLogprobs is not None:
            generation_config_overrides["response_logprobs"] = generation_config.responseLogprobs
        if generation_config.logprobs is not None:
            generation_config_overrides["logprobs"] = generation_config.logprobs
        if generation_config.mediaResolution is not None:
            generation_config_overrides["media_resolution"] = generation_config.mediaResolution
        if generation_config.thinkingConfig is not None:
            generation_config_overrides["thinking_config"] = generation_config.thinkingConfig

    return {
        "model": model,
        "contents": contents,
        "system_instruction": system_instruction,
        "tools": tools or None,
        "capture_prompt": capture_prompt,
        "capture_images": capture_images or None,
        "cleanup_paths": cleanup_paths,
        "temperature": generation_config.temperature if generation_config else None,
        "top_p": generation_config.topP if generation_config else None,
        "top_k": generation_config.topK if generation_config else None,
        "max_tokens": generation_config.maxOutputTokens if generation_config else None,
        "generation_config_overrides": generation_config_overrides or None,
    }
