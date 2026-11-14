"""Quota reset countdowns follow elapsed time across 23/25-hour Pacific days."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from aistudio_api.application import account_rotator as module


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-03-08T08:30:00", "2026-03-09T07:00:00"),
        ("2026-11-01T07:30:00", "2026-11-02T08:00:00"),
        ("2026-07-01T12:00:00", "2026-07-02T07:00:00"),
        ("2026-12-01T12:00:00", "2026-12-02T08:00:00"),
    ],
)
def test_midnight_countdown_matches_utc_elapsed_time(monkeypatch, fallback, start, end):
    now = datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()
    midnight = datetime.fromisoformat(end).replace(tzinfo=UTC).timestamp()
    monkeypatch.setattr(module.time, "time", lambda: now)
    monkeypatch.setattr(
        module,
        "_pacific_zone",
        lambda: None if fallback else ZoneInfo("America/Los_Angeles"),
    )
    assert module.get_seconds_until_pacific_midnight() == midnight - now
