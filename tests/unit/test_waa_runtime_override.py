"""Tests for WAA signer-server runtime override semantics.

优先级链（高 → 低）：
1. 运行时覆盖（面板手动切换写入，进程内即时生效）
2. 环境变量 AISTUDIO_WAA_SERVER（启动默认，重启后恢复）
3. config.yaml 的 waa.active_server

核心诉求：即便用环境变量注入了默认节点（本地或远程），
面板切换也能对当前进程立即生效，无需重启。
"""

from __future__ import annotations

import httpx
import pytest
import yaml

from aistudio_api.api.app import app
from aistudio_api.infrastructure.gateway.model_defaults import (
    clear_runtime_waa_override,
    get_active_waa_server,
    get_runtime_waa_override,
    invalidate_config_cache,
    set_runtime_waa_override,
)

_CONFIG_YAML = """
waa:
  active_server: local_spidermonkey
  servers:
    - id: local_spidermonkey
      name: 本地原生引擎 (SpiderMonkey 152)
      url: http://127.0.0.1:8081
      user_agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0
      headers:
        X-User-Agent: grpc-web-javascript/0.1
    - id: remote_spinsnow
      name: 远程公共签名器 (spinsnow)
      url: https://waa.spinsnow.fun
      description: 社区公共远程 WAA 签名节点
      user_agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0
      headers:
        X-User-Agent: grpc-web-javascript/0.1
"""


@pytest.fixture
def waa_config(tmp_path, monkeypatch):
    """隔离的 config.yaml + 干净的运行时覆盖状态。"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config_file))
    monkeypatch.delenv("AISTUDIO_WAA_SERVER", raising=False)
    set_runtime_waa_override(None)
    invalidate_config_cache()
    yield config_file
    set_runtime_waa_override(None)
    invalidate_config_cache()


@pytest.fixture
def no_daemon(monkeypatch):
    """屏蔽本地签名守护进程的拉起/停止副作用（路由内为函数级导入，patch 模块属性即可）。"""
    from aistudio_api.infrastructure.waa import daemon

    monkeypatch.setattr(daemon, "ensure_signer_ready", lambda: None)
    monkeypatch.setattr(daemon, "stop_signer_daemon", lambda: None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ─── 优先级链（函数级） ───


def test_precedence_config_env_runtime(waa_config, monkeypatch):
    # 1. 仅 config：local_spidermonkey
    assert get_active_waa_server()["id"] == "local_spidermonkey"

    # 2. 环境变量覆盖 config
    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")
    assert get_active_waa_server()["id"] == "remote_spinsnow"

    # 3. 运行时覆盖优先于环境变量（面板切换即时生效的关键）
    set_runtime_waa_override("local_spidermonkey")
    assert get_active_waa_server()["id"] == "local_spidermonkey"

    # 4. 清除覆盖后回到环境变量默认
    assert clear_runtime_waa_override() == "local_spidermonkey"
    assert get_active_waa_server()["id"] == "remote_spinsnow"

    # 5. 清除环境变量后回到 config
    monkeypatch.delenv("AISTUDIO_WAA_SERVER")
    assert get_active_waa_server()["id"] == "local_spidermonkey"


def test_runtime_override_helpers():
    set_runtime_waa_override("  remote_spinsnow  ")
    assert get_runtime_waa_override() == "remote_spinsnow"
    # 空白值视为清除
    set_runtime_waa_override("   ")
    assert get_runtime_waa_override() is None
    # 重复清除返回 None（幂等）
    assert clear_runtime_waa_override() is None


def test_switch_active_waa_server_sets_runtime_override(waa_config, monkeypatch):
    """环境变量指定默认节点时，switch 同样写入运行时覆盖并立即生效。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        switch_active_waa_server,
    )

    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")
    active = switch_active_waa_server("local_spidermonkey")
    assert active["id"] == "local_spidermonkey"
    assert get_runtime_waa_override() == "local_spidermonkey"
    # 持久化到 config.yaml
    saved = yaml.safe_load(waa_config.read_text(encoding="utf-8"))
    assert saved["waa"]["active_server"] == "local_spidermonkey"


