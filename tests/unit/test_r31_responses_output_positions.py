"""Long Responses streams should close each output item by its own position."""

import base64
import json
from contextlib import aclosing
from itertools import count

import pytest

from aistudio_api.application import responses_service
from aistudio_api.application.compat_shared import decode_signature_envelope
from aistudio_api.application.responses_delivery import ResponsesStreamDelivery


@pytest.mark.parametrize("outcome", ["STOP", "MAX_TOKENS", "error", "deadline"])
async def test_long_alternating_items_keep_order_without_scanning_old_ids(
    monkeypatch, outcome
):
    size = 64
    comparisons = 0
    serial = count()

    class CountedID(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__eq__(other)

    monkeypatch.setattr(
        responses_service, "_item_id", lambda kind: CountedID(f"{kind}_{next(serial)}")
    )

    def signature(i):
        return base64.b64encode(f"fixture:{i}".encode()).decode()

    closed = 0

    async def source():
        nonlocal closed
        try:
            for i in range(size):
                part: dict[str, object] = {"text": f"片段 {i}"}
                if i % 2:
                    part.update(thought=True, thoughtSignature=signature(i))
                yield {"candidates": [{"content": {"parts": [part]}}]}
            if outcome == "error":
                yield {"error": {"code": 429, "message": "test busy"}}
            else:
                yield {"candidates": [{"finishReason": outcome}]}
        finally:
            closed += 1

    def data(frame):
        return json.loads(frame.split("\ndata: ", 1)[1])

    failures = []
    events = []
    delivery = ResponsesStreamDelivery()
    stream = responses_service.responses_stream_adapter(
        source(),
        requested_model="fixture",
        include_encrypted_reasoning=True,
        delivery=delivery,
        register_failure=failures.append,
    )
    async with aclosing(stream):
        async for wire in stream:
            delivery.accept(wire.encode())
            event = data(wire)
            events.append(event)
            if (
                outcome == "deadline"
                and event["type"] == "response.reasoning_summary_text.delta"
                and event["output_index"] == size - 1
            ):
                events.extend(data(chunk) for chunk in failures[0]("test deadline"))
                break

    assert closed == 1
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    done = [e for e in events if e["type"] == "response.output_item.done"]
    assert [e["output_index"] for e in done] == list(range(size))
    assert [e["item"] for e in done] == events[-1]["response"]["output"]
    for i, event in enumerate(done):
        item = event["item"]
        assert item["id"] == f"{'rs' if i % 2 else 'msg'}_{i}"
        assert item["status"] == (
            "completed" if i < size - 1 or outcome == "STOP" else "incomplete"
        )
        parts = item["summary" if i % 2 else "content"]
        assert [part["text"] for part in parts] == [f"片段 {i}"]
        if i % 2 and not (outcome == "deadline" and i == size - 1):
            assert item["extra_content"]["google"]["thought_signature"] == signature(i)
            assert decode_signature_envelope(item["encrypted_content"]) == signature(i)
        else:
            assert "extra_content" not in item and "encrypted_content" not in item
    # Permit O(1) owner checks per close; never revisit every earlier item ID.
    assert comparisons <= 2 * size
