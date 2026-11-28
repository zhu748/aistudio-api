"""The usage-counter writer keeps the disk schema stable without sharing mutable counts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, fields
from typing import get_type_hints

import pytest

from aistudio_api.api.state import ModelStatsItem, RuntimeState
from aistudio_api.config import settings
from aistudio_api.infrastructure.utils.common import atomic_write_json


def test_only_finish_reasons_needs_copying_in_a_model_stats_snapshot():
    hints = get_type_hints(ModelStatsItem)
    non_scalar = {
        f.name: hints[f.name]
        for f in fields(ModelStatsItem)
        if hints[f.name] not in (int, str | None)
    }
    assert non_scalar == {"finish_reasons": dict[str, int]}


@pytest.mark.parametrize("debounced", [False, True])
async def test_sync_and_debounced_stats_are_detached_and_match_disk_schema(
    monkeypatch, tmp_path, debounced
):
    path = tmp_path / "stats.json"
    monkeypatch.setenv("AISTUDIO_STATS_FILE", str(path))
    monkeypatch.setattr(settings, "persist_stats", True)
    state = RuntimeState()
    item = ModelStatsItem(
        requests=10,
        success=7,
        errors=2,
        rate_limited=1,
        prompt_tokens=11,
        completion_tokens=12,
        reasoning_tokens=3,
        cached_tokens=4,
        total_tokens=23,
        finish_reasons={"STOP": 6, "MAX_TOKENS": 1},
        last_used="2026-01-02 03:04:05",
    )
    state.model_stats["example"] = item
    state.model_stats["default"] = ModelStatsItem()
    writes: list[Mapping[str, object]] = []
    persist = state._persist_stats

    def capture(payload: Mapping[str, object]) -> None:
        writes.append(payload)
        persist(payload)

    monkeypatch.setattr(state, "_persist_stats", capture)
    if debounced:
        state.record("example", "success", finish_reason="STOP")
        assert state._save_task is not None
        await state._save_task
    else:
        state.save_stats()
    expected = {model: asdict(stats) for model, stats in state.model_stats.items()}
    legacy_file = tmp_path / "legacy.json"
    atomic_write_json(legacy_file, expected)
    assert path.read_bytes() == legacy_file.read_bytes()
    item.finish_reasons["STOP"] += 1
    item.requests += 1
    assert writes == [expected]
    reloaded = RuntimeState().model_stats
    assert {model: asdict(stats) for model, stats in reloaded.items()} == expected


def test_explicit_save_skips_model_scan_when_persistence_is_disabled(
    monkeypatch, tmp_path
):
    path = tmp_path / "stats.json"
    monkeypatch.setenv("AISTUDIO_STATS_FILE", str(path))
    monkeypatch.setattr(settings, "persist_stats", False)

    class UnreadStats(dict[str, ModelStatsItem]):
        def items(self):
            raise AssertionError("disabled persistence should not scan model counters")

    state = RuntimeState(model_stats=UnreadStats(example=ModelStatsItem()))
    state.save_stats()
    assert not path.exists()
