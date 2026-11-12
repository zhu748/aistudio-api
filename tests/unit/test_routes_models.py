import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.config import settings


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_list_models_returns_gemini_format(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/v1beta/models")
        assert response.status_code == 200
        data = response.json()
        assert "models" in data
        assert isinstance(data["models"], list)
        assert len(data["models"]) > 0

        first = data["models"][0]
        assert first["name"].startswith("models/")
        assert "displayName" in first
        assert "supportedGenerationMethods" in first
        assert "generateContent" in first["supportedGenerationMethods"]


@pytest.mark.asyncio
async def test_get_single_model_returns_model_object(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/v1beta/models/gemini-3.7-flash")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "models/gemini-3.7-flash"
        assert "displayName" in data
        assert "supportedGenerationMethods" in data


@pytest.mark.asyncio
async def test_v1_models_endpoint_serves_openai_compat_format(monkeypatch):
    """/v1/models 现为 OpenAI 兼容层端点：返回 OpenAI 模型列表格式。"""
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/v1/models")
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        assert isinstance(data["data"], list)
        assert len(data["data"]) > 0
        first = data["data"][0]
        assert first["object"] == "model"
        assert "id" in first
        assert first["owned_by"] == "google"


@pytest.mark.asyncio
async def test_models_endpoint_accepts_query_param_key(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"my-key"}))
    async with _build_client() as client:
        # Unauthorized without key
        res_unauth = await client.get("/v1beta/models")
        assert res_unauth.status_code == 401

        # Authorized with ?key=
        res_auth = await client.get("/v1beta/models?key=my-key")
        assert res_auth.status_code == 200
        assert "models" in res_auth.json()


@pytest.mark.asyncio
async def test_get_single_model_not_found_returns_404(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/v1beta/models/non-existent-unknown-model-12345")
        assert response.status_code == 404
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == 404
        assert data["error"]["message"] == "Model not found"
        assert data["error"]["status"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_model_discovery_falls_back_to_auth_file_cookies(tmp_path, monkeypatch):
    """测试 model_discovery 从 auth.json 读取凭据进行 HTTP 探测。"""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SAPISID", "value": "test_sapisid"},
                    {"name": "SID", "value": "test_sid"},
                ]
            }
        ),
        encoding="utf-8",
    )

    service = ModelDiscoveryService()
    mock_session = MagicMock()
    mock_session._auth_file = str(auth_file)
    mock_session.get_current_auth_user = MagicMock(return_value="0")

    # Mock HTTP 探测返回
    fake_http_models = [
        {
            "id": "dynamic-gemini-test",
            "displayName": "Dynamic Gemini Test",
            "supportedGenerationMethods": ["generateContent"],
        }
    ]
    service._fetch_via_http = AsyncMock(return_value=fake_http_models)

    models = await service.get_models(session=mock_session, force_refresh=True)
    # 上游模型全部保留，且本地补充模型（gemini-4-argon / gemini-4-carbon）被合并进列表
    assert {str(m["id"]) for m in models} == {
        "dynamic-gemini-test",
        "gemini-4-argon",
        "gemini-4-carbon",
    }
    assert service._fetch_via_http.called
    call_cookies, call_auth_user = service._fetch_via_http.call_args[0]
    assert call_cookies["SAPISID"] == "test_sapisid"
    assert call_auth_user == "0"


@pytest.mark.asyncio
async def test_list_models_includes_gemini_4_argon(monkeypatch):
    """模型列表应始终包含本地补充模型 gemini-4-argon。"""
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/v1beta/models")
        assert response.status_code == 200
        names = [m["name"] for m in response.json()["models"]]
        assert "models/gemini-4-argon" in names


@pytest.mark.asyncio
async def test_get_gemini_4_argon_model_detail(monkeypatch):
    """单个模型详情接口应能返回 gemini-4-argon 元数据。"""
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/v1beta/models/gemini-4-argon")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "models/gemini-4-argon"
        assert data["displayName"] == "Gemini 4 Argon"
        assert "generateContent" in data["supportedGenerationMethods"]


@pytest.mark.asyncio
async def test_model_discovery_merges_extra_models_into_upstream_list():
    """上游 ListModels 未返回 gemini-4-argon 时，本地补充模型应被合并进列表。"""
    from unittest.mock import AsyncMock

    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )

    service = ModelDiscoveryService()
    upstream_models = [
        {"id": "gemini-3.7-flash", "displayName": "Gemini 3.7 Flash"},
    ]
    service._fetch_via_http = AsyncMock(return_value=upstream_models)

    models = await service.get_models(cookies={"SAPISID": "test"}, force_refresh=True)
    ids = [str(m["id"]) for m in models]
    # 2026-11：本地补充模型新增 gemini-4-carbon（旗舰，抓包同期目录实测）
    assert ids == ["gemini-3.7-flash", "gemini-4-argon", "gemini-4-carbon"]


@pytest.mark.asyncio
async def test_model_discovery_does_not_duplicate_extra_models():
    """上游已返回 gemini-4-argon 时，不应重复追加，且保留上游元数据。"""
    from unittest.mock import AsyncMock

    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )

    service = ModelDiscoveryService()
    upstream_models = [
        {
            "id": "gemini-4-argon",
            "displayName": "Gemini 4 Argon (upstream)",
            "description": "upstream metadata should win",
        },
    ]
    service._fetch_via_http = AsyncMock(return_value=upstream_models)

    models = await service.get_models(cookies={"SAPISID": "test"}, force_refresh=True)
    argon_entries = [m for m in models if str(m.get("id")) == "gemini-4-argon"]
    assert len(argon_entries) == 1
    assert argon_entries[0]["description"] == "upstream metadata should win"


