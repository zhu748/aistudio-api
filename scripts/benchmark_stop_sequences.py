"""Benchmark nearly matching long stop sequences without upstream access."""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

from aistudio_api.application.stream_utils import StopSequenceBuffer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lengths", nargs="+", type=int, default=[16384, 65536, 131072]
    )
    parser.add_argument("--frames", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--baseline-file", type=Path)
    args = parser.parse_args()
    if min(*args.lengths, args.frames, args.repeat) < 1:
        parser.error("lengths, frames and repeat must be positive")
    classes = [("after", StopSequenceBuffer)]
    if args.baseline_file:
        spec = importlib.util.spec_from_file_location(
            "baseline_stops", args.baseline_file
        )
        if spec is None or spec.loader is None:
            parser.error("Cannot load baseline module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        classes.insert(0, ("before", module.StopSequenceBuffer))
    results = []
    for length in args.lengths:
        stop, text = "a" * length + "b", "a" * length + "c"
        row: dict[str, object] = {"stop_length": len(stop)}
        for label, buffer_class in classes:
            samples = []
            for _ in range(args.repeat):
                started = time.perf_counter()
                buffer = buffer_class([stop])
                for _ in range(args.frames):
                    if buffer.feed(text) != text:
                        raise RuntimeError("Stop matcher lost or reordered text")
                samples.append((time.perf_counter() - started) * 1000)
            row[label + "_median_ms"] = round(statistics.median(samples), 2)
        results.append(row)
    print(
        json.dumps(
            {"frames": args.frames, "repeat": args.repeat, "results": results}, indent=2
        )
    )


if __name__ == "__main__":
    main()
