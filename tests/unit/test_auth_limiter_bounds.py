"""Authentication throttling retains only the history needed for its decision."""

from collections import OrderedDict

import pytest

from aistudio_api.api import dependencies as module


@pytest.fixture(autouse=True)
def isolated_failures(monkeypatch):
    monkeypatch.setattr(module, "_auth_fail_counts", OrderedDict())


def test_continuous_failures_retain_only_threshold_history(monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    results = [module._register_auth_failure_and_check("client") for _ in range(2000)]
    assert results[:10] == [False] * 10
    assert all(results[10:])
    assert len(module._auth_fail_counts["client"]) <= 11


def test_blocked_client_recovers_after_sliding_window(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    for _ in range(100):
        module._register_auth_failure_and_check("client")
    now[0] = 159.0
    assert module._register_auth_failure_and_check("client") is True
    now[0] = 160.0
    assert module._register_auth_failure_and_check("client") is False
    assert len(module._auth_fail_counts["client"]) == 2


def test_many_live_clients_cannot_exceed_entry_budget(monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    for i in range(5000):
        module._register_auth_failure_and_check(f"client-{i}")
    assert len(module._auth_fail_counts) <= 4096
    assert "client-4999" in module._auth_fail_counts


def test_recent_client_survives_capacity_eviction(monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    for i in range(4096):
        module._register_auth_failure_and_check(f"client-{i}")
    module._register_auth_failure_and_check("client-0")
    module._register_auth_failure_and_check("new-client")
    assert "client-0" in module._auth_fail_counts
    assert "client-1" not in module._auth_fail_counts


def test_expired_clients_are_removed_during_normal_traffic(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    module._register_auth_failure_and_check("old-client")
    now[0] = 160.0
    module._register_auth_failure_and_check("new-client")
    assert "old-client" not in module._auth_fail_counts
    module._reset_auth_failures("new-client")
    assert not module._auth_fail_counts
