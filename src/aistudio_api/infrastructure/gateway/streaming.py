"""Streaming replay workflow for chat completions."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import httpx

from aistudio_api.config import settings
from aistudio_api.domain.models import parse_chunk_usage
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.request_rewriter import modify_body
from aistudio_api.infrastructure.gateway.stream_parser import IncrementalJSONStreamParser, classify_chunk
from aistudio_api.infrastructure.utils.common import compute_sapisidhash
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

logger = logging.getLogger("aistudio")


class StreamingGateway:
    async def stream_chat(
        self,
        *,
        captured: CapturedRequest | None,
        model: str,
        system_instruction: str | None,
        contents: list[AistudioContent] | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
    ) -> AsyncGenerator[tuple[str, object | None], None]:
        if not captured:
            raise ValueError("captured request is required")
        modified_body = modify_body(
            captured.body,
            model=captured.model,
            contents=contents,
            system_instruction=system_instruction,
            system_instruction_content=system_instruction_content,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            sanitize_plain_text=sanitize_plain_text,
        )

        headers = {
            key: value
            for key, value in captured.headers.items()
            if key.lower() not in ("host", "content-length")
        }
        headers["authorization"] = compute_sapisidhash(headers.get("cookie", ""))
        latest_usage: dict | None = None
        async with httpx.AsyncClient(timeout=settings.timeout_stream) as http:
            async with http.stream("POST", captured.url, content=modified_body, headers=headers) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"Upstream error: {resp.status_code}")

                parser = IncrementalJSONStreamParser()
                async for raw_chunk in resp.aiter_text():
                    for chunk in parser.feed(raw_chunk):
                        usage = parse_chunk_usage(chunk)
                        if usage:
                            latest_usage = usage
                        ctype, text = classify_chunk(chunk)
                        if ctype in ("body", "thinking", "tool_calls") and text:
                            yield (ctype, text)

        yield ("usage", latest_usage)
        yield ("done", None)
