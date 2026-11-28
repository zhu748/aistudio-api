"""Exact count parity for the run-based local Claude estimator."""

import json
from itertools import product

import anthropic
import httpx2
import pytest
from anthropic.types import MessageParam, ToolParam
from fastapi import FastAPI

from aistudio_api.api.routes_compat import _estimate_tokens, router

_RANGES = (
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
)


def _reference(text: str) -> int:
    ascii_count = sum(ord(ch) < 128 for ch in text)
    cjk_count = sum(any(lo <= ord(ch) <= hi for lo, hi in _RANGES) for ch in text)
    other_count = len(text) - ascii_count - cjk_count
    return int(ascii_count / 3.5 + cjk_count + other_count / 1.5 + 0.5)


@pytest.mark.parametrize("lo,hi", _RANGES)
def test_cjk_range_endpoints(lo, hi):
    # A single non-ASCII character rounds to 1 either way; distinguish the classes.
    for cp in (lo - 1, lo, lo + 1, hi - 1, hi, hi + 1):
        assert _estimate_tokens(chr(cp) * 21) == (21 if lo <= cp <= hi else 14)


def test_weighted_count_across_mixed_runs():
    # Include empty/ASCII, DEL, accents/combining marks, astral Han (outside the
    # historical ranges), emoji and an unpaired surrogate in direct string inputs.
    pieces = ("", "a", "\0\x7f", "港あ한", "\x80é\u0301", "\U00020000🙂", "\ud800")
    for parts in product(pieces, repeat=4):
        text = "".join(parts)
        assert _estimate_tokens(text) == _reference(text)


@pytest.mark.parametrize("note", ["Ferry times. ", "渡船时刻表。かな한글 café 🌊 "])
async def test_strict_sdk_count_preserves_media_and_exact_output(note):
    data = "QUJD" * 100

    def messages(image_data: str) -> list[MessageParam]:
        return [
            {
                "role": "user",
                "content": [
                    # The same base64 characters in ordinary text are counted literally.
                    {"type": "text", "text": note * 64 + data},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_data,
                        },
                    },
                ],
            }
        ]

    tools: list[ToolParam] = [{"name": "timetable", "input_schema": {"type": "object"}}]
    projected = {
        "system": note,
        "tools": tools,
        "messages": messages(f"<base64 media: {len(data)} chars>"),
    }
    expected = _reference(json.dumps(projected, ensure_ascii=False))

    app = FastAPI()
    app.include_router(router)
    async with anthropic.AsyncAnthropic(
        api_key="fixture",
        base_url="http://fixture.invalid",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app)),
        max_retries=0,
        _strict_response_validation=True,
    ) as sdk:
        response = await sdk.messages.with_raw_response.count_tokens(
            model="fixture", system=note, tools=tools, messages=messages(data)
        )
        assert response.status_code == 200
        assert await response.read() == f'{{"input_tokens":{expected}}}'.encode("ascii")
        assert (await response.parse()).input_tokens == expected
