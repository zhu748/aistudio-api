"""Base64 padding must not reject valid media at exact size/budget limits."""

from __future__ import annotations

import base64

import pytest

from aistudio_api.application import compat_service
from aistudio_api.application.media_session import current_media, media_session
from aistudio_api.config import settings


@pytest.mark.parametrize("raw", [b"a", b"ab", b"abc"])
def test_padded_media_at_exact_limit_is_accepted(monkeypatch, raw):
    monkeypatch.setattr(compat_service, "_MAX_MEDIA_BYTES", len(raw))
    data = base64.b64encode(raw).decode()
    assert compat_service._parse_data_url("data:image/png;base64," + data) == (
        "image/png",
        data,
    )


async def test_aggregate_media_budget_uses_decoded_byte_count(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 3)
    async with media_session():
        compat_service._parse_data_url("data:image/png;base64,YQ==")
        compat_service._parse_data_url("data:image/png;base64,YWI=")
        session = current_media.get()
        assert session is not None and session.total_bytes == 3


def test_mime_sniffing_preserves_large_original_payload():
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024)
    data = base64.b64encode(raw).decode()
    assert compat_service._parse_data_url("data:;base64," + data) == ("image/png", data)
