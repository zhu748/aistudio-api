"""Stop matching keeps exact text across overlapping and Unicode fragments."""

from __future__ import annotations

import random

import pytest

from aistudio_api.application.stream_utils import StopSequenceBuffer


@pytest.mark.parametrize("seed", range(10))
def test_stop_buffer_matches_reference_for_arbitrary_fragmentation(seed):
    randomizer = random.Random(seed)
    sequences = ["ababac", "中🙂文", "aaaaab", "<END>"]
    text = (
        "".join(randomizer.choice("abx中文🙂<END>") for _ in range(2000))
        + sequences[seed % len(sequences)]
    )
    buffer = StopSequenceBuffer(sequences)
    received = ""
    emitted = ""
    offset = 0
    expected_hit = None
    expected_text = ""
    while offset < len(text):
        size = randomizer.randint(1, 31)
        chunk = text[offset : offset + size]
        offset += size
        received += chunk
        emitted += buffer.feed(chunk)
        matches = [
            (received.find(stop), stop) for stop in sequences if stop in received
        ]
        if expected_hit is None and matches:
            index, expected_hit = min(matches, key=lambda match: match[0])
            expected_text = received[:index]
    emitted += buffer.feed("", final=True)
    assert emitted == expected_text
    assert buffer.hit == expected_hit


def test_long_nearly_matching_stop_does_not_lose_text():
    stop = "a" * 65536 + "b"
    buffer = StopSequenceBuffer([stop])
    text = "a" * 65536 + "c"
    assert buffer.feed(text) == text
    assert buffer.pending == "" and buffer.hit is None
