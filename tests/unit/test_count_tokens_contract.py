"""CountTokens normalizes Google envelopes and rejects malformed token totals."""

import json
from unittest.mock import AsyncMock

import pytest

from aistudio_api.domain.errors import (
    AuthError,
    RequestError,
    SessionExpiredError,
    TransportError,
    UsageLimitExceeded,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import SessionContext
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


async def count(raw):
    client = AIStudioClient()
    client.session.set_ready_context(SessionContext(cookies={"SAPISID": "test"}))
    client.session.send_request = AsyncMock(return_value=(200, raw.encode()))
    try:
        return await client.count_tokens(
            model="test",
            contents=[AistudioContent(role="user", parts=[AistudioPart(text="hello")])],
        )
    finally:
        await client.close()


@pytest.mark.parametrize("prefix", ["", ")]}'\n"])
@pytest.mark.parametrize("total", [0, 12])
async def test_valid_count_in_plain_or_xssi_response(prefix, total):
    assert await count(prefix + json.dumps([total, []])) == total


@pytest.mark.parametrize("total", [True, False, -1, None, "12", {}, []])
async def test_invalid_count_is_not_a_token_total(total):
    with pytest.raises(TransportError):
        await count(json.dumps([total]))


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, SessionExpiredError),
        (403, AuthError),
        (429, UsageLimitExceeded),
        (500, RequestError),
    ],
)
async def test_google_error_inside_http_200_keeps_its_error_type(code, expected):
    with pytest.raises(expected) as failure:
        await count(
            ")]}'\n"
            + json.dumps(
                {"error": {"code": code, "message": "upstream rejected request"}}
            )
        )
    if isinstance(failure.value, RequestError):
        assert failure.value.status == 500
