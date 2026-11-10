"""AISTUDIO_API_KEYS 环境变量注入 API Key 的单元测试。"""

from __future__ import annotations

from typing import ClassVar

import pytest

from aistudio_api.config import _load_api_keys

# ========== 解析器：分隔符 / 格式 / 去重 ==========


def test_load_api_keys_comma_separated(monkeypatch):
    monkeypatch.setenv("AISTUDIO_API_KEYS", "sk-aaa, sk-bbb,,sk-ccc")
    assert _load_api_keys() == frozenset({"sk-aaa", "sk-bbb", "sk-ccc"})


def test_load_api_keys_json_array(monkeypatch):
    monkeypatch.setenv("AISTUDIO_API_KEYS", '["sk-a1", "sk-a2", " sk-a3 "]')
    assert _load_api_keys() == frozenset({"sk-a1", "sk-a2", "sk-a3"})


def test_load_api_keys_newline_and_literal_escape(monkeypatch):
    monkeypatch.setenv("AISTUDIO_API_KEYS", "sk-n1\nsk-n2\\nsk-n3")
    assert _load_api_keys() == frozenset({"sk-n1", "sk-n2", "sk-n3"})


def test_load_api_keys_singular_alias_and_merge(monkeypatch):
    monkeypatch.setenv("AISTUDIO_API_KEY", "sk-single")
    monkeypatch.setenv("AISTUDIO_API_KEYS", "sk-multi")
    # 单数 + 复数同时存在 → 合并去重
    assert _load_api_keys() == frozenset({"sk-single", "sk-multi"})


def test_load_api_keys_dedupe_across_formats(monkeypatch):
    monkeypatch.setenv("AISTUDIO_API_KEYS", '["sk-dup", "sk-other"]')
    monkeypatch.setenv("AISTUDIO_API_KEY", "sk-dup")
    assert _load_api_keys() == frozenset({"sk-dup", "sk-other"})


def test_load_api_keys_empty_or_whitespace(monkeypatch):
    monkeypatch.setenv("AISTUDIO_API_KEYS", "")
    monkeypatch.setenv("AISTUDIO_API_KEY", "   ")
    assert _load_api_keys() == frozenset()


def test_load_api_keys_unset(monkeypatch):
    monkeypatch.delenv("AISTUDIO_API_KEYS", raising=False)
    monkeypatch.delenv("AISTUDIO_API_KEY", raising=False)
    assert _load_api_keys() == frozenset()


def test_load_api_keys_invalid_json_falls_back_to_split(monkeypatch):
    # JSON 解析失败 → 回退按逗号/换行切分（宽松容错）
    monkeypatch.setenv("AISTUDIO_API_KEYS", "[sk-broken, sk-still-works")
    assert _load_api_keys() == frozenset({"[sk-broken", "sk-still-works"})


# ========== 鉴权链路：环境变量 Key 保护 /v1beta API ==========


@pytest.mark.asyncio
async def test_env_api_keys_protect_routes(monkeypatch):
    """设置环境变量 Key 后，无 Key 请求 401、带 Key 请求放行。"""
    import httpx
    from fastapi import Depends, FastAPI

    from aistudio_api.api.dependencies import require_api_key
    from aistudio_api.config import settings

    monkeypatch.setenv("AISTUDIO_API_KEYS", "sk-env-secret")
    monkeypatch.setattr(settings, "api_keys", _load_api_keys())

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected():
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 无 Key → 401
        resp = await client.get("/protected")
        assert resp.status_code == 401
        # x-goog-api-key（Gemini 标准头）→ 放行
        resp = await client.get(
            "/protected", headers={"x-goog-api-key": "sk-env-secret"}
        )
        assert resp.status_code == 200
        # ?key= 查询参数（Gemini 标准写法）→ 放行
        resp = await client.get("/protected?key=sk-env-secret")
        assert resp.status_code == 200
        # 错误 Key → 401
        resp = await client.get("/protected?key=sk-wrong")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_no_env_keys_stays_open(monkeypatch):
    """未配置任何 Key（config.yaml 与环境变量均为空）→ API 免密开放。"""
    import httpx
    from fastapi import Depends, FastAPI

    from aistudio_api.api.dependencies import require_api_key
    from aistudio_api.config import settings

    monkeypatch.delenv("AISTUDIO_API_KEYS", raising=False)
    monkeypatch.delenv("AISTUDIO_API_KEY", raising=False)
    monkeypatch.setattr(settings, "api_keys", frozenset())

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected():
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/protected")
        assert resp.status_code == 200


# ========== UI 列表合并展示 ==========


@pytest.mark.asyncio
async def test_list_api_keys_merges_env_injected(monkeypatch):
    """面板 API Key 列表应合并展示环境变量注入的 Key（标注来源）。

    未配置管理密码时（API Key 回退鉴权场景），Key 明文应脱敏展示（2.1）。
    """
    from aistudio_api.api import routes_system
    from aistudio_api.config import settings

    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults.get_configured_api_key_items",
        lambda: [
            {"name": "面板创建", "key": "sk-from-config", "created_at": "2026-01-01"}
        ],
    )
    monkeypatch.setattr(settings, "api_keys", frozenset({"sk-env-a", "sk-env-b"}))

    class _StubRequest:
        headers: ClassVar[dict[str, str]] = {"authorization": "Bearer sk-env-a"}

    items = await routes_system.list_api_keys(_StubRequest())
    by_key = {i.key: i for i in items}

    # 无管理密码：脱敏展示，但保留首尾可辨识字符
    assert "sk-fro***nfig" in by_key
    assert by_key["sk-fro***nfig"].name == "面板创建"
    assert "sk***-a" in by_key and "sk***-b" in by_key
    assert "环境变量注入" in by_key["sk***-a"].name
    assert by_key["sk***-a"].created_at is None
    # 明文不应出现在返回中
    assert all(i.key != "sk-from-config" and i.key != "sk-env-a" for i in items)
