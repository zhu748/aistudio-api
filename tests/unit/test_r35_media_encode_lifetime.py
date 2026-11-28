"""Downloaded transport chunks need not overlap their joined image or its base64."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from weakref import WeakValueDictionary

import httpx
import pytest

from aistudio_api.api.schemas_compat import OpenAIChatCompletionRequest
from aistudio_api.application import compat_shared
from aistudio_api.application.compat_openai import openai_to_gemini_request
from aistudio_api.application.media_session import MediaSession


class _Live:
    pass


class _Chunk(bytes):
    marker: _Live


@pytest.mark.parametrize(
    ("buffers", "declared", "expected_mime"),
    [
        (
            [b"\x89PN", b"G\r\n\x1a", b"\n\x00\xff"],
            "application/octet-stream",
            "image/png",
        ),
        ([b"\xff\xd8\xff\x00"], "image/example; label=fixture", "image/example"),
        ([], "", "image/jpeg"),
    ],
)
async def test_download_releases_owned_chunks_before_encoding(
    monkeypatch, buffers, declared, expected_mime
):
    # The reader owns only the small fixture value, never our marked chunk.
    live = WeakValueDictionary()
    closes = calls = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index, buf in enumerate(buffers):
                chunk = _Chunk(buf)
                chunk.marker = _Live()
                live[index] = chunk.marker
                yield chunk

        async def aclose(self):
            nonlocal closes
            closes += 1

    async def source(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stream(), headers={"content-type": declared})

    client = httpx.AsyncClient(transport=httpx.MockTransport(source), trust_env=False)

    async def get_client(self):
        self.client = client
        return client

    def checked_encode(raw: bytes) -> bytes:
        nonlocal calls
        calls += 1
        assert not live, "consumed transport chunks still retained at base64 encoding"
        assert raw == b"".join(buffers)
        return base64.b64encode(raw)

    monkeypatch.setattr(MediaSession, "get_client", get_client)
    monkeypatch.setattr(
        compat_shared, "base64", SimpleNamespace(b64encode=checked_encode)
    )
    req = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "fixture",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect color"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://fixture.invalid/swatch"},
                        },
                    ],
                }
            ],
        }
    )
    converted, _ = await openai_to_gemini_request(req)
    part = converted.contents[0].parts[1].inlineData
    assert part is not None and part.mimeType == expected_mime
    assert part.data == base64.b64encode(b"".join(buffers)).decode("ascii")
    assert calls == closes == 1 and client.is_closed
