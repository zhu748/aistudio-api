"""Adapters release their source even if disconnected at the initial event."""

from unittest.mock import AsyncMock

import pytest
from test_compat_combined_parts import adapter


class Source:
    def __init__(self):
        self.aclose = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise AssertionError("Source should not have been read")


@pytest.mark.parametrize(
    "protocol,count",
    [("chat", 1), ("claude", 1), ("claude", 2), ("responses", 1), ("responses", 2)],
)
async def test_close_after_initial_event_releases_source(protocol, count):
    source = Source()
    stream = adapter(protocol, source)
    for _ in range(count):
        await anext(stream)
    await stream.aclose()
    source.aclose.assert_awaited_once()
