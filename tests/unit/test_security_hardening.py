"""安全加固与并发修复的配套回归测试（对应审查报告 2.1/2.2/2.3/3.1/3.9/3.11）。"""

import time as _time
from typing import cast

import pytest

# ========== 2.2 ListModels XSSI 前缀剥离 ==========


@pytest.mark.asyncio
async def test_list_models_strips_xssi_prefix(monkeypatch):
    """上游响应携带 )]}' XSSI 前缀时必须剥离后再解析，不得静默失败。"""
    from aistudio_api.infrastructure.gateway import model_discovery as md

    captured = {}

    class _FakeResp:
        status_code = 200
        text = (
            ")]}'\n"
            '[[["models/gemini-3.9-flash",null,"001","Gemini 3.9 Flash","",'
            '1048576,65536,["generateContent"],1.0,0.95]]]'
        )

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, content=None):
            captured["url"] = url
            return _FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    svc = md.ModelDiscoveryService(cache_ttl_seconds=600)

    models = await svc._fetch_via_http({"SAPISID": "x", "SID": "y"}, auth_user="0")

    assert models, "XSSI 前缀未剥离导致解析结果为空（2.2 回归）"
    assert models[0]["id"] == "gemini-3.9-flash"
    methods = cast("list[str]", models[0]["supportedGenerationMethods"])
    assert "generateContent" in methods


@pytest.mark.asyncio
async def test_list_models_fetch_failure_short_ttl_fallback(monkeypatch):
    """上游拉取失败且无缓存时兜底数据只给短 TTL（60s 后可重试，L1）。"""
    from aistudio_api.infrastructure.gateway import model_discovery as md

    class _BoomClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, content=None):
            raise ConnectionError("network down")

    monkeypatch.setattr("httpx.AsyncClient", _BoomClient)
    svc = md.ModelDiscoveryService(cache_ttl_seconds=600)

    models = await svc.get_models(cookies={"SAPISID": "x"})
    assert models  # 兜底内置列表
    # 兜底数据不应占用完整 TTL：60 秒后（模拟时间前进）应再次尝试上游
    svc._last_fetched_at -= 61.0
    # 直接验证 TTL 判定：时间推进后缓存已过期
    assert (_time.time() - svc._last_fetched_at) >= svc._cache_ttl


# ========== 3.1 Netscape #HttpOnly_ 前缀 ==========


def test_parse_raw_cookies_httponly_netscape_lines():
    """cookies.txt 的 #HttpOnly_ 行是 HttpOnly Cookie 而非注释，必须解析。"""
    from aistudio_api.infrastructure.account.cookie_parser import parse_raw_cookies

    raw = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1999999999\tSID\tsecret-sid\n"
        "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1999999999\t__Secure-1PSID\tsecure-psid\n"
        ".google.com\tTRUE\t/\tFALSE\t1999999999\tSAPISID\tplain-sapisid\n"
        "# 这一行才是真正的注释\n"
    )
    parsed = parse_raw_cookies(raw)

    assert parsed.get("SID") == "secret-sid", "HttpOnly SID 被当注释丢弃（3.1 回归）"
    assert parsed.get("__Secure-1PSID") == "secure-psid"
    assert parsed.get("SAPISID") == "plain-sapisid"
    assert "这一行才是真正的注释" not in str(parsed)


# ========== 2.3 子账号探活上界 ==========


@pytest.mark.asyncio
async def test_probe_sub_accounts_stops_on_consecutive_403(monkeypatch):
    """连续 403（IP/配额级封锁）必须在 3 次后停止，绝不无限请求。"""

    from aistudio_api.infrastructure.account import cookie_parser as cp

    calls = {"n": 0}

    class _Resp403:
        status_code = 403

    class _Client:
        async def post(self, url, headers=None, content=None):
            if "ListModels" in url:
                calls["n"] += 1
            return _Resp403()

    async def _fake_emails_map(cookie_dict, auth_user="0", client=None, **kw):
        return {}

    monkeypatch.setattr(cp, "probe_account_emails_map", _fake_emails_map)

    async def _fake_client_factory(*a, **kw):
        return _Client()

    class _Ctx:
        async def __aenter__(self):
            return _Client()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: _Ctx())
    monkeypatch.setattr("aistudio_api.config.settings.proxy_url", None)

    result = await cp.probe_google_accounts_infinite(
        "SAPISID=abc; SID=def; HSID=ghi",
        max_fails_in_a_row=1000,  # 故意调高：只应依赖 403 连击停止
    )

    assert calls["n"] == 3, (
        f"连续 403 未在 3 次后停止，发了 {calls['n']} 次请求（2.3 回归）"
    )
    # 前 2 次 403 仍按「账号存在」入列（保留原语义），第 3 次判定 IP 级封锁停止
    assert len(result) == 2


