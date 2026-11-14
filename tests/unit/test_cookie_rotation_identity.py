"""Cookie rotation matches canonical auth files rather than account ID substrings."""

from unittest.mock import AsyncMock

import pytest

from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account import cookie_parser
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.session import GatewaySession, SessionContext


@pytest.mark.parametrize("active_kind", ["target", "sibling", "prefix", "external"])
async def test_rotation_updates_only_the_matching_session(
    tmp_path, monkeypatch, active_kind
):
    store = AccountStore(accounts_dir=tmp_path / "accounts")
    for account_id, anchor in [
        ("acc", "shared"),
        ("sibling", "shared"),
        ("acc_long", "other"),
    ]:
        store.save_account(
            name=account_id,
            email=None,
            account_id=account_id,
            storage_state={
                "cookies": [
                    {"name": "SAPISID", "value": anchor},
                    {"name": "__Secure-1PSIDTS", "value": "old"},
                ]
            },
        )

    active_id = {
        "target": "acc",
        "sibling": "sibling",
        "prefix": "acc_long",
        "external": "acc",
    }[active_kind]
    active_path = store.get_auth_path(active_id)
    assert active_path is not None
    if active_kind == "external":
        # Even the exact account directory name is insufficient outside the store.
        active_path = tmp_path / "external" / "acc" / "auth.json"
    session = GatewaySession()
    session.set_ready_context(
        SessionContext(
            auth_file=str(active_path),
            cookies={
                "SAPISID": "shared"
                if active_kind in {"target", "sibling"}
                else "other",
                "__Secure-1PSIDTS": "old",
            },
        )
    )

    async def rotate(cookies, **kwargs):
        cookies["__Secure-1PSIDTS"] = "new"
        return {"ok": True, "updated": ["__Secure-1PSIDTS"]}

    monkeypatch.setattr(
        cookie_parser, "rotate_google_cookies", AsyncMock(side_effect=rotate)
    )
    try:
        result = await AccountService(store).rotate_account_cookies("acc", session)
        assert result["ok"] is True
        assert session.active_context is not None
        expected = "new" if active_kind in {"target", "sibling"} else "old"
        assert session.active_context.cookies["__Secure-1PSIDTS"] == expected
        assert (
            cookie_parser.load_cookies_from_auth_file(store.get_auth_path("acc"))[
                "__Secure-1PSIDTS"
            ]
            == "new"
        )
    finally:
        await session.close()
