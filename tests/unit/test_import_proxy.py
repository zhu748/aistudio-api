"""导入账号时携带出口代理的端到端测试。

历史行为：`/api/accounts/import-cookies`、`/probe-import`、`/import-bundle`
都不接收代理参数，网页端只能「先导入 Cookie，再单独给账号设置代理」。
现在三个入口都支持显式 `proxy_url`，也支持从粘贴文本里自动拆出代理。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from fastapi import HTTPException

from aistudio_api.api import routes_accounts
from aistudio_api.api.routes_accounts import (
    BundleAccountItem,
    ImportBundleRequest,
    ImportCookiesRequest,
    ProbeAndImportRequest,
    import_bundle,
    import_cookies,
    probe_and_import,
)
from aistudio_api.api.state import RuntimeState
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountStore

_COOKIE = "SID=sid_value; SAPISID=sapisid_value; HSID=hsid_value"


class _StubRuntimeState:
    rotator = None
    client = None


_STUB_RUNTIME = cast(RuntimeState, _StubRuntimeState())


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AccountService, Path]:
    async def _fake_tier(*_args: object, **_kwargs: object) -> str:
        return "Pro"

    async def _fake_email(*_args: object, **_kwargs: object) -> str:
        return "proxy@example.com"

    monkeypatch.setattr(routes_accounts, "probe_account_benefit_tier", _fake_tier)
    monkeypatch.setattr(routes_accounts, "probe_account_email", _fake_email)
    accounts_dir = tmp_path / "accounts"
    service = AccountService(AccountStore(accounts_dir=accounts_dir))
    return service, accounts_dir


def _read_meta(accounts_dir: Path, account_id: str) -> dict[str, object]:
    meta_path = accounts_dir / account_id / "meta.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_import_cookies_accepts_explicit_proxy(env) -> None:
    service, accounts_dir = env
    res = await import_cookies(
        ImportCookiesRequest(
            cookies=_COOKIE,
            proxy_url="socks5://user:pass@127.0.0.1:1080",
        ),
        account_service=service,
        runtime_state=_STUB_RUNTIME,
    )
    assert res.proxy_url == "socks5://user:pass@127.0.0.1:1080"
    meta = _read_meta(accounts_dir, res.account_id)
    assert meta["proxy_url"] == "socks5://user:pass@127.0.0.1:1080"


@pytest.mark.asyncio
async def test_import_cookies_parses_proxy_from_pasted_text(env) -> None:
    service, accounts_dir = env
    res = await import_cookies(
        ImportCookiesRequest(cookies=f"{_COOKIE}\n1.2.3.4:1080:user:pass"),
        account_service=service,
        runtime_state=_STUB_RUNTIME,
    )
    assert res.proxy_url == "http://user:pass@1.2.3.4:1080"
    meta = _read_meta(accounts_dir, res.account_id)
    assert meta["proxy_url"] == "http://user:pass@1.2.3.4:1080"


@pytest.mark.asyncio
async def test_import_cookies_rejects_bad_explicit_proxy(env) -> None:
    service, _accounts_dir = env
    with pytest.raises(HTTPException) as exc_info:
        await import_cookies(
            ImportCookiesRequest(cookies=_COOKIE, proxy_url="not a proxy"),
            account_service=service,
            runtime_state=_STUB_RUNTIME,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "代理地址格式无法识别"


@pytest.mark.asyncio
async def test_probe_import_applies_proxy_to_all_accounts(env, monkeypatch) -> None:
    service, accounts_dir = env
    from aistudio_api.infrastructure.account import cookie_parser

    async def _fake_probe(_raw: object, **_kwargs: object):
        return [
            {"auth_user": "0", "email": "a@example.com", "tier": "Pro"},
            {"auth_user": "1", "email": "b@example.com", "tier": "Free"},
        ]

    monkeypatch.setattr(cookie_parser, "probe_google_accounts_infinite", _fake_probe)

    res = await probe_and_import(
        ProbeAndImportRequest(cookies=f"127.0.0.1:7890+{_COOKIE}"),
        account_service=service,
        runtime_state=_STUB_RUNTIME,
    )
    assert res.imported_count == 2
    for account in res.accounts:
        assert account.proxy_url == "http://127.0.0.1:7890"
        assert _read_meta(accounts_dir, account.id)["proxy_url"] == (
            "http://127.0.0.1:7890"
        )


@pytest.mark.asyncio
async def test_import_bundle_supports_per_item_proxy(env) -> None:
    service, _accounts_dir = env
    res = await import_bundle(
        ImportBundleRequest(
            accounts=[
                BundleAccountItem(
                    name="With Proxy",
                    email="with@example.com",
                    cookies=_COOKIE,
                    auth_user="0",
                    tier="Pro",
                    proxy_url="socks5://127.0.0.1:1080",
                ),
                BundleAccountItem(
                    name="Inline Proxy",
                    email="inline@example.com",
                    cookies=f"1.2.3.4:8080:user:pass|{_COOKIE}",
                    auth_user="0",
                    tier="Free",
                ),
            ]
        ),
        account_service=service,
    )
    assert res.imported_count == 2
    assert res.accounts[0].proxy_url == "socks5://127.0.0.1:1080"
    assert res.accounts[1].proxy_url == "http://user:pass@1.2.3.4:8080"