@pytest.mark.asyncio
async def test_probe_sub_accounts_hard_cap(monkeypatch):
    """即使持续返回 200，探测索引也必须停在硬上限内。"""

    from aistudio_api.infrastructure.account import cookie_parser as cp

    calls = {"n": 0}

    class _Resp200:
        status_code = 200

    class _Client:
        async def post(self, url, headers=None, content=None):
            calls["n"] += 1
            if "GetAiStudioBenefitTier" in url:
                # tier 探活返回非 200，走 Free 默认
                class _R:
                    status_code = 500
                    text = ""

                return _R()
            return _Resp200()

    async def _fake_emails_map(cookie_dict, auth_user="0", client=None, **kw):
        return {}

    monkeypatch.setattr(cp, "probe_account_emails_map", _fake_emails_map)

    class _Ctx:
        async def __aenter__(self):
            return _Client()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: _Ctx())
    monkeypatch.setattr("aistudio_api.config.settings.proxy_url", None)

    result = await cp.probe_google_accounts_infinite(
        "SAPISID=abc; SID=def; HSID=ghi",
        max_fails_in_a_row=1000,
        max_sub_accounts=5,
    )

    assert calls["n"] <= 5 * 2, "探测请求数超过硬上限（2.3 回归）"
    assert len(result) <= 5


# ========== 2.1 鉴权对称化 ==========


class _StubRequest:
    """最小化 Request 桩：只提供 headers / query_params / url / client。"""

    def __init__(self, headers=None, query=None, path="/api/accounts"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._query = query or {}
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": "203.0.113.9"})()


def test_require_web_auth_blocks_anonymous_when_api_keys_configured(monkeypatch):
    """仅配置 API Key（未设管理密码）时管理端必须拒绝匿名访问（2.1 核心）。"""
    from fastapi import HTTPException

    from aistudio_api.api import dependencies as deps
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "web_password", None)
    monkeypatch.setattr(settings, "api_keys", frozenset({"sk-real-key"}))
    monkeypatch.setattr(
        deps, "_all_configured_api_keys", lambda: frozenset({"sk-real-key"})
    )

    assert deps.web_auth_required() is True

    with pytest.raises(HTTPException) as ei:
        deps.require_web_auth(_StubRequest())  # type: ignore[arg-type]
    assert ei.value.status_code == 401


def test_require_web_auth_accepts_api_key_fallback(monkeypatch):
    """无管理密码时，任意有效 API Key 可进入管理端（对称回退）。"""
    from aistudio_api.api import dependencies as deps
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "web_password", None)
    monkeypatch.setattr(settings, "api_keys", frozenset({"sk-real-key"}))
    monkeypatch.setattr(
        deps, "_all_configured_api_keys", lambda: frozenset({"sk-real-key"})
    )

    req = _StubRequest(headers={"authorization": "Bearer sk-real-key"})
    deps.require_web_auth(req)  # type: ignore[arg-type]  # 不应抛出


def test_require_web_auth_rejects_api_key_when_admin_password_set(monkeypatch):
    """设置了管理密码后，API Key 持有者不可越权进入管理端。"""
    from fastapi import HTTPException

    from aistudio_api.api import dependencies as deps
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "web_password", "admin-pass-123")
    monkeypatch.setattr(settings, "api_keys", frozenset({"sk-real-key"}))

    req = _StubRequest(headers={"authorization": "Bearer sk-real-key"})
    with pytest.raises(HTTPException) as ei:
        deps.require_web_auth(req)  # type: ignore[arg-type]
    assert ei.value.status_code == 401

    # 管理密码本体可以通过
    ok_req = _StubRequest(headers={"authorization": "Bearer admin-pass-123"})
    deps.require_web_auth(ok_req)  # type: ignore[arg-type]


