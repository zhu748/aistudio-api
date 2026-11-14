"""Non-finite generation settings are client errors before signing or sending."""

import json
from unittest.mock import MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client


@pytest.mark.parametrize("protocol", ["gemini", "chat", "claude", "responses"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_http_rejects_nonfinite_temperature_before_upstream(protocol, value):
    if protocol == "gemini":
        path = "/v1beta/models/test:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            "generationConfig": {"temperature": value},
        }
    elif protocol == "responses":
        path = "/v1/responses"
        payload = {"model": "test", "input": "hello", "temperature": value}
    else:
        path = "/v1/chat/completions" if protocol == "chat" else "/v1/messages"
        payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": value,
        }
    upstream = MagicMock()
    app.dependency_overrides[get_client] = lambda: upstream
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                path,
                content=json.dumps(payload),
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 400
        assert "error" in response.json()
        upstream.generate_content.assert_not_called()
        upstream.stream_generate_content.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_client, None)
