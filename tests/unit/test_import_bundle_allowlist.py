"""``import_bundle`` 的 ``file_path`` 白名单测试。

历史缺陷：``file_path`` 直接 ``Path(...).resolve()`` 后 ``read_text()``，
认证用户可读取任意文件（返回内容），或用「文件不存在」与绝对路径回显做
存在性探测。现在只允许读取 ``data/`` 目录（或 ``AISTUDIO_IMPORT_ROOTS``
登记的目录）下的 ``.json`` 文件，并统一使用固定错误文案。
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from aistudio_api.api import routes_accounts
from aistudio_api.api.routes_accounts import ImportBundleRequest, import_bundle
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountStore


@pytest.fixture()
def service(tmp_path, monkeypatch) -> AccountService:
    async def _fake_tier(*_args, **_kwargs) -> str:
        return "Free"

    async def _fake_email(*_args, **_kwargs) -> str:
        return "mock@example.com"

    monkeypatch.setattr(routes_accounts, "probe_account_benefit_tier", _fake_tier)
    monkeypatch.setattr(routes_accounts, "probe_account_email", _fake_email)
    return AccountService(AccountStore(accounts_dir=tmp_path / "accounts"))


def _bundle() -> str:
    return json.dumps(
        {
            "version": 1,
            "accounts": [
                {
                    "name": "From File",
                    "email": "file@example.com",
                    "cookies": "SID=file_sid; SAPISID=file_sapisid",
                    "auth_user": "0",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_file_path_outside_allowed_roots_is_rejected(
    tmp_path, monkeypatch
) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(_bundle(), encoding="utf-8")
    # 只允许 tmp_path/data 与项目 data/，secret.json 位于其外
    monkeypatch.setenv("AISTUDIO_IMPORT_ROOTS", str(tmp_path / "data"))
    store = AccountStore(accounts_dir=tmp_path / "accounts")

    with pytest.raises(HTTPException) as exc_info:
        await import_bundle(
            ImportBundleRequest(file_path=str(secret)),
            account_service=AccountService(store),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "文件路径不在允许的导入目录内"


@pytest.mark.asyncio
async def test_non_json_suffix_is_rejected(tmp_path, monkeypatch) -> None:
    allowed_root = tmp_path / "imports"
    allowed_root.mkdir(parents=True, exist_ok=True)
    target = allowed_root / "accounts.txt"
    target.write_text(_bundle(), encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_IMPORT_ROOTS", str(allowed_root))

    with pytest.raises(HTTPException) as exc_info:
        await import_bundle(
            ImportBundleRequest(file_path=str(target)),
            account_service=AccountService(AccountStore(accounts_dir=tmp_path / "a")),
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "文件路径不在允许的导入目录内"


@pytest.mark.asyncio
async def test_missing_file_does_not_leak_path(tmp_path, monkeypatch) -> None:
    allowed_root = tmp_path / "imports"
    allowed_root.mkdir(parents=True, exist_ok=True)
    missing = allowed_root / "nope.json"
    monkeypatch.setenv("AISTUDIO_IMPORT_ROOTS", str(allowed_root))

    with pytest.raises(HTTPException) as exc_info:
        await import_bundle(
            ImportBundleRequest(file_path=str(missing)),
            account_service=AccountService(AccountStore(accounts_dir=tmp_path / "a")),
        )
    assert exc_info.value.status_code == 400
    # 不得回显绝对路径（存在性探测）
    assert str(missing) not in str(exc_info.value.detail)
    assert exc_info.value.detail == "文件不存在或不是有效 JSON 文件"


@pytest.mark.asyncio
async def test_registered_root_file_is_imported(tmp_path, monkeypatch, service) -> None:
    allowed_root = tmp_path / "imports"
    allowed_root.mkdir(parents=True, exist_ok=True)
    target = allowed_root / "accounts.json"
    target.write_text(_bundle(), encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_IMPORT_ROOTS", str(allowed_root))

    res = await import_bundle(
        ImportBundleRequest(file_path=str(target)), account_service=service
    )
    assert res.imported_count == 1
    assert res.accounts[0].name == "From File"
