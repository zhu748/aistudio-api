"""After classification, previously sent Parts need not outlive an ASGI write."""

from __future__ import annotations

import asyncio
import gc
import json
import time
import weakref
from collections.abc import AsyncGenerator

import pytest

from aistudio_api.application.api_service_gemini import (
    GeminiStreamResponse,
    _stream_attempt_frames,
    _StreamAttemptState,
    _usage_payload,
)
from aistudio_api.domain.models import ResponsePart


class _TrackedString(str):
    """Observe lifetime without asserting machine-dependent heap sizes."""


@pytest.mark.parametrize("ending", ["STOP", "MAX_TOKENS", "interruption", "failure"])
async def test_finished_native_parts_are_released_while_slow_client_reads(ending):
    refs = {}

    def tracked(key, value):
        text = _TrackedString(value)
        refs[key] = weakref.ref(text)
        return text

    def group():
        # One source event can contain the *whole* suffix. Holding either its
        # raw list or its encoded list is enough to keep all these objects live.
        return [
            ResponsePart(
                function_call={
                    "id": "fixture",
                    "name": "lookup",
                    "args": tracked("shard", '{"q":'),
                }
            ),
            ResponsePart(
                function_call={"id": "fixture", "args": '"abc"}'},
                thought_signature="c2ln",
            ),
            ResponsePart(text=tracked("sent", "first display"), text_present=True),
            ResponsePart(text="last display", text_present=True),
        ]

    async def source():
        yield "ordered_parts", group()
        # Neither shards nor suffix may be discarded before a terminal decision.
        assert refs["shard"]() is not None and refs["sent"]() is not None
        if ending == "failure":
            raise ValueError("fixture ended early")
        if ending == "interruption":
            yield "tool_interruption", None
        yield "finish_reason", "STOP" if ending == "interruption" else ending

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        state = _StreamAttemptState()
        try:
            async for frame in _stream_attempt_frames(
                source(), state, "fixture", time.perf_counter()
            ):
                yield frame
        except ValueError as exc:
            assert ending == "failure" and str(exc) == "fixture ended early"
            yield {"error": {"message": str(exc)}}
        else:
            yield _usage_payload(None, finish_reason=state.final_finish_reason)

    response = GeminiStreamResponse(frames())
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}
    pauses = [asyncio.Event(), asyncio.Event()]
    resumes = [asyncio.Event(), asyncio.Event()]
    wire = []
    parts = []

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        body = message.get("body", b"")
        if not body.startswith(b"data: "):
            return
        event = json.loads(body[6:])
        wire.append(event)
        for candidate in event.get("candidates", []):
            for part in candidate["content"]["parts"]:
                parts.append(part)
                if len(parts) == 1 or part.get("text") == "last display":
                    index = int(part.get("text") == "last display")
                    pauses[index].set()
                    await resumes[index].wait()

    task = asyncio.create_task(response(scope, receive, send))
    try:
        for index, key in enumerate(("shard", "sent")):
            await asyncio.wait_for(pauses[index].wait(), timeout=3)
            gc.collect()
            assert refs[key]() is None
            assert not task.done()
            resumes[index].set()
    finally:
        for gate in resumes:
            gate.set()
        await asyncio.wait_for(task, timeout=3)

    expected: list[dict[str, object]] = [
        {"text": "first display"},
        {"text": "last display"},
    ]
    if ending == "STOP":
        expected.insert(
            0,
            {
                "functionCall": {
                    "id": "fixture",
                    "name": "lookup",
                    "args": {"q": "abc"},
                },
                "thoughtSignature": "c2ln",
            },
        )
    assert parts == expected
    if ending == "failure":
        assert wire[-1] == {"error": {"message": "fixture ended early"}}
    else:
        assert wire[-1]["candidates"][0]["finishReason"] == (
            "STOP" if ending == "interruption" else ending
        )
