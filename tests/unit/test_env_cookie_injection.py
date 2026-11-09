"""环境变量 Cookie 注入与账号级独立代理的单元测试。"""

from __future__ import annotations

import json

import pytest

from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.account.cookie_parser import (
    collect_env_cookie_raw_values,
    normalize_proxy_url,
    parse_env_cookie_entries,
    split_proxy_cookie_entry,
)

# ========== 单条注入项解析：代理+Cookie / Cookie / Cookie+代理 ==========


def test_split_proxy_first():
    proxy, cookies = split_proxy_cookie_entry(
        "socks5://127.0.0.1:1080+SID=abc; HSID=def; __Secure-3PSID=x"
    )
    assert proxy == "socks5://127.0.0.1:1080"
    assert "SID=abc" in cookies


def test_split_cookie_only():
    proxy, cookies = split_proxy_cookie_entry("SID=abc; HSID=def")
    assert proxy is None
    assert "SID=abc" in cookies


def test_split_cookie_first():
    proxy, cookies = split_proxy_cookie_entry(
        "SID=abc; HSID=def+http://proxy.example.com:8080"
    )
    assert proxy == "http://proxy.example.com:8080"
    assert cookies == "SID=abc; HSID=def"


def test_split_implicit_host_port_proxy():
    proxy, cookies = split_proxy_cookie_entry("127.0.0.1:7890+SID=abc")
    assert proxy == "http://127.0.0.1:7890"
    assert cookies == "SID=abc"


def test_split_plus_inside_cookie_value_is_not_proxy():
    """Cookie 值自身含 +（如 base64）时不应误切分。"""
    proxy, cookies = split_proxy_cookie_entry("SID=abc+def+ghi; HSID=x")
    assert proxy is None
    assert cookies == "SID=abc+def+ghi; HSID=x"


def test_split_plus_in_both_proxy_and_cookie():
    """代理口令含 + 且 Cookie 值含 + 的极端场景。"""
    proxy, cookies = split_proxy_cookie_entry("http://user:p+ss@proxy:8080+SID=a+b")
    assert proxy == "http://user:p+ss@proxy:8080"
    assert cookies == "SID=a+b"


def test_split_json_cookies_with_proxy():
    raw = '[{"name":"SID","value":"json_sid"},{"name":"SAPISID","value":"json_sap"}]'
    proxy, cookies = split_proxy_cookie_entry(f"https://p.example.com:443+{raw}")
    assert proxy == "https://p.example.com:443"
    parsed = json.loads(cookies)
    assert parsed[0]["name"] == "SID"


