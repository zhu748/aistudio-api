"""Application service layer for API handlers."""

from __future__ import annotations

import base64
import json
import logging
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aistudio_api.application.chat_service import cleanup_files, normalize_chat_request, normalize_gemini_request, normalize_openai_tools
from aistudio_api.domain.errors import AistudioError, UsageLimitExceeded
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.api.responses import (
    chat_completion_response,
    new_chat_id,
    sse_chunk,
    sse_error,
    sse_usage_chunk,
    to_gemini_parts,
    to_openai_tool_calls,
)
from aistudio_api.api.schemas import ChatRequest, GeminiGenerateContentRequest, ImageRequest
from aistudio_api.api.state import runtime_state

logger = logging.getLogger("aistudio.server")


def health_response() -> dict:
    busy_lock = runtime_state.busy_lock
    return {"status": "ok", "busy": busy_lock.locked() if busy_lock else False}


def stats_response() -> dict:
    stats = dict(runtime_state.model_stats)
    totals = {
        "requests": sum(s["requests"] for s in stats.values()),
        "success": sum(s["success"] for s in stats.values()),
        "rate_limited": sum(s["rate_limited"] for s in stats.values()),
        "errors": sum(s["errors"] for s in stats.values()),
        "prompt_tokens": sum(s["prompt_tokens"] for s in stats.values()),
        "completion_tokens": sum(s["completion_tokens"] for s in stats.values()),
        "total_tokens": sum(s["total_tokens"] for s in stats.values()),
    }
    return {"models": stats, "totals": totals}


async def handle_chat(req: ChatRequest, client: AIStudioClient):
    busy_lock = runtime_state.busy_lock
    if busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    if busy_lock.locked():
        raise HTTPException(429, detail={"message": "Server is busy", "type": "rate_limit_exceeded"})

    async with busy_lock:
        model = req.model
        system_instruction, prompt, images = normalize_chat_request(req.messages, model)
        tmp_files = list(images)

        try:
            logger.info("Chat: model=%s, prompt=%s..., images=%s, stream=%s", model, prompt[:50], len(images), req.stream)
            tools = normalize_openai_tools(req.tools)

            if req.stream:
                return _build_streaming_response(
                    client=client,
                    prompt=prompt,
                    model=model,
                    images=images if images else None,
                    system_instruction=system_instruction,
                    cleanup_paths=tmp_files,
                    include_usage=bool(req.stream_options and req.stream_options.include_usage),
                    temperature=req.temperature,
                    top_p=req.top_p,
                    top_k=req.top_k,
                    max_tokens=req.max_tokens,
                    tools=tools,
                )

            output = await client.chat(
                prompt=prompt,
                model=model,
                system_instruction=system_instruction,
                images=images if images else None,
                temperature=req.temperature,
                top_p=req.top_p,
                top_k=req.top_k,
                max_tokens=req.max_tokens,
                tools=tools,
            )

            runtime_state.record(model, "success", output.usage)
            return chat_completion_response(
                model=model,
                content=output.text,
                thinking=output.thinking,
                usage=output.usage,
                function_calls=output.function_calls,
            )
        except UsageLimitExceeded as exc:
            runtime_state.record(model, "rate_limited")
            raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
        except AistudioError as exc:
            runtime_state.record(model, "errors")
            raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
        except Exception as exc:
            runtime_state.record(model, "errors")
            logger.error("Chat error: %s", exc, exc_info=True)
            raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
        finally:
            if not req.stream:
                cleanup_files(tmp_files)


async def handle_image_generation(req: ImageRequest, client: AIStudioClient):
    busy_lock = runtime_state.busy_lock
    if busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    if busy_lock.locked():
        raise HTTPException(429, detail={"message": "Server is busy", "type": "rate_limit_exceeded"})

    async with busy_lock:
        try:
            logger.info("Image: model=%s, prompt=%s...", req.model, req.prompt[:50])
            output = await client.generate_image(prompt=req.prompt, model=req.model)

            data = []
            for img in output.images:
                b64 = base64.b64encode(img.data).decode("ascii")
                data.append({"b64_json": b64, "revised_prompt": output.text or ""})

            runtime_state.record(req.model, "success", output.usage)
            return {"created": int(time.time()), "data": data}
        except UsageLimitExceeded as exc:
            runtime_state.record(req.model, "rate_limited")
            raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
        except AistudioError as exc:
            runtime_state.record(req.model, "errors")
            raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
        except Exception as exc:
            runtime_state.record(req.model, "errors")
            logger.error("Image error: %s", exc, exc_info=True)
            raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc


