from aistudio_api.infrastructure.gateway.model_defaults import resolve_model_defaults
from aistudio_api.infrastructure.gateway.wire_types import AistudioImageOutputMode


def test_resolve_model_defaults_uses_repo_config_for_gemma():
    defaults = resolve_model_defaults("models/gemma-4-31b-it")

    assert defaults.default_tools == ()
    assert defaults.is_image_model is False
    assert defaults.safety_settings == (
        (None, None, 7, 5),
        (None, None, 8, 5),
        (None, None, 9, 5),
        (None, None, 10, 5),
    )


def test_resolve_model_defaults_merges_yaml_profiles(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_defaults:
  profiles:
    - name: gemini_models
      match:
        prefixes: [gemini-]
      generation_config_defaults:
        response_mime_type: application/json
    - name: image_models
      match:
        contains: [image]
      is_image_model: true
      generation_config_defaults:
        image_output_mode: text_and_image
        thinking_config:
          level: HIGH
          mode: 9
        media_resolution: HIGH
      clear_generation_config_indexes: [7, 17]
      disable_safety_settings: true
  models:
    gemini-3.1-flash-image-preview:
      generation_config_defaults:
        response_mime_type: null
"""
    )

    defaults = resolve_model_defaults(
        "models/gemini-3.1-flash-image-preview", config_path=config_path
    )

    assert defaults.is_image_model is True
    assert defaults.disable_safety_settings is True
    assert defaults.clear_generation_config_indexes == (7, 17)
    assert defaults.generation_config_defaults["response_mime_type"] is None
    assert (
        defaults.generation_config_defaults["image_output_mode"]
        == AistudioImageOutputMode.text_and_image()
    )
    assert defaults.generation_config_defaults["thinking_config"] == [9, None, None, 3]
    assert defaults.generation_config_defaults["media_resolution"] == 3


def test_resolve_model_defaults_coerces_safety_settings(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_defaults:
  profiles:
    - name: gemini_models
      match:
        prefixes: [gemini-]
      safety_settings:
        Harassment: 1
        Hate: 2
        Sexually Explicit: 3
        Dangerous Content: 5
"""
    )

    defaults = resolve_model_defaults(
        "models/gemini-2.5-flash", config_path=config_path
    )

    assert defaults.safety_settings == (
        (None, None, 7, 1),
        (None, None, 8, 2),
        (None, None, 9, 3),
        (None, None, 10, 5),
    )


def test_resolve_model_defaults_supports_image_default_tool_names(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_defaults:
  profiles:
    - name: image_models
      match:
        contains: [image]
      is_image_model: true
      default_tools:
        - google_search_and_image_search
"""
    )

    defaults = resolve_model_defaults(
        "models/gemini-3.1-flash-image-preview", config_path=config_path
    )

    assert defaults.is_image_model is True
    assert defaults.default_tools == ("google_search_and_image_search",)


def test_resolve_model_defaults_keeps_gemini_four_tool_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_defaults:
  profiles:
    - name: gemini_models
      match:
        prefixes: [gemini-]
      default_tools:
        - google_search
        - code_execution
        - google_maps
        - url_context
"""
    )

    defaults = resolve_model_defaults(
        "models/gemini-3.5-flash", config_path=config_path
    )

    assert defaults.default_tools == (
        "google_search",
        "code_execution",
        "google_maps",
        "url_context",
    )


def test_resolve_model_defaults_model_override_replaces_profile_tools_and_flags(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_defaults:
  profiles:
    - name: gemini_models
      match:
        prefixes: [gemini-]
      default_tools:
        - google_search
      disable_safety_settings: true
  models:
    gemini-3.5-flash:
      default_tools:
        - code_execution
      disable_safety_settings: false
"""
    )

    defaults = resolve_model_defaults(
        "models/gemini-3.5-flash", config_path=config_path
    )

    assert defaults.default_tools == ("code_execution",)
    assert defaults.disable_safety_settings is False


def test_resolve_model_defaults_drop_unsupported_params_inheritance(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_defaults:
  drop_unsupported_params: true
  profiles:
    - name: strict_profile
      match:
        prefixes: [strict-]
      drop_unsupported_params: false
    - name: loose_profile
      match:
        prefixes: [loose-]
  models:
    strict-override:
      drop_unsupported_params: true
"""
    )

    assert (
        resolve_model_defaults(
            "loose-model", config_path=config_path
        ).drop_unsupported_params
        is True
    )
    assert (
        resolve_model_defaults(
            "strict-model", config_path=config_path
        ).drop_unsupported_params
        is False
    )
    assert (
        resolve_model_defaults(
            "strict-override", config_path=config_path
        ).drop_unsupported_params
        is True
    )


def test_canonicalize_model_alias_resolves_known_aliases():
    """别名必须解析到 canonical id，且解析后解析出的默认值与原 id 一致。

    回归点：resolve_model_defaults 旧实现直接用 normalize_model_name，别名
    （如 nano-banana）拿不到 image 模型 profile，is_image_model 判为 False。
    """
    from aistudio_api.infrastructure.gateway.model_defaults import (
        canonicalize_model_alias,
        normalize_model_name,
        resolve_model_defaults,
    )

    pairs = {
        "nano-banana": "gemini-3.1-flash-lite-image",
        "nano-banana-pro": "gemini-3-pro-image",
        "gemini-3.1-flash-image-preview": "gemini-3.1-flash-image",
    }
    for alias, canonical in pairs.items():
        assert canonicalize_model_alias(alias) == canonical

    # 稳定 key 归一化器绝不能改写别名（账号轮换的统计/冷却 key 依赖它）
    assert normalize_model_name("nano-banana") == "nano-banana"

    # 别名与 canonical 解析出的默认值一致
    alias_defaults = resolve_model_defaults("nano-banana")
    canonical_defaults = resolve_model_defaults("gemini-3.1-flash-lite-image")
    assert alias_defaults == canonical_defaults
    assert alias_defaults.is_image_model is True

    # 未知名字原样返回
    assert canonicalize_model_alias("totally-unknown-model") == "totally-unknown-model"
    assert canonicalize_model_alias("") == ""


def test_resolve_tier_quota_is_memoized_and_invalidated_by_mtime(tmp_path):
    """额度解析被记忆化（/stats 与选路热路径），但配置变更必须立即生效。"""
    import os

    from aistudio_api.infrastructure.gateway.model_defaults import (
        invalidate_config_cache,
        resolve_tier_quota,
    )

    config_path = tmp_path / "config.yaml"

    def _write_limit(limit: int, mtime: int) -> None:
        config_path.write_text(
            f"""
tier_quotas:
  Pro:
    default_limit: {limit}
""",
            encoding="utf-8",
        )
        os.utime(config_path, (mtime, mtime))

    _write_limit(77, 1_700_000_000)
    assert resolve_tier_quota("Pro", "gemini-3.7-flash", config_path=config_path) == 77

    # 同内容重复调用走缓存
    assert resolve_tier_quota("Pro", "gemini-3.7-flash", config_path=config_path) == 77

    # mtime 变化即失效
    _write_limit(123, 1_700_000_100)
    assert resolve_tier_quota("Pro", "gemini-3.7-flash", config_path=config_path) == 123

    # 显式 invalidate 后仍然读到磁盘上的最新值
    _write_limit(9, 1_700_000_200)
    invalidate_config_cache()
    assert resolve_tier_quota("Pro", "gemini-3.7-flash", config_path=config_path) == 9
