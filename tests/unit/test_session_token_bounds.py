"""Optional upstream telemetry tokens have strict validation and bounded retention."""

import pytest

from aistudio_api.infrastructure.gateway import session_token as module

TOKEN = "v1_" + "a" * 24


@pytest.fixture(autouse=True)
def isolated_cache():
    module.clear_session_tokens()
    yield
    module.clear_session_tokens()


@pytest.mark.parametrize("token", [TOKEN + "\n", "v1_" + "a" * 5000])
def test_noncanonical_or_oversized_token_is_rejected(token):
    assert module.record_session_token("account", token) is False
    assert module.get_session_token("account") is None


def test_capacity_eviction_keeps_recently_read_accounts(monkeypatch):
    monkeypatch.setattr(module, "_MAX_SESSION_TOKEN_ENTRIES", 3, raising=False)
    for key in ("a", "b", "c"):
        assert module.record_session_token(key, TOKEN)
    assert module.get_session_token("a") == TOKEN
    assert module.record_session_token("d", TOKEN)
    assert module.get_session_token("b") is None
    assert module.get_session_token("a") == TOKEN
    assert module.clear_session_tokens() == 3


def test_unchanged_token_refreshes_account_recency(monkeypatch):
    monkeypatch.setattr(module, "_MAX_SESSION_TOKEN_ENTRIES", 2, raising=False)
    module.record_session_token("a", TOKEN)
    module.record_session_token("b", TOKEN)
    assert module.record_session_token("a", TOKEN) is False
    module.record_session_token("c", TOKEN)
    assert module.get_session_token("a") == TOKEN
    assert module.get_session_token("b") is None
