"""A bound request cannot borrow credentials from another account or pool jar."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway import transport as module


def test_missing_bound_credentials_do_not_fall_back_to_active_account(monkeypatch):
    monkeypatch.setattr(
        module,
        "load_cookies_from_auth_file",
        lambda path: {"SAPISID": "other-account"} if path == "other-auth" else None,
    )
    monkeypatch.setattr(
        AccountStore, "get_active_account", lambda self: SimpleNamespace(id="other")
    )
    monkeypatch.setattr(
        AccountStore, "get_auth_path_optional", lambda *args, **kwargs: "other-auth"
    )
    assert module._get_auth_cookies("missing-bound-auth") is None


def test_explicit_empty_cookie_snapshot_does_not_reload_global_state(monkeypatch):
    load = Mock(return_value={"SAPISID": "other-account"})
    monkeypatch.setattr(module, "_get_auth_cookies", load)
    headers = module._ensure_authorization_header({}, cookies={}, tier="Free")
    load.assert_not_called()
    assert "Cookie" not in headers and "Authorization" not in headers


@pytest.mark.parametrize("streaming", [False, True])
async def test_rpc_without_cookies_does_not_inherit_another_account_from_jar(streaming):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    transport = module.HTTPStreamTransport()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.cookies.set("SAPISID", "other-account", domain="example.com")
    transport._clients[transport.proxy_url or ""] = client
    try:
        if streaming:
            result = [
                event
                async for event in transport.send_streaming_request(
                    url="https://example.com/rpc", headers={}, body="{}", cookies={}
                )
            ]
            assert result[0] == ("status", 200)
        else:
            assert await transport.send_request(
                url="https://example.com/rpc", headers={}, body="{}", cookies={}
            ) == (200, b"ok")
    finally:
        await transport.close()
    assert len(seen) == 1 and "cookie" not in seen[0].headers
