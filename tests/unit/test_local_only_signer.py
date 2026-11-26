"""本地签名唯一化的回归测试（远程签名与 AISTUDIO_WAA_SERVER 已移除）。

锁死三件事：
1. ``AISTUDIO_WAA_SERVER`` / ``AISTUDIO_PROOF_SERVER_URL`` /
   ``AISTUDIO_SIGNER_SECRET_URL`` 环境变量不再参与签名节点选择——
   设了也无效（历史部署残留这些变量时行为不受影响）；
2. 本地节点是唯一签名方式：config.yaml 的 ``waa`` 节（历史配置残留）
   被整体忽略，激活节点恒为 local_spidermonkey / 127.0.0.1:8081；
3. 多节点管理端点已删除（404），健康熔断端点保留（200）。
"""

from __future__ import annotations

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.config import Settings
from aistudio_api.infrastructure.gateway.model_defaults import get_local_waa_server
from aistudio_api.infrastructure.waa.signer import LocalProofSigner, WaaSigner

_REMOVED_SIGNER_ENV_VARS = (
    "AISTUDIO_WAA_SERVER",
    "AISTUDIO_PROOF_SERVER_URL",
    "AISTUDIO_SIGNER_SECRET_URL",
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.parametrize("env_name", _REMOVED_SIGNER_ENV_VARS)
def test_signer_selection_env_vars_are_ignored(monkeypatch, env_name):
    """签名选择类环境变量已删除：设任意值都不改变本地唯一节点。"""
    monkeypatch.setenv(env_name, "remote_spinsnow")
    local = get_local_waa_server()
    assert local["id"] == "local_spidermonkey"
    assert local["url"] == "http://127.0.0.1:8081"
    assert WaaSigner().server_url == "http://127.0.0.1:8081"


@pytest.mark.parametrize("env_name", _REMOVED_SIGNER_ENV_VARS)
def test_settings_no_longer_expose_signer_selection(monkeypatch, env_name):
    monkeypatch.setenv(env_name, "https://waa.example.com")
    fields = set(Settings.__dataclass_fields__)
    assert "proof_server_url" not in fields
    assert "signer_secret_url" not in fields


def test_local_node_has_fingerprint_binding():
    """固定本地节点必须携带特征绑定（UA / 指纹头），上游请求依赖它们。"""
    local = get_local_waa_server()
    assert "Firefox/152.0" in str(local["user_agent"])
    headers = local["headers"]
    assert isinstance(headers, dict) and headers.get("X-User-Agent")
    # 返回拷贝：调用方改写不得污染模块常量
    local["url"] = "http://mutated.example"
    assert get_local_waa_server()["url"] == "http://127.0.0.1:8081"


def test_local_proof_signer_defaults_to_local_node(monkeypatch):
    monkeypatch.delenv("AISTUDIO_SIGNER_TIMEOUT", raising=False)
    signer = LocalProofSigner()
    assert signer.server_url == "http://127.0.0.1:8081"
    # 本地节点默认 95s：覆盖 Go 侧 90s 冷启动预算
    assert signer.timeout == 95.0


@pytest.mark.asyncio
async def test_signer_management_endpoints_are_gone():
    """多节点管理端点已随远程签名支持一并删除。"""
    removed_paths = (
        ("GET", "/config/signer-servers"),
        ("POST", "/config/signer-servers/switch"),
        ("POST", "/config/signer-servers/reset"),
        ("PUT", "/config/signer-servers"),
        ("POST", "/config/signer-servers/test"),
        ("DELETE", "/config/signer-servers/anything"),
    )
    async with _client() as client:
        for method, path in removed_paths:
            response = await client.request(method, path)
            assert response.status_code == 404, f"{method} {path}"


@pytest.mark.asyncio
async def test_signer_health_endpoint_is_kept():
    """健康熔断端点保留（本地引擎的 proof 质量降级检测仍有效）。"""
    async with _client() as client:
        response = await client.get("/config/signer-health")
        assert response.status_code == 200
        assert "degraded" in response.json()
