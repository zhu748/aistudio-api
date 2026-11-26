"""An opaque-only Part belongs to one generation, even after a transport error."""

import pytest
import test_ordered_tool_routes as wire_routes

wire_gateway = wire_routes.wire_source
_MARK = "Y2Fycmllcg=="


@pytest.mark.parametrize("ending", ["close", "eof", "normal", "before_part"])
async def test_retry_respects_native_part_ownership(wire_gateway, monkeypatch, ending):
    attempts = 0

    async def answer(**_kwargs):
        nonlocal attempts
        attempts += 1
        yield "status", 200
        if attempts == 1:
            if ending != "before_part":
                chunks = [wire_routes.chunk([wire_routes.text(None, signature=_MARK)])]
                if ending == "normal":
                    chunks.append(
                        wire_routes.chunk([wire_routes.text("same attempt")], 1)
                    )
                yield "chunk", wire_routes.wire(*chunks)
            if ending in ("close", "before_part"):
                yield "error", "fixture connection closed"
        else:
            yield (
                "chunk",
                wire_routes.wire(
                    wire_routes.chunk([wire_routes.text("new attempt")], 1)
                ),
            )

    monkeypatch.setattr(wire_gateway, "send_streaming_request", answer)
    events = await wire_routes.request("native", True)
    parts = wire_routes.native_parts(events)
    if ending == "before_part":
        assert attempts == 2 and parts == [{"text": "new attempt"}]
    elif ending == "normal":
        assert attempts == 1
        assert parts == [{"thoughtSignature": _MARK}, {"text": "same attempt"}]
    else:
        assert attempts == 1 and parts == [{"thoughtSignature": _MARK}]
        assert events[-1]["error"]["code"] == 502
        assert not any(
            candidate.get("finishReason") == "STOP"
            for event in events
            for candidate in event.get("candidates", [])
        )
