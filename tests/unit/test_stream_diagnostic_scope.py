"""Internal diagnostic text belongs only to the native Gemini SSE encoder."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_ordered_tool_routes as wire_routes

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import api_service_gemini
from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError

_PREFIX = "[aistudio-api 请求失败"
_PROSE = f"{_PREFIX}] 是用户指南中解释的标签。"
_ERROR = "fixture weather station unavailable"


@pytest.mark.parametrize("protocol", wire_routes.PROTOCOLS)
@pytest.mark.parametrize("failure", ["before_text", "after_text", "none"])
async def test_diagnostic_is_not_model_prose(
    monkeypatch: pytest.MonkeyPatch, protocol: str, failure: str
) -> None:
    attempts = closed = 0

    async def source(**_kwargs):
        nonlocal attempts, closed
        attempts += 1
        try:
            if failure != "before_text":
                yield "body", _PROSE
            if failure != "none":
                raise RequestError(500, _ERROR)
            yield "finish_reason", 1
        finally:
            closed += 1

    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(settings, "request_history", False)
    monkeypatch.setitem(
        app.dependency_overrides,
        get_client,
        lambda: SimpleNamespace(stream_generate_content=source),
    )
    events = await wire_routes.request(protocol, True)
    assert attempts == closed == 1

    if protocol == "native":
        text = "".join(
            part.get("text", "") for part in wire_routes.native_parts(events)
        )
    elif protocol == "chat":
        text = "".join(
            c["delta"].get("content", "")
            for event in events
            for c in event.get("choices", [])
        )
    elif protocol == "claude":
        text = "".join(event.get("delta", {}).get("text", "") for event in events)
    else:
        text = "".join(
            event["delta"]
            for event in events
            if event["type"] == "response.output_text.delta"
        )

    if failure == "before_text":
        if protocol == "native":
            assert text.startswith(_PREFIX) and _ERROR in text
        else:
            assert not text
    else:
        assert text == _PROSE

    if failure != "none":
        terminal = events[-1]
        if protocol == "responses":
            assert terminal["type"] == "response.failed"
            assert _ERROR in terminal["response"]["error"]["message"]
        else:
            assert _ERROR in terminal["error"]["message"]
