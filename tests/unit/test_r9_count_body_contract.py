"""The local Claude count route keeps its byte boundary and its error dialect."""

from unittest.mock import Mock

import httpx
import pytest

from aistudio_api.api import request_middleware
from aistudio_api.api.app import app

PATH = "/v1/messages/count_tokens"
LIMIT = 1_048_576
JSON_HEADERS = {"Content-Type": "application/json"}


def count_body(size):
    prefix = b'{"model":"local","messages":[{"role":"user","content":"'
    suffix = b'"}]}'
    sentence = b"The ferry leaves the pier at noon. "
    n = size - len(prefix) - len(suffix)
    return prefix + (sentence * (n // len(sentence) + 1))[:n] + suffix


async def chunks(body):
    for offset in range(0, len(body), 65_536):
        yield body[offset : offset + 65_536]


@pytest.fixture
async def client(monkeypatch):
    # Use the shipping ASGI stack, including the request and HTTP middlewares;
    # a bare compat router does not execute the receive-side limit.
    register = Mock(side_effect=AssertionError("unexpected model admission"))
    history = Mock(side_effect=AssertionError("unexpected model history"))
    monkeypatch.setattr(request_middleware, "register_request", register)
    monkeypatch.setattr(request_middleware, "request_history", history)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    register.assert_not_called()
    history.assert_not_called()


@pytest.mark.parametrize("streamed", [False, True], ids=["content-length", "streamed"])
@pytest.mark.parametrize("size", [LIMIT, LIMIT + 1], ids=["at-limit", "one-byte-over"])
async def test_count_body_boundary(client, size, streamed):
    body = count_body(size)
    assert len(body) == size
    response = await client.post(
        PATH, headers=JSON_HEADERS, content=chunks(body) if streamed else body
    )
    assert response.request.headers.get("content-length") == (
        None if streamed else str(size)
    )
    assert response.headers["x-request-id"].startswith("req_")
    if size == LIMIT:
        assert response.status_code == 200
        assert response.json()["input_tokens"] > 0
    else:
        assert response.status_code == 413, response.text
        assert response.json() == {
            "type": "error",
            "error": {
                "type": "request_too_large",
                "message": "Request body exceeds configured limit",
            },
        }


async def test_unrelated_errors_keep_their_response(client):
    invalid_json = await client.post(
        PATH, headers=JSON_HEADERS, content=b'{"messages":'
    )
    assert invalid_json.status_code == 400
    assert invalid_json.json()["error"]["type"] == "invalid_request_error"

    # A real body-read error with no limit rejection still belongs to FastAPI.
    invalid_encoding = await client.post(PATH, headers=JSON_HEADERS, content=b"\xff")
    assert invalid_encoding.status_code == 400
    assert invalid_encoding.json() == {"detail": "There was an error parsing the body"}

    missing = await client.get("/no-such-route-r9")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
