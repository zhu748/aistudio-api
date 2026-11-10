"""增量流式 JSON 解析器的回归测试。

覆盖历史上容易出错的路径：
1. 单次 ``feed`` 送入大量完整分块（缓冲区压缩不能再丢数据）；
2. XSSI 前缀被拆到多次 ``feed`` 中；
3. 结构被任意切开（逐字符喂入）；
4. 流被截断时 ``finish`` 能交出最后一个修复后的分块。
"""

from __future__ import annotations

import json
from pathlib import Path

from aistudio_api.infrastructure.gateway.stream_parser import (
    XSSI_PREFIX,
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_chunk

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _chunk(text: str) -> list[object]:
    return [[None, text, None]]


def _bundle(chunks: list[list[object]]) -> str:
    return XSSI_PREFIX + json.dumps([[c] for c in chunks])


def test_many_chunks_in_single_feed_are_all_extracted() -> None:
    chunks = [_chunk(f"t{i}") for i in range(500)]
    parser = IncrementalJSONStreamParser()

    parsed = list(parser.feed(_bundle(chunks)))

    assert len(parsed) == 500
    assert parsed[0] == chunks[0]
    assert parsed[-1] == chunks[-1]


def test_many_chunks_split_across_feeds_are_all_extracted() -> None:
    chunks = [_chunk(f"t{i}") for i in range(200)]
    raw = _bundle(chunks)
    parser = IncrementalJSONStreamParser()

    parsed: list[object] = []
    for index in range(0, len(raw), 13):
        parsed.extend(parser.feed(raw[index : index + 13]))
    parsed.extend(parser.finish())

    assert len(parsed) == 200
    assert parsed[0] == chunks[0]
    assert parsed[-1] == chunks[-1]


def test_byte_by_byte_feeding_yields_same_chunks() -> None:
    chunks = [_chunk("hello"), _chunk("world")]
    raw = _bundle(chunks)
    parser = IncrementalJSONStreamParser()

    parsed: list[object] = []
    for char in raw:
        parsed.extend(parser.feed(char))

    assert parsed == chunks


def test_xssi_prefix_then_multiple_chunks_in_one_feed() -> None:
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    raw = XSSI_PREFIX + json.dumps([[c] for c in chunks])
    parser = IncrementalJSONStreamParser()

    # 分块与首帧一次性到达（真实上游常见的形态）
    parsed = list(parser.feed(raw))

    assert parsed == chunks


def test_finish_emits_repaired_trailing_fragment() -> None:
    """流被硬截断时，finish 必须交出最后一个（修复后的）分块，不能整块丢弃。"""
    raw = (
        Path(__file__).resolve().parent.parent / "fixtures" / "test_output.json"
    ).read_text(encoding="utf-8")
    truncated = raw[: raw.rindex('"') - 5]
    parser = IncrementalJSONStreamParser()

    head = list(parser.feed(truncated))
    tail = list(parser.finish())

    # 完整流共 10 个分块；截断掉最后一段后，剩余部分必须仍能全部产出
    assert len(head) == 9
    assert len(tail) == 1
    assert isinstance(tail[0], list)
    # 被截断的分块经修复后至少能读出收尾信息，不会静默丢字
    repaired = parse_response_chunk(tail[0])
    assert repaired.finish_reason is not None


def test_buffer_does_not_grow_after_full_consumption() -> None:
    """压缩逻辑正确性：完全消费后缓冲区不应保留历史数据。"""
    parser = IncrementalJSONStreamParser()
    list(parser.feed(_bundle([_chunk(f"t{i}") for i in range(100)])))

    assert len(parser.buffer) < 16
    assert parser.chunk_start is None
