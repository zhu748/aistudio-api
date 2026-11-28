"""Hermetic test environment fixtures isolating test runs from local config."""

from __future__ import annotations

import pytest

_TEST_DEFAULT_CONFIG = """
api_keys: []
compat:
  openai:
    default_model: gemini-3.8-flash
    model_mapping:
      "gpt-4o-mini": gemini-3.7-flash
  claude:
    default_model: gemini-3.8-flash
    model_mapping:
      "claude-opus": gemini-4-argon
model_defaults:
  drop_unsupported_params: false
  profiles:
    - name: image_models
      match:
        contains:
          - image
      is_image_model: true
      disable_safety_settings: true
    - name: gemma_models
      match:
        prefixes:
          - gemma-
      safety_settings:
        Harassment: 5
        Hate: 5
        Sexually Explicit: 5
        Dangerous Content: 5
    - name: gemini_models
      match:
        prefixes:
          - gemini-
      safety_settings:
        Harassment: 5
        Hate: 5
        Sexually Explicit: 5
        Dangerous Content: 5
      # 与仓库默认配置同步（2026-11 浏览器形态默认值，抓包对齐）：
      # 详见 docs/CAPTURE_ALIGNMENT_2026-11.md 与 model_defaults._default_config。
      generation_config_defaults:
        max_tokens: 65536
        temperature: 1
        top_p: 0.95
        top_k: 64
        thinking_config:
          level: 2
    - name: aux_output_models
      match:
        contains:
          - image
          - imagen
          - veo
          - lyria
          - tts
          - live
          - transcribe
          - robotics
          - omni
          - nano-banana
      clear_generation_config_indexes:
        - 3
        - 4
        - 5
        - 6
        - 13
        - 16
  models: {}
paid_model_rules:
  ultra_required:
    contains: ["veo", "ultra", "antigravity", "carbon"]
    required_tiers: ["Ultra", "Ultra_20X"]
  pro_required:
    # 与生产 config.yaml 的 pro_required.contains 保持一致（neon 为
    # 2026-11 上游新增 Pro+ 模型，夹具缺项会让依赖 config 兜底的
    # 测试在生产/夹具间行为分叉）。
    contains: ["argon", "neon", "image", "imagen", "omni", "deep-research", "lyria"]
    required_tiers: ["Pro", "Ultra", "Ultra_20X"]
rotator:
  max_retries: 5
  strategy: sticky
  stickiness_bonus: 400.0
  stickiness_threshold: 0.8
  drip_penalty: 500.0
  tier_weights:
    Free: 0.0
    Plus: 0.5
    Pro: 1.0
    Ultra: 2.0
    Ultra_20X: 3.0
  cooldowns:
    rate_limited_short: 60
    rate_limited_drip: 300
    rate_limited_drip_extended: 600
tier_quotas:
  Free:
    flash_models:
      match:
        contains: ["flash"]
      daily_limit: 100
    pro_models:
      match:
        contains: ["pro"]
      daily_limit: 10
    default_limit: 20
  Pro:
    argon_models:
      match:
        contains: ["argon", "carbon"]
      daily_limit: 50
    flash_models:
      match:
        contains: ["flash"]
      daily_limit: 150
    pro_models:
      match:
        contains: ["pro"]
      daily_limit: 50
    image_models:
      match:
        contains: ["image"]
      daily_limit: 50
    default_limit: 100
  Ultra:
    argon_models:
      match:
        contains: ["argon", "carbon"]
      daily_limit: 250
    flash_models:
      match:
        contains: ["flash"]
      daily_limit: 1500
    pro_models:
      match:
        contains: ["pro"]
      daily_limit: 250
    image_models:
      match:
        contains: ["image"]
      daily_limit: 250
    default_limit: 500
  overrides: {}
account_presets:
  - id: preset_all
    name: 全部启用 (最大并发)
    description: 激活所有可用账号，提供最充沛的并发与负载池
    action: enable_all
    is_system: true
  - id: preset_paid_only
    name: 纯高阶付费池 (Pro/Ultra)
    description: 仅激活 Pro 与 Ultra 等级账号，全力保障付费模型调用
    action: filter_tier
    target_tiers: ["Pro", "Ultra"]
    is_system: true
  - id: preset_free_only
    name: 纯免费节能池 (Free)
    description: 仅激活 Free 等级账号，用于日常普通模型测试
    action: filter_tier
    target_tiers: ["Free"]
    is_system: true
  - id: preset_production
    name: 生产专属集群 (Production)
    description: 仅激活 group 标记为 production 的核心业务账号
    action: filter_group
    target_groups: ["production"]
    is_system: true
"""