def test_normalize_proxy_url():
    assert normalize_proxy_url("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert normalize_proxy_url("socks5://h:1") == "socks5://h:1"
    assert normalize_proxy_url("HTTPS://H:1") == "HTTPS://H:1"
    assert normalize_proxy_url("not a proxy") is None
    assert normalize_proxy_url("") is None
    assert normalize_proxy_url(None) is None


# ========== 环境变量收集与多条解析 ==========


def test_collect_env_vars_main_numbered_and_file(monkeypatch, tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("SID=file_sid; SAPISID=file_sap\n", encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_COOKIES", "SID=main_sid; SAPISID=main_sap")
    monkeypatch.setenv("AISTUDIO_COOKIES_2", "SID=num2; SAPISID=num2_sap")
    monkeypatch.setenv("AISTUDIO_COOKIES_1", "SID=num1; SAPISID=num1_sap")
    monkeypatch.setenv("AISTUDIO_COOKIES_FILE", str(cookie_file))

    values = collect_env_cookie_raw_values()
    assert values == [
        "SID=main_sid; SAPISID=main_sap",
        "SID=num1; SAPISID=num1_sap",
        "SID=num2; SAPISID=num2_sap",
        "SID=file_sid; SAPISID=file_sap",
    ]


def test_collect_env_vars_literal_newline_and_json_array(monkeypatch):
    monkeypatch.setenv("AISTUDIO_COOKIES", "SID=a; SAPISID=a1\\nSID=b; SAPISID=b2")
    values = collect_env_cookie_raw_values()
    assert values == ["SID=a; SAPISID=a1", "SID=b; SAPISID=b2"]

    monkeypatch.setenv(
        "AISTUDIO_COOKIES",
        '["SID=c; SAPISID=c3", "socks5://p:1080+SID=d; SAPISID=d4"]',
    )
    values = collect_env_cookie_raw_values()
    assert values == ["SID=c; SAPISID=c3", "socks5://p:1080+SID=d; SAPISID=d4"]


def test_parse_env_entries_skips_invalid_and_duplicates():
    entries = parse_env_cookie_entries(
        [
            "not-a-cookie-at-all",
            "SID=first; SAPISID=anchor1",
            "SID=first-rotated; SAPISID=anchor1; __Secure-1PSIDTS=new",  # 同锚点去重
            "http://p:8080+SID=second; SAPISID=anchor2",
        ]
    )
    assert len(entries) == 2
    assert entries[0].proxy_url is None
    assert entries[0].anchor == "anchor1"
    assert entries[1].proxy_url == "http://p:8080"
    assert entries[1].anchor == "anchor2"
    # 滚动 Cookie 变化不影响确定性 ID（锚点稳定）
    assert entries[0].id_hash != ""
    # storage_state 已按 parse_cookie_string 规整
    assert {"name": "SID", "value": "first"} in entries[0].storage_state["cookies"]


# ========== 注入幂等性 / 代理绑定 / 清理 ==========


def _make_store(tmp_path):
    return AccountStore(accounts_dir=tmp_path / "accounts")


def test_inject_env_accounts_creates_updates_and_prunes(monkeypatch, tmp_path):
    from aistudio_api.application.env_account_injector import inject_env_accounts

    store = _make_store(tmp_path)

    monkeypatch.setenv(
        "AISTUDIO_COOKIES",
        "socks5://10.0.0.1:1080+SID=a1; SAPISID=anchor_x\nSID=b1; SAPISID=anchor_y",
    )
    report = inject_env_accounts(store)
    assert report.total == 2
    assert len(report.created_ids) == 2
    assert not report.pruned_ids

    acc_x = next(a for a in store.list_accounts() if a.proxy_url)
    acc_y = next(a for a in store.list_accounts() if not a.proxy_url)
    assert acc_x.proxy_url == "socks5://10.0.0.1:1080"
    assert acc_y.proxy_url is None
    assert acc_x.id.startswith("env_") and acc_y.id.startswith("env_")

    # 二次注入（Cookie 刷新但锚点不变）→ 幂等更新而非重复创建
    monkeypatch.setenv(
        "AISTUDIO_COOKIES",
        "socks5://10.0.0.1:1080+SID=a2; SAPISID=anchor_x; __Secure-1PSIDTS=t2\n"
        "SID=b2; SAPISID=anchor_y",
    )
    report2 = inject_env_accounts(store)
    assert report2.total == 2
    assert not report2.created_ids
    assert len(report2.updated_ids) == 2
    assert len(store.list_accounts()) == 2

    # 移除一条 + 给另一条换代理 → 清理 + 代理覆盖
    monkeypatch.setenv(
        "AISTUDIO_COOKIES", "SID=b3; SAPISID=anchor_y+http://10.9.9.9:80"
    )
    report3 = inject_env_accounts(store)
    assert report3.total == 1
    assert len(report3.pruned_ids) == 1
    remaining = store.list_accounts()
    assert len(remaining) == 1
    assert remaining[0].proxy_url == "http://10.9.9.9:80"

    # 去掉代理段 → 显式清除，回退全局
    monkeypatch.setenv("AISTUDIO_COOKIES", "SID=b4; SAPISID=anchor_y")
    inject_env_accounts(store)
    assert store.list_accounts()[0].proxy_url is None


def test_inject_env_accounts_preserves_custom_name_and_skips_ui_accounts(
    monkeypatch, tmp_path
):
    from aistudio_api.application.env_account_injector import inject_env_accounts

    store = _make_store(tmp_path)
    # UI 导入的普通账号（acc_ 前缀）
    store.save_account(
        name="UI Account",
        email="ui@example.com",
        storage_state={"cookies": [{"name": "SID", "value": "ui_sid"}]},
    )
    ui_id = store.list_accounts()[0].id

    monkeypatch.setenv("AISTUDIO_COOKIES", "SID=e1; SAPISID=anchor_z")
    inject_env_accounts(store)
    env_acc = next(a for a in store.list_accounts() if a.id.startswith("env_"))
    assert env_acc.name == "Env Account 1"

    # 用户在 UI 改名后重启，注入不应覆盖自定义名
    store.update_account(env_acc.id, "我的主力号")
    inject_env_accounts(store)
    env_acc = store.get_account(env_acc.id)
    assert env_acc.name == "我的主力号"

    # UI 账号不受清理影响
    assert store.get_account(ui_id) is not None


def test_inject_env_accounts_no_env_vars_is_noop(monkeypatch, tmp_path):
    from aistudio_api.application.env_account_injector import inject_env_accounts

    monkeypatch.delenv("AISTUDIO_COOKIES", raising=False)
    monkeypatch.delenv("AISTUDIO_COOKIES_FILE", raising=False)
    store = _make_store(tmp_path)
    store.save_account(
        name="Keep",
        email="keep@example.com",
        storage_state={"cookies": [{"name": "SID", "value": "k"}]},
    )
    report = inject_env_accounts(store)
    assert report.total == 0
    assert len(store.list_accounts()) == 1


# ========== 账号级代理在请求链路的生效 ==========


@pytest.mark.asyncio
async def test_transport_maintains_client_pool_per_proxy():
    from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport

    transport = HTTPStreamTransport(proxy_url=None)
    transport.proxy_url = None  # 明确无全局代理

    default_client = await transport._get_client()
    same_default = await transport._get_client(None)
    proxy_client = await transport._get_client("http://10.1.1.1:8080")
    other_proxy_client = await transport._get_client("socks5://10.2.2.2:1080")
    again_proxy_client = await transport._get_client("http://10.1.1.1:8080")

    assert default_client is same_default
    assert proxy_client is again_proxy_client
    assert default_client is not proxy_client
    assert proxy_client is not other_proxy_client
    assert len(transport._clients) == 3

    await transport.close()
    assert not transport._clients


@pytest.mark.asyncio
async def test_session_context_reads_proxy_from_meta(tmp_path):
    from aistudio_api.infrastructure.gateway.session import GatewaySession

    account_dir = tmp_path / "accounts" / "env_deadbeef"
    account_dir.mkdir(parents=True)
    (account_dir / "auth.json").write_text(
        json.dumps({"cookies": [{"name": "SID", "value": "sid_val"}]}),
        encoding="utf-8",
    )
    (account_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": "env_deadbeef",
                "auth_user": "0",
                "tier": "Free",
                "proxy_url": "socks5://172.16.0.1:1080",
            }
        ),
        encoding="utf-8",
    )

    session = GatewaySession()
    ctx = await session.transition_to_account(str(account_dir / "auth.json"))
    assert ctx.proxy_url == "socks5://172.16.0.1:1080"

    # 无 proxy_url 的旧账号 → None（走全局）
    old_dir = tmp_path / "accounts" / "acc_old"
    old_dir.mkdir(parents=True)
    (old_dir / "auth.json").write_text(
        json.dumps({"cookies": [{"name": "SID", "value": "old"}]}),
        encoding="utf-8",
    )
    (old_dir / "meta.json").write_text(
        json.dumps({"id": "acc_old", "auth_user": "1", "tier": "Pro"}),
        encoding="utf-8",
    )
    ctx_old = await session.transition_to_account(str(old_dir / "auth.json"))
    assert ctx_old.proxy_url is None
    assert ctx_old.auth_user == "1"
    await session.close()


@pytest.mark.asyncio
async def test_session_send_request_uses_context_proxy():
    """会话发送请求时应将账号代理传给传输层。"""
    from aistudio_api.infrastructure.gateway.session import (
        GatewaySession,
        SessionContext,
    )

    session = GatewaySession()
    captured: dict[str, object] = {}

    class FakeTransport:
        async def send_request(self, **kwargs):
            captured.update(kwargs)
            return 200, b"[]"

        async def close(self):
            pass

    session._transport = FakeTransport()
    ctx = SessionContext(
        auth_user="0",
        auth_file="/tmp/auth.json",
        cookies={"SID": "x"},
        proxy_url="http://account-proxy:8080",
    )
    status, _body = await session.send_request(body="[]", ctx=ctx)
    assert status == 200
    assert captured["proxy_url"] == "http://account-proxy:8080"
    await session.close()


def test_account_meta_proxy_roundtrip():
    from aistudio_api.infrastructure.account.account_store import AccountMeta

    meta = AccountMeta(id="acc_1", name="A", email=None, created_at="2024-01-01")
    assert meta.proxy_url is None

    restored = AccountMeta.from_dict(
        {
            "id": "acc_1",
            "name": "A",
            "created_at": "2024-01-01",
            "proxy_url": "http://p:1",
        }
    )
    assert restored.proxy_url == "http://p:1"

    legacy = AccountMeta.from_dict({"id": "acc_2", "name": "B", "created_at": "x"})
    assert legacy.proxy_url is None


def test_update_account_proxy_persists(tmp_path):
    store = _make_store(tmp_path)
    acc = store.save_account(
        name="P",
        email="p@example.com",
        storage_state={"cookies": [{"name": "SID", "value": "p"}]},
    )
    assert store.update_account_proxy(acc.id, "10.0.0.9:7890")
    assert store.get_account(acc.id).proxy_url == "http://10.0.0.9:7890"
    # 清除
    assert store.update_account_proxy(acc.id, None)
    assert store.get_account(acc.id).proxy_url is None
    # 非法代理直接被规整为 None
    assert not store.update_account_proxy(acc.id, "not a proxy")


def test_save_account_preserves_proxy_on_ui_reimport(tmp_path):
    """UI 再次导入同名账号（未传代理）时保留已绑定代理。"""
    store = _make_store(tmp_path)
    store.save_account(
        name="A",
        email="a@example.com",
        storage_state={"cookies": [{"name": "SID", "value": "v1"}]},
        proxy_url="socks5://keep:1080",
    )
    again = store.save_account(
        name="A",
        email="a@example.com",
        storage_state={"cookies": [{"name": "SID", "value": "v2"}]},
    )
    assert again.proxy_url == "socks5://keep:1080"


@pytest.mark.asyncio
async def test_probe_env_sub_accounts_imports_and_dedupes(monkeypatch, tmp_path):

    from aistudio_api.application.env_account_injector import (
        inject_env_accounts,
        probe_env_sub_accounts,
    )

    store = _make_store(tmp_path)
    monkeypatch.setenv("AISTUDIO_COOKIES", "socks5://s:1080+SID=sub; SAPISID=anchor_s")
    report = inject_env_accounts(store)
    assert len(report.created_ids) == 1
    main_id = report.created_ids[0]

    async def fake_probe(raw, *, max_fails_in_a_row=1, proxy_url=None):
        assert proxy_url == "socks5://s:1080"
        return [
            {"auth_user": "0", "email": "main@example.com", "tier": "Pro"},
            {"auth_user": "1", "email": "sub@example.com", "tier": "Free"},
            {"auth_user": "2", "email": None, "tier": "Free"},
        ]

    monkeypatch.setattr(
        "aistudio_api.infrastructure.account.cookie_parser.probe_google_accounts_infinite",
        fake_probe,
    )

    imported = await probe_env_sub_accounts(store, [main_id])
    assert imported == 2

    accs = {a.id: a for a in store.list_accounts()}
    assert f"{main_id}_u1" in accs
    assert f"{main_id}_u2" in accs
    assert accs[f"{main_id}_u1"].email == "sub@example.com"
    assert accs[f"{main_id}_u1"].proxy_url == "socks5://s:1080"
    assert accs[f"{main_id}_u1"].cookie_id == main_id
    # u/0 回填邮箱与等级
    assert accs[main_id].email == "main@example.com"
    assert accs[main_id].tier == "Pro"

    # 重复探活不重复导入
    imported_again = await probe_env_sub_accounts(store, [main_id])
    assert imported_again == 0
    assert len(store.list_accounts()) == 3


def test_healthz_alias_still_available():
    """/healthz 别名（上一任务新增）不应受本次改动影响。"""
    from aistudio_api.api.routes_system import healthz  # noqa: F401
