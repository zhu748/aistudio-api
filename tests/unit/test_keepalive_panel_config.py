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
