"""An in-flight 403 must not rotate an unrelated account after its owner is deleted."""

from unittest.mock import AsyncMock, MagicMock

from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service_gemini import Auth403RetryManager
from aistudio_api.domain.errors import AuthError
from aistudio_api.infrastructure.waa import health


async def test_403_resign_does_not_rotate_new_active_account(monkeypatch):
    service = MagicMock()
    service.get_account.return_value = None
    service.get_active_account.return_value.id = "unrelated"
    service.rotate_account_cookies = AsyncMock()
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(health, "_singleton", health.SignerHealthTracker())
    manager = Auth403RetryManager()
    manager.count = 2
    manager.serving_account_id = "deleted"
    assert await manager.handle_403(
        AuthError("denied"), model="test", client=MagicMock()
    )
    service.rotate_account_cookies.assert_not_awaited()
    service.get_active_account.assert_not_called()