# ─── HTTP 端点 ───


@pytest.mark.asyncio
async def test_get_signer_servers_reports_env_default_and_runtime(
    waa_config, monkeypatch
):
    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")
    set_runtime_waa_override("local_spidermonkey")

    async with _client() as client:
        resp = await client.get("/config/signer-servers")
    assert resp.status_code == 200
    data = resp.json()

    # 实际生效节点 = 运行时覆盖
    assert data["active_server"] == "local_spidermonkey"
    assert data["env_override"] == "remote_spinsnow"
    assert data["runtime_override"] == "local_spidermonkey"

    by_id = {s["id"]: s for s in data["servers"]}
    assert by_id["local_spidermonkey"]["is_active"] is True
    assert by_id["remote_spinsnow"]["is_active"] is False
    # 环境变量默认徽标挂在 remote_spinsnow 上（而非激活节点）
    assert by_id["remote_spinsnow"]["is_env_default"] is True
    assert by_id["local_spidermonkey"]["is_env_default"] is False


@pytest.mark.asyncio
async def test_switch_endpoint_works_under_env(waa_config, monkeypatch, no_daemon):
    """环境变量存在时，面板切换接口对当前进程立即生效（核心诉求）。"""
    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")

    async with _client() as client:
        resp = await client.post(
            "/config/signer-servers/switch",
            json={"server_id": "local_spidermonkey"},
        )
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert data["active_server"]["id"] == "local_spidermonkey"
    assert data["runtime_override"] == "local_spidermonkey"
    # 切换已实际生效（非旧的“仅写配置文件”）
    assert get_active_waa_server()["id"] == "local_spidermonkey"
    # 信息提示说明重启后的回退行为（不再是不生效的 warning）
    assert data["note"] is not None
    assert "AISTUDIO_WAA_SERVER=remote_spinsnow" in data["note"]

    # 持久化同步落盘
    saved = yaml.safe_load(waa_config.read_text(encoding="utf-8"))
    assert saved["waa"]["active_server"] == "local_spidermonkey"


@pytest.mark.asyncio
async def test_switch_endpoint_no_note_when_env_matches(
    waa_config, monkeypatch, no_daemon
):
    """切换目标与环境变量默认一致时无需信息提示。"""
    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "local_spidermonkey")

    async with _client() as client:
        resp = await client.post(
            "/config/signer-servers/switch",
            json={"server_id": "local_spidermonkey"},
        )
    assert resp.status_code == 200
    assert resp.json()["note"] is None


@pytest.mark.asyncio
async def test_reset_endpoint_restores_env_default(waa_config, monkeypatch, no_daemon):
    """临时切换后一键恢复环境变量指定的默认节点。"""
    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")
    set_runtime_waa_override("local_spidermonkey")

    async with _client() as client:
        resp = await client.post("/config/signer-servers/reset")
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert data["changed"] is True
    assert data["active_server"]["id"] == "remote_spinsnow"
    assert data["runtime_override"] is None
    assert get_active_waa_server()["id"] == "remote_spinsnow"


@pytest.mark.asyncio
async def test_reset_endpoint_without_override_is_noop(waa_config, no_daemon):
    async with _client() as client:
        resp = await client.post("/config/signer-servers/reset")
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] is False
    # 无覆盖时保持 config 默认
    assert data["active_server"]["id"] == "local_spidermonkey"


