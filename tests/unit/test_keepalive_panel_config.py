"""出站保活面板配置（network.keepalive_*）单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aistudio_api.infrastructure.gateway.model_defaults import (
    get_keepalive_settings,
    invalidate_config_cache,
    update_keepalive_settings,
)
from aistudio_api.infrastructure.outbound_keepalive import (
    OutboundKeepAliveService,
    validate_keepalive_url,
)


def _write_config(tmp_path: Path, content: str) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    return config


# ---------------------------------------------------------------------------
# 读：默认 / 已配置
# ---------------------------------------------------------------------------


def test_defaults_when_section_missing(tmp_path, monkeypatch):
    config = _write_config(tmp_path, "api_keys: []\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()
    assert get_keepalive_settings() == {
        "url": "",
        "interval": 60,
        "allow_private": False,
    }


def test_reads_existing_network_section(tmp_path, monkeypatch):
    config = _write_config(
        tmp_path,
        "network:\n  keepalive_url: https://example.com/healthz\n"
        "  keepalive_interval: 45\n  keepalive_allow_private: true\n",
    )
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()
    assert get_keepalive_settings() == {
        "url": "https://example.com/healthz",
        "interval": 45,
        "allow_private": True,
    }


def test_interval_below_floor_is_clamped_on_read(tmp_path, monkeypatch):
    config = _write_config(tmp_path, "network:\n  keepalive_interval: 1\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()
    assert get_keepalive_settings()["interval"] == 5


# ---------------------------------------------------------------------------
# 写：round-trip / 去重 / 夹紧
# ---------------------------------------------------------------------------


def test_update_round_trip(tmp_path, monkeypatch):
    config = _write_config(tmp_path, "api_keys: []\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()

    result = update_keepalive_settings(
        url=" https://example.com/healthz ",
        interval=200,
        allow_private=True,
    )
    assert result == {
        "url": "https://example.com/healthz",
        "interval": 200,
        "allow_private": True,
    }
    # 落盘内容
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["network"] == {
        "keepalive_url": "https://example.com/healthz",
        "keepalive_interval": 200,
        "keepalive_allow_private": True,
    }
    # 重新读取（含缓存失效验证）
    invalidate_config_cache()
    assert get_keepalive_settings()["url"] == "https://example.com/healthz"


def test_update_overwrites_existing_values(tmp_path, monkeypatch):
    config = _write_config(
        tmp_path,
        "network:\n  keepalive_url: https://old.example/healthz\n"
        "  keepalive_interval: 30\n  keepalive_allow_private: false\n",
    )
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()

    update_keepalive_settings(url="", interval=120, allow_private=False)
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["network"]["keepalive_url"] == ""
    assert parsed["network"]["keepalive_interval"] == 120


def test_update_interval_clamped(tmp_path, monkeypatch):
    config = _write_config(tmp_path, "api_keys: []\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()

    low = update_keepalive_settings(url="", interval=1, allow_private=False)
    high = update_keepalive_settings(url="", interval=10**6, allow_private=False)
    assert low["interval"] == 5
    assert high["interval"] == 86400


def test_update_preserves_other_config_keys(tmp_path, monkeypatch):
    config = _write_config(
        tmp_path,
        "api_keys: []\ncompat:\n  openai:\n    default_model: gemini-3.7-flash\n",
    )
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    invalidate_config_cache()

    update_keepalive_settings(url="", interval=60, allow_private=False)
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["compat"]["openai"]["default_model"] == "gemini-3.7-flash"


# ---------------------------------------------------------------------------
# URL 校验（私网防护语义）
# ---------------------------------------------------------------------------


def test_validate_rejects_private_by_default():
    assert validate_keepalive_url("http://127.0.0.1:8080/healthz") is False
    assert validate_keepalive_url("https://10.0.0.5/healthz") is False
    assert validate_keepalive_url("https://192.168.1.1/healthz") is False


def test_validate_accepts_private_when_allowed():
    assert (
        validate_keepalive_url("http://127.0.0.1:8080/healthz", allow_private=True)
        is True
    )


def test_validate_rejects_malformed():
    assert validate_keepalive_url("not-a-url") is False
    assert validate_keepalive_url("ftp://example.com") is False
    # 空串在既有语义中合法（=「未配置」，不参与校验）。
    assert validate_keepalive_url("") is True


# ---------------------------------------------------------------------------
# 保活服务重启语义（面板保存 → 立即生效）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_restart_picks_up_new_config(tmp_path, monkeypatch):
    config = _write_config(
        tmp_path,
        "network:\n  keepalive_url: https://example.com/healthz\n"
        "  keepalive_interval: 60\n  keepalive_allow_private: false\n",
    )
    # Settings 是 dataclass：类体默认值在 import 时已求值（monkeypatch
    # resolve_config_file 为时已晚），故直接在实例上注入读到的 yaml 值，
    # 等价于真实进程读取同一文件后构造的 Settings。
    import aistudio_api.infrastructure.gateway.model_defaults as md
    from aistudio_api.config import Settings

    monkeypatch.setattr(md, "_resolve_config_path", lambda p=None: config)
    invalidate_config_cache()
    yaml_settings = md.get_keepalive_settings()

    fresh = Settings()
    fresh.keepalive_url = str(yaml_settings["url"])
    fresh.keepalive_interval = int(str(yaml_settings["interval"]))  # type: ignore[arg-type]
    fresh.keepalive_allow_private = bool(yaml_settings["allow_private"])
    monkeypatch.setattr(
        "aistudio_api.infrastructure.outbound_keepalive.settings", fresh
    )
    # 服务按配置真正起任务（真实任务句柄；_run 内的网络调用由 URL 间隔
    # 60s 保证首轮 sleep 前不会立刻发请求，断言只看任务存在性）。
    service = OutboundKeepAliveService()
    await service.start()
    try:
        assert service.is_running is True
    finally:
        await service.stop()
    assert service.is_running is False

    # 清空 URL 后重启 → 不应启动
    update_keepalive_settings(url="", interval=60, allow_private=False)
    invalidate_config_cache()
    yaml_after = md.get_keepalive_settings()
    fresh2 = Settings()
    fresh2.keepalive_url = str(yaml_after["url"])
    fresh2.keepalive_interval = int(str(yaml_after["interval"]))  # type: ignore[arg-type]
    fresh2.keepalive_allow_private = bool(yaml_after["allow_private"])
    monkeypatch.setattr(
        "aistudio_api.infrastructure.outbound_keepalive.settings", fresh2
    )
    await service.restart()
    assert service.is_running is False


# ---------------------------------------------------------------------------
# 运行时覆盖语义（环境变量注入时面板保存 → 覆盖 env 立即生效）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_runtime_override():
    """隔离模块级运行时覆盖状态，防测试间泄漏。"""
    import aistudio_api.infrastructure.outbound_keepalive as ok_module

    ok_module._runtime_override = None
    yield
    ok_module._runtime_override = None


def test_runtime_override_priority_over_settings():
    """覆盖三元组优先于（import 时固化的）settings 默认链。"""
    import aistudio_api.infrastructure.outbound_keepalive as ok_module

    ok_module._runtime_override = ("https://override.example/healthz", 25, True)
    svc = OutboundKeepAliveService()
    assert svc._current_config() == ("https://override.example/healthz", 25)
    assert ok_module.keepalive_private_targets_allowed() is True

    ok_module._runtime_override = None
    # 回退 settings 默认链（测试环境 yaml 中无 network 段 → 空 URL）
    assert svc._current_config()[0] == ""
    assert ok_module.keepalive_runtime_override_active() is False


def test_set_and_clear_runtime_override():
    """set/clear/active 三 API 的形状与钳制语义。"""
    import aistudio_api.infrastructure.outbound_keepalive as ok_module

    ok_module.set_keepalive_runtime_override(
        url=" https://panel.example/healthz ", interval=2, allow_private=True
    )
    assert ok_module._runtime_override == (
        "https://panel.example/healthz",
        5,  # 低于下限 5 被钳制
        True,
    )
    assert ok_module.keepalive_runtime_override_active() is True

    ok_module.clear_keepalive_runtime_override()
    assert ok_module._runtime_override is None
    assert ok_module.keepalive_runtime_override_active() is False


def test_env_prefill_and_injection_badges(tmp_path, monkeypatch):
    """payload：env 注入字段预填 env 值并标注；未注入字段保持 yaml 值。"""
    from aistudio_api.api.routes_config import _keepalive_payload

    config = _write_config(
        tmp_path,
        "network:\n  keepalive_url: https://yaml.example/healthz\n"
        "  keepalive_interval: 90\n  keepalive_allow_private: true\n",
    )
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults._resolve_config_path",
        lambda p=None: config,
    )
    invalidate_config_cache()
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_URL", "https://env.example/healthz")
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_INTERVAL", "33")
    monkeypatch.delenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", raising=False)

    payload = _keepalive_payload()
    # url/interval 被 env 注入 → 预填 env 值
    assert payload["url"] == "https://env.example/healthz"
    assert payload["interval"] == 33
    # allow_private 未注入 → 保持 yaml 值
    assert payload["allow_private"] is True
    assert payload["env_overrides"] == {
        "url": "AISTUDIO_KEEPALIVE_URL",
        "interval": "AISTUDIO_KEEPALIVE_INTERVAL",
    }
    # yaml 原值仍完整回传（供前端展示落盘值）
    assert payload["yaml_url"] == "https://yaml.example/healthz"
    assert payload["yaml_interval"] == 90
    assert payload["yaml_allow_private"] is True


def test_payload_without_env_uses_yaml_values(tmp_path, monkeypatch):
    """无 env 注入时 payload 纯 yaml 值（env_overrides 为空）。"""
    from aistudio_api.api.routes_config import _keepalive_payload

    config = _write_config(
        tmp_path,
        "network:\n  keepalive_url: https://yaml.example/healthz\n"
        "  keepalive_interval: 120\n  keepalive_allow_private: false\n",
    )
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults._resolve_config_path",
        lambda p=None: config,
    )
    invalidate_config_cache()
    monkeypatch.delenv("AISTUDIO_KEEPALIVE_URL", raising=False)
    monkeypatch.delenv("AISTUDIO_KEEPALIVE_INTERVAL", raising=False)
    monkeypatch.delenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", raising=False)

    payload = _keepalive_payload()
    assert payload["url"] == "https://yaml.example/healthz"
    assert payload["interval"] == 120
    assert payload["allow_private"] is False
    assert payload["env_overrides"] == {}


def test_env_prefill_bool_and_int_parsing(monkeypatch):
    """env 值解析口径与 Settings 一致（int 失败回退 60、bool 0/false 判假）。"""
    from aistudio_api.api.routes_config import _keepalive_env_prefill

    monkeypatch.setenv("AISTUDIO_KEEPALIVE_URL", " https://env.example/hz ")
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_INTERVAL", "not-a-number")
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", "0")
    prefill = _keepalive_env_prefill()
    assert prefill["url"] == "https://env.example/hz"
    assert prefill["interval"] == 60
    assert prefill["allow_private"] is False

    monkeypatch.setenv("AISTUDIO_KEEPALIVE_INTERVAL", " 45 ")
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", "True")
    prefill2 = _keepalive_env_prefill()
    assert prefill2["interval"] == 45
    assert prefill2["allow_private"] is True


@pytest.mark.asyncio
async def test_put_sets_override_and_restarts(tmp_path, monkeypatch):
    """PUT /config/keepalive：写 yaml + 设运行时覆盖 + 重启服务（environ/env 注入下）。"""
    import aistudio_api.api.routes_config as routes_config
    import aistudio_api.infrastructure.outbound_keepalive as ok_module
    from aistudio_api.infrastructure.outbound_keepalive import (
        OutboundKeepAliveService,
    )

    config = _write_config(tmp_path, "api_keys: []\n")
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults._resolve_config_path",
        lambda p=None: config,
    )
    invalidate_config_cache()
    # 环境变量注入（面板保存应覆盖它，而不是被 env 挡住）
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_URL", "https://env.example/healthz")
    monkeypatch.setenv("AISTUDIO_KEEPALIVE_INTERVAL", "600")

    # 隔离全局服务实例与全局覆盖状态
    stub_service = OutboundKeepAliveService()
    monkeypatch.setattr(ok_module, "outbound_keepalive_service", stub_service)
    monkeypatch.setattr(
        routes_config, "outbound_keepalive_service", stub_service, raising=False
    )
    restarted = []

    async def fake_restart() -> None:
        restarted.append(stub_service._current_config())

    monkeypatch.setattr(stub_service, "restart", fake_restart)

    req = routes_config.UpdateKeepaliveConfigRequest(
        url="https://panel.example/healthz", interval=30, allow_private=False
    )
    result = await routes_config.update_keepalive_config(req)

    # 覆盖已设为面板值（优先于 env 注入）
    assert ok_module._runtime_override == (
        "https://panel.example/healthz",
        30,
        False,
    )
    # 服务按覆盖值重启（_current_config 读覆盖，不是 env 的 600）
    assert restarted and restarted[0] == ("https://panel.example/healthz", 30)
    # payload 回显面板值 + yaml 落盘值
    assert result["url"] == "https://panel.example/healthz"
    assert result["yaml_url"] == "https://panel.example/healthz"
    assert result["runtime_override_active"] is True
    # yaml 已落盘
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["network"]["keepalive_url"] == "https://panel.example/healthz"
    assert parsed["network"]["keepalive_interval"] == 30
