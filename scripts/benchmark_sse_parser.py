"""Benchmark SSE fallback parsing with sharded payloads, without network access."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import time
from pathlib import Path

from aistudio_api.application.compat_service import _parse_gemini_sse_frames


async def benchmark(args: argparse.Namespace) -> None:
    parse = _parse_gemini_sse_frames
    if args.parser_file:
        spec = importlib.util.spec_from_file_location(
            "baseline_compat", args.parser_file
        )
        if spec is None or spec.loader is None:
            raise ValueError("Cannot load baseline module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parse = module._parse_gemini_sse_frames
    results = []
    for size in args.size_mib:
        raw = "data: " + json.dumps({"text": "a" * (size * 1024 * 1024)}) + "\n\n"

        async def source(payload: str):
            for offset in range(0, len(payload), args.fragment_bytes):
                yield payload[offset : offset + args.fragment_bytes]

        samples = []
        for _ in range(args.repeat):
            started = time.perf_counter()
            frames = [frame async for frame in parse(source(raw))]
            samples.append((time.perf_counter() - started) * 1000)
            if len(frames) != 1 or len(frames[0]["text"]) != size * 1024 * 1024:
                raise RuntimeError("SSE payload was lost or corrupted")
        results.append(
            {"size_mib": size, "median_ms": round(statistics.median(samples), 2)}
        )
    print(
        json.dumps(
            {
                "fragment_bytes": args.fragment_bytes,
                "repeat": args.repeat,
                "results": results,
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument("--fragment-bytes", type=int, default=1024)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--parser-file", type=Path)
    args = parser.parse_args()
    if min(*args.size_mib, args.fragment_bytes, args.repeat) < 1:
        parser.error("sizes, fragment bytes and repeat must be positive")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