@pytest.mark.asyncio
async def test_model_discovery_skips_extra_model_known_by_alias():
    """上游以别名形式返回该模型时，同样不重复追加。"""
    from unittest.mock import AsyncMock

    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )

    service = ModelDiscoveryService()
    upstream_models = [
        {
            "id": "gemini-4-argon-preview-001",
            "displayName": "Gemini 4 Argon Preview",
            "aliases": ["gemini-4-argon"],
        },
    ]
    service._fetch_via_http = AsyncMock(return_value=upstream_models)

    models = await service.get_models(cookies={"SAPISID": "test"}, force_refresh=True)
    ids = [str(m["id"]) for m in models]
    assert ids.count("gemini-4-argon") == 0
    assert "gemini-4-argon-preview-001" in ids


@pytest.mark.asyncio
async def test_gemini_4_argon_tier_gating_from_config():
    """gemini-4-argon 的层级门槛应由 config.yaml 的 paid_model_rules 驱动。"""
    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )

    service = ModelDiscoveryService()
    assert service.is_model_allowed_for_tier("gemini-4-argon", "Free") is False
    assert service.is_model_allowed_for_tier("gemini-4-argon", "Pro") is True
    assert service.is_model_allowed_for_tier("gemini-4-argon", "Ultra") is True
    assert service.is_paid_model("gemini-4-argon") is True
    assert service.get_required_tiers("gemini-4-argon") == ["Pro", "Ultra", "Ultra_20X"]


@pytest.mark.asyncio
async def test_model_discovery_extended_fields_and_aliases():
    """Verify ListModels parses version, temperature, topP, topK, aliases, capabilities, and accessModes."""
    from aistudio_api.api.routes_models import _to_gemini_model
    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )

    row: list[object] = [None] * 85
    row[0] = "models/gemini-4-ultra-preview"
    row[2] = "2.5"
    row[3] = "Gemini 4 Ultra Preview"
    row[4] = "Ultra reasoning model"
    row[5] = 2097152
    row[6] = 65536
    row[7] = ["generateContent", "countTokens"]
    row[8] = 0.7
    row[9] = 0.9
    row[10] = 40
    row[56] = [["models/gemini-ultra-latest", "alias"]]
    row[64] = [1, 9, 10, 12, 13, 25, 58]
    row[77] = 2
    row[82] = [4]  # Ultra only

    svc = ModelDiscoveryService()
    parsed = svc._parse_raw_models([[row]])
    assert len(parsed) == 1
    m = parsed[0]
    assert m["id"] == "gemini-4-ultra-preview"
    assert m["version"] == "2.5"
    assert m["temperature"] == 0.7
    assert m["topP"] == 0.9
    assert m["topK"] == 40
    assert m["aliases"] == ["gemini-ultra-latest"]
    caps = m["capabilities"]
    assert isinstance(caps, list)
    assert "google_search" in caps
    assert "google_maps" in caps
    assert "url_context" in caps
    assert m["paid"] is True
    assert m["accessModes"] == [4]

    svc._cached_models = parsed
    svc._last_fetched_at = 9999999999.0

    # Alias lookup works
    by_alias = await svc.get_model_by_id("models/gemini-ultra-latest")
    assert by_alias is not None
    assert by_alias["id"] == "gemini-4-ultra-preview"

    # Tier check works
    assert svc.is_model_allowed_for_tier("gemini-4-ultra-preview", "Free") is False
    assert svc.is_model_allowed_for_tier("gemini-4-ultra-preview", "Pro") is False
    assert svc.is_model_allowed_for_tier("gemini-4-ultra-preview", "Ultra") is True

    # _to_gemini_model preserves custom sampling parameters and version
    resp_model = _to_gemini_model(m)
    assert resp_model.version == "2.5"
    assert resp_model.temperature == 0.7
    assert resp_model.topP == 0.9
    assert resp_model.topK == 40


def test_to_gemini_model_limits_are_parsed_and_bad_values_fall_back():
    """上游 token 上限可能是字符串、脏值或缺失；映射必须稳健。"""
    from aistudio_api.api.routes_models import _safe_int, _to_gemini_model

    model = _to_gemini_model(
        {"id": "gemini-x", "inputTokenLimit": "12345", "outputTokenLimit": 678}
    )
    assert model.inputTokenLimit == 12345
    assert model.outputTokenLimit == 678

    # 缺失走默认值
    fallback = _to_gemini_model({"id": "gemini-y"})
    assert fallback.inputTokenLimit == 1048576
    assert fallback.outputTokenLimit == 8192

    # 脏数据不再抛 ValueError/TypeError
    dirty = _to_gemini_model(
        {"id": "gemini-z", "inputTokenLimit": "N/A", "outputTokenLimit": {"a": 1}}
    )
    assert dirty.inputTokenLimit == 1048576
    assert dirty.outputTokenLimit == 8192

    # bool 是 int 的子类，但语义上不是合法上限
    assert _safe_int(True, 7) == 7
    assert _safe_int(None, 7) == 7
    assert _safe_int("42", 7) == 42
