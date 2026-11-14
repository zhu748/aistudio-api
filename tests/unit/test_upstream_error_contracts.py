"""HTTP success must not hide Google error envelopes or malformed empty streams."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from aistudio_api.domain.errors import (
    AuthError,
    RequestError,
    SessionExpiredError,
    TransportError,
    UsageLimitExceeded,
    classify_error,
)
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway


@pytest.mark.parametrize(
    "code,error",
    [(429, UsageLimitExceeded), (401, SessionExpiredError), (403, AuthError)],
)
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("xssi", [False, True])
def test_google_json_errors_keep_their_effective_status(code, error, wrapped, xssi):
    payload: object = {"error": {"code": code, "message": "upstream rejected"}}
    if wrapped:
        payload = [payload]
    body = (")]}'\n" if xssi else "") + json.dumps(payload)
    assert isinstance(classify_error(200, body), error)


@pytest.mark.parametrize("code,expected", [(400, 400), (503, 503), (3, 400), (14, 503)])
def test_json_error_status_supports_http_and_grpc_codes(code, expected):
    error = classify_error(
        200, json.dumps({"error": {"code": code, "message": "failed"}})
    )
    assert isinstance(error, RequestError) and error.status == expected


@pytest.mark.parametrize("body", [b" ", b"{}", b"[]", b")]}'\n[]", b"[[[]]]", b"]"])
async def test_http_200_without_content_or_finish_is_a_transport_error(body):
    closed = False

    class Session:
        async def send_streaming_request(self, **kwargs):
            nonlocal closed
            try:
                yield "status", 200
                yield "chunk", body
            finally:
                closed = True

    gateway = StreamingGateway(cast(Any, Session()))
    with pytest.raises(TransportError):
        [
            event
            async for event in gateway.stream_chat(
                request=PreparedRequest(
                    url="https://example.com", headers={}, body="{}"
                ),
                model="test",
            )
        ]
    assert closed


@pytest.mark.parametrize(
    "code,error",
    [(429, UsageLimitExceeded), (401, SessionExpiredError), (403, AuthError)],
)
@pytest.mark.parametrize("xssi", [False, True])
async def test_http_200_google_error_is_not_reported_as_normal_finish(
    code, error, xssi
):
    class Session:
        async def send_streaming_request(self, **kwargs):
            yield "status", 200
            yield (
                "chunk",
                (
                    (")]}'\n" if xssi else "")
                    + json.dumps({"error": {"code": code, "message": "rejected"}})
                ).encode(),
            )

    gateway = StreamingGateway(cast(Any, Session()))
    with pytest.raises(error):
        [
            event
            async for event in gateway.stream_chat(
                request=PreparedRequest(
                    url="https://example.com", headers={}, body="{}"
                ),
                model="test",
            )
        ]