@pytest.mark.asyncio
async def test_switch_endpoint_rejects_unknown_server(waa_config, no_daemon):
    async with _client() as client:
        resp = await client.post(
            "/config/signer-servers/switch",
            json={"server_id": "nonexistent_node"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_active_server_clears_stale_override(waa_config, monkeypatch):
    """删除覆盖所指向的节点时，同步清理运行时覆盖避免悬空。"""
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(waa_config))
    set_runtime_waa_override("remote_spinsnow")

    async with _client() as client:
        # 激活节点（运行时覆盖指向 remote_spinsnow），删除它
        resp = await client.delete("/config/signer-servers/remote_spinsnow")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    saved = yaml.safe_load(waa_config.read_text(encoding="utf-8"))
    assert saved["waa"]["active_server"] == "local_spidermonkey"
    assert get_runtime_waa_override() is None


@pytest.mark.asyncio
async def test_failed_local_switch_reports_actual_remote_node(waa_config, monkeypatch):
    """重现截图：缺本地二进制时回退远程，不能提示本地切换成功。"""
    from aistudio_api.infrastructure.waa import daemon

    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")
    monkeypatch.setattr(daemon.Path, "is_file", lambda self: False)
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)

    async with _client() as client:
        resp = await client.post(
            "/config/signer-servers/switch", json={"server_id": "local_spidermonkey"}
        )
        current = (await client.get("/config/signer-servers")).json()

    data = resp.json()
    assert data["ok"] is False
    assert data["note"] is None
    assert "未检测到" in data["error"]
    assert data["active_server"]["id"] == current["active_server"] == "remote_spinsnow"
    assert data["runtime_override"] == current["runtime_override"] == "remote_spinsnow"


@pytest.mark.asyncio
async def test_local_health_test_starts_inactive_daemon_without_switching(
    waa_config, monkeypatch
):
    from aistudio_api.infrastructure.waa import daemon

    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")
    prepared: list[str | None] = []

    def prepare(*, server_url=None):
        prepared.append(server_url)

    monkeypatch.setattr(daemon, "ensure_signer_ready", prepare)
    # 只替换签名器探活 HTTP transport，面板的 ASGI 客户端保持真实。
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    original_client = httpx.AsyncClient
    kwargs_seen: list[dict] = []

    def signer_client(**kwargs):
        kwargs_seen.append(kwargs)
        return original_client(transport=transport, **kwargs)

    async with _client() as client:
        monkeypatch.setattr(httpx, "AsyncClient", signer_client)
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "http://127.0.0.1:8081"}
        )

    assert resp.json()["ok"] is True
    assert prepared == ["http://127.0.0.1:8081"]
    assert kwargs_seen[0]["trust_env"] is False
    assert kwargs_seen[0]["proxy"] is None
    assert get_active_waa_server()["id"] == "remote_spinsnow"
    assert get_runtime_waa_override() is None


@pytest.mark.asyncio
async def test_local_health_test_returns_startup_diagnostic(waa_config, monkeypatch):
    from aistudio_api.infrastructure.waa import daemon

    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "remote_spinsnow")
    monkeypatch.setattr(daemon.Path, "is_file", lambda self: False)
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)

    async with _client() as client:
        resp = await client.post(
            "/config/signer-servers/test", json={"url": "http://127.0.0.1:8081"}
        )

    assert resp.json()["ok"] is False
    assert "未检测到" in resp.json()["error"]
    assert get_active_waa_server()["id"] == "remote_spinsnow"
    assert get_runtime_waa_override() is None


@pytest.mark.asyncio
async def test_failed_reset_reports_fallback_runtime_override(waa_config, monkeypatch):
    from aistudio_api.infrastructure.waa import daemon

    monkeypatch.setenv("AISTUDIO_WAA_SERVER", "local_spidermonkey")
    set_runtime_waa_override("remote_spinsnow")
    monkeypatch.setattr(daemon.Path, "is_file", lambda self: False)
    monkeypatch.setattr(daemon, "is_service_healthy", lambda url: False)
    monkeypatch.setattr(daemon, "is_port_open", lambda host, port: False)

    async with _client() as client:
        resp = await client.post("/config/signer-servers/reset")

    data = resp.json()
    assert data["ok"] is False
    assert "未检测到" in data["error"]
    assert data["active_server"]["id"] == "remote_spinsnow"
    assert data["runtime_override"] == get_runtime_waa_override() == "remote_spinsnow"