def _build_streaming_response(
    *,
    client: AIStudioClient,
    prompt: str,
    model: str,
    images: list[str] | None,
    system_instruction: str | None,
    cleanup_paths: list[str],
    include_usage: bool = False,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    max_tokens: int | None = None,
    tools: list[list] | None = None,
) -> StreamingResponse:
    async def stream_response():
        try:
            chat_id = new_chat_id()
            final_usage = None
            saw_tool_calls = False
            async for event_type, text in client.stream_chat(
                prompt=prompt,
                model=model,
                images=images,
                system_instruction=system_instruction,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_tokens=max_tokens,
                tools=tools,
            ):
                if event_type == "body" and text:
                    yield sse_chunk(chat_id, model, text, include_usage=include_usage)
                elif event_type == "thinking" and text:
                    yield sse_chunk(chat_id, model, "", thinking=text, include_usage=include_usage)
                elif event_type == "tool_calls" and text:
                    saw_tool_calls = True
                    yield sse_chunk(
                        chat_id,
                        model,
                        "",
                        tool_calls=to_openai_tool_calls(text if isinstance(text, list) else []),
                        include_usage=include_usage,
                    )
                elif event_type == "usage" and include_usage:
                    final_usage = text if isinstance(text, dict) else None

            runtime_state.record(model, "success", final_usage)
            yield sse_chunk(chat_id, model, "", finish="tool_calls" if saw_tool_calls else "stop", include_usage=include_usage)
            if include_usage:
                yield sse_usage_chunk(chat_id, model, final_usage)
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Stream error: %s", exc, exc_info=True)
            runtime_state.record(model, "errors")
            message = str(exc)
            if message.startswith("Upstream error: "):
                yield sse_error(message)
            else:
                yield sse_error(message)
        finally:
            cleanup_files(cleanup_paths)

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_gemini_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    stream: bool,
):
    busy_lock = runtime_state.busy_lock
    if busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    if busy_lock.locked():
        raise HTTPException(429, detail={"message": "Server is busy", "type": "rate_limit_exceeded"})

    async with busy_lock:
        normalized = None
        try:
            normalized = normalize_gemini_request(req, model_path)
            logger.info(
                "Gemini: model=%s, contents=%s, stream=%s",
                normalized["model"],
                len(req.contents),
                stream,
            )

            if stream:
                return _build_gemini_streaming_response(client=client, normalized=normalized)

            output = await client.generate_content(
                model=normalized["model"],
                capture_prompt=normalized["capture_prompt"],
                capture_images=normalized["capture_images"],
                contents=normalized["contents"],
                system_instruction_content=normalized["system_instruction"],
                tools=normalized["tools"],
                temperature=normalized["temperature"],
                top_p=normalized["top_p"],
                top_k=normalized["top_k"],
                max_tokens=normalized["max_tokens"],
                generation_config_overrides=normalized["generation_config_overrides"],
                sanitize_plain_text=False,
            )

            runtime_state.record(normalized["model"], "success", output.usage)
            return {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": to_gemini_parts(
                                output.text,
                                function_calls=output.function_calls,
                                function_responses=output.function_responses,
                            ),
                        },
                        "finishReason": "STOP" if not output.function_calls else "FUNCTION_CALL",
                    }
                ]
            }
        except ValueError as exc:
            raise HTTPException(400, detail={"message": str(exc), "type": "bad_request"}) from exc
        except UsageLimitExceeded as exc:
            runtime_state.record(model_path, "rate_limited")
            raise HTTPException(429, detail={"message": str(exc), "type": "rate_limit_exceeded"}) from exc
        except AistudioError as exc:
            runtime_state.record(model_path, "errors")
            raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
        except Exception as exc:
            runtime_state.record(model_path, "errors")
            logger.error("Gemini error: %s", exc, exc_info=True)
            raise HTTPException(500, detail={"message": str(exc), "type": "server_error"}) from exc
        finally:
            if normalized is not None and not stream:
                cleanup_files(normalized["cleanup_paths"])


def _build_gemini_streaming_response(*, client: AIStudioClient, normalized: dict) -> StreamingResponse:
    async def stream_response():
        try:
            async for event_type, text in client.stream_generate_content(
                model=normalized["model"],
                capture_prompt=normalized["capture_prompt"],
                capture_images=normalized["capture_images"],
                contents=normalized["contents"],
                system_instruction_content=normalized["system_instruction"],
                tools=normalized["tools"],
                temperature=normalized["temperature"],
                top_p=normalized["top_p"],
                top_k=normalized["top_k"],
                max_tokens=normalized["max_tokens"],
                generation_config_overrides=normalized["generation_config_overrides"],
                sanitize_plain_text=False,
            ):
                if event_type == "body" and text:
                    yield "data: " + json.dumps(
                        {
                            "candidates": [
                                {
                                    "content": {"role": "model", "parts": [{"text": text}]},
                                    "finishReason": None,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ) + "\n\n"

            runtime_state.record(normalized["model"], "success")
            yield "data: [DONE]\n\n"
        except Exception as exc:
            logger.error("Gemini stream error: %s", exc, exc_info=True)
            runtime_state.record(normalized["model"], "errors")
            yield "data: " + json.dumps({"error": {"message": str(exc)}}, ensure_ascii=False) + "\n\n"
        finally:
            cleanup_files(normalized["cleanup_paths"])

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