@pytest.fixture(scope="session")
def _shared_test_env(tmp_path_factory):
    base_dir = tmp_path_factory.mktemp("test_env")
    config_file = base_dir / "test_isolated_config.yaml"
    config_file.write_text(_TEST_DEFAULT_CONFIG, encoding="utf-8")
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "accounts").mkdir(exist_ok=True)
    return config_file, data_dir


@pytest.fixture(scope="session", autouse=True)
def _close_request_history_on_exit():
    """测试会话结束时关闭 RequestHistory 写路径长连接。

    ``request_history()`` 是进程级单例并复用一条 sqlite 写连接；不显式关闭时，
    解释器退出阶段由 GC 终结会抛 ``ResourceWarning: unclosed database``，
    在 ``filterwarnings = error`` 下会把整个测试会话标成失败。
    """
    yield
    from aistudio_api.infrastructure.request_history import close_all_histories

    close_all_histories()


@pytest.fixture(autouse=True)
def isolate_test_config(monkeypatch, _shared_test_env):
    """Isolate all test runs from repository-level config.yaml modifications and runtime state files."""
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "allow_no_auth", True)
    config_file, data_dir = _shared_test_env
    stats_file = data_dir / "stats.json"
    rotator_file = data_dir / "rotator_state.json"
    accounts_dir = data_dir / "accounts"

    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AISTUDIO_ACCOUNTS_DIR", str(accounts_dir))
    monkeypatch.setenv("AISTUDIO_STATS_FILE", str(stats_file))
    monkeypatch.setenv("AISTUDIO_ROTATOR_STATE_FILE", str(rotator_file))
    # 凭据类 env 清零：开发 shell / .env 携带的 AISTUDIO_API_KEY(S) 等
    # 会让精确断言（frozenset 相等）被单数别名变量击穿。
    for var in (
        "AISTUDIO_API_KEY",
        "AISTUDIO_API_KEYS",
        "AISTUDIO_WEB_PASSWORD",
        "AISTUDIO_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Settings 单例在 import 期已用旧环境构造完毕：其 data_dir / stats_file
    # / rotator_state_file / accounts_dir / auth_file 字段仍指向真实仓库
    # 路径（凡读 settings.* 的代码未被 env 隔离，与 resolve_*() 双真相源）。
    # 直接覆盖字段，使两套真相源收敛到同一临时目录。
    monkeypatch.setattr(settings, "data_dir", str(data_dir))
    monkeypatch.setattr(settings, "stats_file", str(stats_file))
    monkeypatch.setattr(settings, "rotator_state_file", str(rotator_file))
    monkeypatch.setattr(settings, "accounts_dir", str(accounts_dir))
    monkeypatch.setattr(settings, "auth_file", None)
    yield
    # 注意：不能整目录清空——request_history() 单例持有指向 requests.sqlite3
    # 的长连接，删除文件后后续测试的读写会落在已删除的 inode 上。stats/
    # rotator 状态文件是唯一会影响跨测试语义的持久状态；accounts/ 与
    # requests.sqlite3 留在 session 级 tmp 目录由 pytest 自动回收。
    if stats_file.exists():
        stats_file.unlink()
    if rotator_file.exists():
        rotator_file.unlink()
