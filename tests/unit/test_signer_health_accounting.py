"""Signer health maintains exact rolling counts without rescanning live events."""

import random

import pytest

from aistudio_api.infrastructure.waa import health as module


def test_health_counts_match_sliding_window_reference(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    tracker = module.SignerHealthTracker(
        min_rejects=5, min_accounts=3, window_seconds=10, cooldown_seconds=3
    )
    randomizer = random.Random(731)
    events = []
    degraded_until = 0
    for _ in range(1000):
        now[0] += randomizer.random()
        if randomizer.random() < 0.05:
            tracker.record_proof_accepted()
            events.clear()
            degraded_until = 0
        else:
            account = randomizer.choice([None, "a", "b", "c"])
            events = [(stamp, key) for stamp, key in events if now[0] - stamp <= 10]
            events.append((now[0], account))
            if (
                now[0] >= degraded_until
                and len(events) >= 5
                and len({key for _, key in events if key}) >= 3
            ):
                degraded_until = now[0] + 3
            tracker.record_proof_rejected(account)
        summary = tracker.state_summary()
        assert summary["recentRejects"] == len(events)
        assert summary["recentAccounts"] == len({key for _, key in events if key})
        assert summary["degraded"] == (now[0] < degraded_until)


def test_health_summary_releases_expired_event_storage(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    tracker = module.SignerHealthTracker(window_seconds=10)
    tracker.record_proof_rejected("account")
    now[0] += 11
    assert tracker.state_summary()["recentRejects"] == 0
    assert not tracker._rejects


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-1"])
def test_invalid_health_timing_environment_uses_finite_defaults(monkeypatch, raw):
    monkeypatch.setenv("AISTUDIO_SIGNER_CB_WINDOW_S", raw)
    monkeypatch.setenv("AISTUDIO_SIGNER_CB_COOLDOWN_S", raw)
    monkeypatch.setattr(module, "_singleton", None)
    tracker = module.get_signer_health()
    assert tracker.window_seconds == 300
    assert tracker.cooldown_seconds == 60
