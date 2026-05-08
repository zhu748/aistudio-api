"""Streaming replay workflow for chat completions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError, classify_error
from aistudio_api.domain.models import parse_chunk_usage
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.request_rewriter import modify_body
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.stream_parser import IncrementalJSONStreamParser, classify_chunk
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

logger = logging.getLogger("aistudio")


def _dump_stream_exchange(
    *,
    model: str,
    url: str,
    modified_body: str,
    status_code: int,
    raw_response: str,
) -> None:
    if not settings.dump_raw_response:
        return

    out_dir = Path(settings.dump_raw_response_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "_")
    timestamp = __import__("time").strftime("%Y%m%d_%H%M%S")
    payload = {
        "kind": "stream_generate_content",
        "model": model,
        "url": url,
        "status_code": status_code,
        "modified_body": json.loads(modified_body),
        "raw_response": raw_response,
    }
    path = out_dir / f"aistudio_stream_generate_content_{safe_model}_{timestamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("已落盘流式原始请求/响应: %s", path)


def _summarize_error_body(raw_response: str, limit: int = 500) -> str:
    text = raw_response.strip()
    if not text:
        return ""

    try:
        payload = json.loads(text)
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = " ".join(text.split())

    if len(compact) > limit:
        return compact[:limit] + "..."
    return compact


class StreamingGateway:
    def __init__(self, session: BrowserSession | None = None):
        self._session = session

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
        if self._session is None:
            raise RuntimeError("browser session is required for streaming xhr replay")

        modified_body = modify_body(
            captured.body,
            model=model,
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

        status, raw = await self._session.send_hooked_request(
            body=modified_body,
            timeout_ms=settings.timeout_stream * 1000,
        )
        parser = IncrementalJSONStreamParser()
        latest_usage: dict | None = None
        raw_response = raw.decode("utf-8", errors="replace")
        _dump_stream_exchange(
            model=model,
            url=captured.url,
            modified_body=modified_body,
            status_code=status,
            raw_response=raw_response,
        )
        if status != 200:
            detail = _summarize_error_body(raw_response)
            if status in (401, 403, 429):
                raise classify_error(status, raw_response)
            if detail:
                raise RequestError(status, detail)
            raise RequestError(status, "")

        for chunk in parser.feed(raw_response):
            usage = parse_chunk_usage(chunk)
            if usage:
                latest_usage = usage
            ctype, text = classify_chunk(chunk)
            if ctype in ("body", "thinking", "tool_calls") and text:
                yield (ctype, text)

        yield ("usage", latest_usage)
        yield ("done", None)
