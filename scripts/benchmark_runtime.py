"""Local runtime benchmarks for history aggregation and long Claude streams.

Pass --baseline-dir with round2_baseline_metrics.py and
round2_baseline_compat.py to compare snapshots from before the optimization.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.util
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from aistudio_api.application import compat_service
from aistudio_api.infrastructure import request_metrics


def load_baseline(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def rows(count: int):
    for index in range(count):
        yield {
            "model": f"models/m{index % 4}",
            "account_id": f"account{index % 8}",
            "key_id": f"key{index % 3}",
            "account_name": f"name{index % 8}",
            "started_at": 1700000000 + index,
            "elapsed_ms": (index * 3571) % 10000 / 10,
            "first_byte_ms": index % 300 / 10,
            "result": "success" if index % 5 else "error",
            "attempts": 1 + index % 3,
            "prompt_tokens": 15,
            "completion_tokens": index % 20,
            "total_tokens": 0,
        }


def metrics_run(module: ModuleType, count: int, buffered: bool):
    source = rows(count)
    return module.summarize(
        list(source) if buffered else source,
        since=1700000000,
        until=1700000000 + count,
    )


def measure(run: Callable, repeat: int) -> dict[str, float]:
    samples = []
    for _ in range(repeat):
        gc.collect()
        started = time.perf_counter()
        run()
        samples.append((time.perf_counter() - started) * 1000)
    gc.collect()
    tracemalloc.start()
    try:
        run()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return {
        "median_ms": round(statistics.median(samples), 2),
        "peak_python_mib": round(peak / 1024**2, 3),
    }


async def claude_run(module: ModuleType, size: int, fragment: int) -> None:
    async def source():
        for index in range(0, size, fragment):
            length = min(fragment, size - index)
            text = (f"{index:016d}" + "x" * fragment)[:length]
            yield {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        yield {"candidates": [{"finishReason": "STOP"}]}

    output_size = 0
    async for event in module.claude_stream_adapter(
        source(), requested_model="test", stop_sequences=None
    ):
        for line in event.split("\n"):
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                output_size += len(payload.get("delta", {}).get("text", ""))
    if output_size != size:
        raise RuntimeError("Claude output was lost or duplicated")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--text-mib", type=int, default=4)
    parser.add_argument("--fragment-bytes", type=int, default=1024)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if min(args.rows, args.text_mib, args.fragment_bytes, args.repeat) < 1:
        parser.error("sizes and repeat must be positive")
    modules = [("after", request_metrics, compat_service, False)]
    if args.baseline_dir:
        modules.insert(
            0,
            (
                "before",
                load_baseline(
                    args.baseline_dir / "round2_baseline_metrics.py", "baseline_metrics"
                ),
                load_baseline(
                    args.baseline_dir / "round2_baseline_compat.py", "baseline_compat"
                ),
                True,
            ),
        )
    results: dict[str, object] = {}
    fingerprints = set()
    for label, metrics, compat, buffered in modules:
        fingerprint = hashlib.sha256(
            json.dumps(
                metrics_run(metrics, args.rows, buffered), sort_keys=True
            ).encode()
        ).hexdigest()
        fingerprints.add(fingerprint)
        results[label] = {
            "history_aggregation": measure(
                lambda m=metrics, b=buffered: metrics_run(m, args.rows, b), args.repeat
            ),
            "claude_stream": measure(
                lambda m=compat: asyncio.run(
                    claude_run(m, args.text_mib * 1024**2, args.fragment_bytes)
                ),
                args.repeat,
            ),
            "metrics_sha256": fingerprint,
        }
    if len(fingerprints) != 1:
        raise RuntimeError("Metrics results differ from the baseline")
    print(
        json.dumps(
            {
                "rows": args.rows,
                "text_mib": args.text_mib,
                "fragment_bytes": args.fragment_bytes,
                "repeat": args.repeat,
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
