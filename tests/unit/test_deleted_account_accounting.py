"""A deleted request account must not penalize the next active account."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import record_rotator_event


@pytest.mark.parametrize("event", ["success", "rate_limited", "auth_error", "error"])
def test_deleted_request_account_is_not_replaced_by_global_active(monkeypatch, event):
    rotator = Mock()
    service = SimpleNamespace(
        get_account=Mock(return_value=None),
        get_active_account=Mock(return_value=SimpleNamespace(id="other")),
    )
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    record_rotator_event(event, model="test", account_id="deleted")
    service.get_active_account.assert_not_called()
    assert not rotator.mock_calls
