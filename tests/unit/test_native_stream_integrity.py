"""Complete bad wire chunks are errors; metadata alone is not generated content."""

import json
from typing import Any, cast

import pytest

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.gateway import stream_parser as module
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway


async def collect(raw):
    class Session:
        async def send_streaming_request(self, **kwargs):
            yield "status", 200
            for fragment in raw:
                yield "chunk", fragment

    return [
        event
        async for event in StreamingGateway(cast(Any, Session())).stream_chat(
            request=PreparedRequest(url="https://example.com", headers={}, body="[]"),
            model="test",
        )
    ]


@pytest.mark.parametrize("bad", ["[[[1,broken]]]", "[[[1,]]]", "[[[}]]"])
def test_complete_invalid_chunk_is_not_repaired_or_silently_dropped(bad):
    parser = module.IncrementalJSONStreamParser()
    with pytest.raises(ValueError):
        list(parser.feed(bad))


@pytest.mark.parametrize("fragment_size", [1, 10000])
def test_native_chunk_limit_counts_utf8_bytes(monkeypatch, fragment_size):
    monkeypatch.setattr(module, "_MAX_JSON_CHUNK_BYTES", 100, raising=False)
    raw = json.dumps([[["中" * 40]]], ensure_ascii=False)
    parser = module.IncrementalJSONStreamParser()
    with pytest.raises(ValueError, match="limit"):
        for offset in range(0, len(raw), fragment_size):
            list(parser.feed(raw[offset : offset + fragment_size]))


@pytest.mark.parametrize("kind", ["response_id", "signature", "usage"])
async def test_metadata_only_stream_without_finish_is_a_transport_failure(kind):
    chunk: list[object]
    if kind == "response_id":
        chunk = [None] * 8
        chunk[7] = "v1_" + "a" * 24
    elif kind == "usage":
        chunk = [None, None, [5, 0, 5]]
    else:
        part: list[object] = [None] * 15
        part[14] = "signature"
        chunk = [[[[[part], "model"]]]]
    with pytest.raises(TransportError, match="no usable content"):
        await collect([json.dumps([[chunk]]).encode()])


@pytest.mark.parametrize("truncated", [False, True])
async def test_invalid_or_incomplete_utf8_is_an_upstream_transport_failure(truncated):
    content = [[[None, "PLACEHOLDER"]], "model"]
    chunk = [[[content, 1]]]
    raw = json.dumps([[chunk]]).encode()
    fragments = (
        [raw[: raw.index(b"PLACEHOLDER")] + b"\xe4\xb8"]
        if truncated
        else [raw.replace(b"PLACEHOLDER", b"\xff")]
    )
    with pytest.raises(TransportError):
        await collect(fragments)


async def test_bad_complete_chunk_after_content_cannot_finish_successfully():
    content = [[[None, "answer"]], "model"]
    chunk = [[[content, 1]]]
    raw = b"[[" + json.dumps(chunk).encode() + b",[1,broken]]]"
    with pytest.raises(TransportError):
        await collect([raw])


@pytest.mark.parametrize("truncated", [False, True])
async def test_new_content_cannot_reuse_a_previous_chunks_finish_reason(truncated):
    first = [[[[[[None, "first"]], "model"], 1]]]
    second = [[[[[[None, "second"]], "model"]]]]
    raw = json.dumps([[first, second]]).encode()
    if truncated:
        raw = raw[: raw.index(b"second") + 3]
    events = await collect([raw])
    assert ("finish_reason", 5) in events
    assert ("finish_reason", 1) not in events


@pytest.mark.parametrize("finish", [True, False])
async def test_boolean_finish_code_cannot_certify_a_complete_answer(finish):
    content = [[[None, "answer"]], "model"]
    chunk = [[[content, finish]]]
    events = await collect([json.dumps([[chunk]]).encode()])
    assert ("finish_reason", 5) in events
