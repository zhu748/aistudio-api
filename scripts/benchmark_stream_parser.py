"""Reproducible local parser benchmark; no upstream account or network needed."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import time
from pathlib import Path

from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", nargs="+", type=int, default=[1, 4, 8])
    parser.add_argument("--fragment-bytes", type=int, default=1024)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--parser-file", type=Path, help="Optional baseline parser module"
    )
    args = parser.parse_args()
    if min(*args.size_mib, args.fragment_bytes, args.repeat) < 1:
        parser.error("sizes, fragment bytes and repeat must be positive")
    parser_class = IncrementalJSONStreamParser
    if args.parser_file:
        spec = importlib.util.spec_from_file_location(
            "benchmark_baseline", args.parser_file
        )
        if spec is None or spec.loader is None:
            parser.error("Cannot load baseline module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser_class = module.IncrementalJSONStreamParser
    results = []
    for size_mib in args.size_mib:
        raw = '[[["' + "a" * (size_mib * 1024 * 1024) + '"]]]'
        samples = []
        for _ in range(args.repeat):
            instance = parser_class()
            started = time.perf_counter()
            chunks = 0
            for offset in range(0, len(raw), args.fragment_bytes):
                chunks += sum(
                    1 for _ in instance.feed(raw[offset : offset + args.fragment_bytes])
                )
            chunks += sum(1 for _ in instance.finish())
            samples.append((time.perf_counter() - started) * 1000)
            if chunks != 1:
                raise RuntimeError(f"Expected one complete chunk, got {chunks}")
        results.append(
            {"size_mib": size_mib, "median_ms": round(statistics.median(samples), 2)}
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


if __name__ == "__main__":
    main()