def test_require_web_auth_rejects_query_param_token(monkeypatch):
    """管理端不采信 ?key= 查询参数（3.11）：凭据只接受 Header 形式。"""
    from fastapi import HTTPException

    from aistudio_api.api import dependencies as deps
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "web_password", "admin-pass-123")
    monkeypatch.setattr(settings, "api_keys", frozenset())

    class _QueryReq(_StubRequest):
        @property
        def query_params(self):
            return {"key": "admin-pass-123"}

    with pytest.raises(HTTPException) as ei:
        deps.require_web_auth(_QueryReq())  # type: ignore[arg-type]
    assert ei.value.status_code == 401


def test_management_auth_rate_limit(monkeypatch):
    """同一 IP 连续 401 超过阈值后应被限流为 429（3.11）。"""
    from fastapi import HTTPException

    from aistudio_api.api import dependencies as deps
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "web_password", "admin-pass-123")
    monkeypatch.setattr(settings, "api_keys", frozenset())

    req = _StubRequest(headers={"authorization": "Bearer wrong"})
    statuses = []
    for _ in range(12):
        try:
            deps.require_web_auth(req)  # type: ignore[arg-type]
            statuses.append(200)
        except HTTPException as e:
            statuses.append(e.status_code)

    assert 429 in statuses, "管理端 401 无速率限制（3.11 回归）"


def test_mask_api_key_shapes():
    """脱敏展示保留首尾可辨识字符，且明文不出现。"""
    from aistudio_api.api.dependencies import mask_api_key

    assert mask_api_key("sk-aistudio-abcdef123456") == "sk-ais***3456"
    assert mask_api_key("short12") == "sh***12"
    assert mask_api_key("") == ""


# ========== 3.9 记账串号 ==========


def test_record_rotator_event_uses_explicit_account_id(monkeypatch):
    """显式传入 account_id 时必须记到该账号，而非收尾时的全局活跃账号。"""
    from aistudio_api.api.state import runtime_state
    from aistudio_api.application import account_orchestrator as orch

    recorded = []

    class _Rotator:
        def record_success(self, account_id, model=None, usage=None):
            recorded.append(("success", account_id))

        def record_error(self, account_id, model=None):
            recorded.append(("error", account_id))

    class _AccMeta:
        id = "acc_serving"

    class _AccService:
        def get_account(self, account_id):
            return _AccMeta() if account_id == "acc_serving" else None

        def get_active_account(self):
            return type("_Other", (), {"id": "acc_switched"})()

    monkeypatch.setattr(runtime_state, "rotator", _Rotator())
    monkeypatch.setattr(runtime_state, "account_service", _AccService())

    orch.record_rotator_event(
        "success", model="gemini-3.7-flash", account_id="acc_serving"
    )

    assert recorded == [("success", "acc_serving")], (
        "记账落到了切换后的账号上（3.9 回归）"
    )


# ========== 3.13 原子写 fsync / 权限 ==========


def test_atomic_write_private_mode_permissions(tmp_path):
    """private=True 写入的文件权限应为 0600。"""
    import stat

    from aistudio_api.infrastructure.utils.common import atomic_write_json

    target = tmp_path / "auth.json"
    atomic_write_json(target, {"cookies": []}, private=True)

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"凭据文件权限为 {oct(mode)}，应为 0600（3.13 回归）"
    # 临时文件不残留
    leftovers = list(tmp_path.glob("*.tmp.*"))
    assert not leftovers, f"原子写残留临时文件: {leftovers}"


def test_cleanup_stale_tmp_files(tmp_path):
    """超过 1 小时的残留临时文件应被清扫。"""
    import os
    import time

    from aistudio_api.infrastructure.utils.common import cleanup_stale_tmp_files

    stale = tmp_path / "auth.json.tmp.12345_6789"
    stale.write_text('{"cookies": [{"name": "SID", "value": "x"}]}')
    old = time.time() - 7200
    os.utime(stale, (old, old))

    fresh = tmp_path / "meta.json.tmp.12345_6788"
    fresh.write_text("{}")

    removed = cleanup_stale_tmp_files(tmp_path)
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists(), "误删了仍在写入窗口内的新鲜临时文件"
