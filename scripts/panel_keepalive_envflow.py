"""保活环境变量注入交互重构（批次 A：后端 service 覆盖层 + payload 重构）。

环境变量注入时表单预填 env 值 + 「环境变量注入」徽章；框内修改保存后
运行时覆盖 env（重启后回退 env）。同时修掉 service 读 import 时固化
settings、面板写 yaml 后 restart 读不到新值的缺陷。
"""

import pathlib

ROOT = pathlib.Path("/home/z/my-project/aistudio-api")


def rw(p: pathlib.Path, old: str, new: str, label: str) -> None:
    s = p.read_text(encoding="utf-8")
    assert old in s, f"{label}: anchor not found"
    assert s.count(old) == 1, f"{label}: anchor not unique"
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    print(f"[ok] {label}")


# ══════════════════════════════════════════════════════════════════
# 1) outbound_keepalive.py：运行时覆盖层（含 allow_private 三元组）
# ══════════════════════════════════════════════════════════════════
ok = ROOT / "src/aistudio_api/infrastructure/outbound_keepalive.py"

rw(
    ok,
    '''def keepalive_private_targets_allowed() -> bool:
    """是否允许私网/环回保活目标（AISTUDIO_KEEPALIVE_ALLOW_PRIVATE，默认关）。"""
    return settings.keepalive_allow_private''',
    '''# 面板写入的运行时覆盖三元组（None = 未覆盖，走 env > yaml 默认链）。
# 与签名服务器切换的 runtime_override 同款语义：进程内立即生效、重启
# 后回退环境变量/配置文件默认值。写方为 routes_config 的 PUT 端点，
# 读方为 _current_config / keepalive_private_targets_allowed。
_RUNTIME_OVERRIDE: tuple[str, int, bool] | None = None


def keepalive_private_targets_allowed() -> bool:
    """是否允许私网/环回保活目标（运行时覆盖 > env > yaml 默认关）。"""
    if _RUNTIME_OVERRIDE is not None:
        return _RUNTIME_OVERRIDE[2]
    return settings.keepalive_allow_private


def set_keepalive_runtime_override(url: str, interval: int, allow_private: bool) -> None:
    """面板保存写入的运行时覆盖（立即生效，重启后回退 env/yaml）。"""
    global _RUNTIME_OVERRIDE
    _RUNTIME_OVERRIDE = (
        (url or "").strip(),
        max(_MIN_INTERVAL_SECONDS, int(interval)),
        bool(allow_private),
    )


def clear_keepalive_runtime_override() -> None:
    """清除运行时覆盖（回退 env > yaml）。"""
    global _RUNTIME_OVERRIDE
    _RUNTIME_OVERRIDE = None


def keepalive_runtime_override_active() -> bool:
    """运行时覆盖是否在生效（面板「覆盖环境变量中」状态用）。"""
    return _RUNTIME_OVERRIDE is not None''',
    "ok runtime override layer",
)

rw(
    ok,
    '''    def _current_config(self) -> tuple[str, int]:
        """读取当前生效配置（环境变量 > 内置默认）。"""
        url = (settings.keepalive_url or "").strip()
        interval = settings.keepalive_interval
        return url, int(interval)''',
    '''    def _current_config(self) -> tuple[str, int]:
        """读取当前生效配置（运行时覆盖 > 环境变量 > yaml 默认）。"""
        if _RUNTIME_OVERRIDE is not None:
            return _RUNTIME_OVERRIDE[0], _RUNTIME_OVERRIDE[1]
        url = (settings.keepalive_url or "").strip()
        interval = settings.keepalive_interval
        return url, int(interval)''',
    "ok _current_config override first",
)

# ══════════════════════════════════════════════════════════════════
# 2) routes_config.py：env 注入预填 + 覆盖语义
# ══════════════════════════════════════════════════════════════════
rc = ROOT / "src/aistudio_api/api/routes_config.py"

rw(
    rc,
    '''def _keepalive_env_override() -> dict[str, str]:
    """环境变量覆盖展示（AISTUDIO_KEEPALIVE_* 存在时面板只读提示）。"""
    overrides: dict[str, str] = {}
    if os.getenv("AISTUDIO_KEEPALIVE_URL", "").strip():
        overrides["url"] = "AISTUDIO_KEEPALIVE_URL"
    if os.getenv("AISTUDIO_KEEPALIVE_INTERVAL", "").strip():
        overrides["interval"] = "AISTUDIO_KEEPALIVE_INTERVAL"
    if os.getenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", "").strip():
        overrides["allow_private"] = "AISTUDIO_KEEPALIVE_ALLOW_PRIVATE"
    return overrides


def _keepalive_payload() -> dict[str, object]:
    """组装保活卡片数据：yaml 设置 + env 覆盖 + 服务运行态。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_keepalive_settings,
    )
    from aistudio_api.infrastructure.outbound_keepalive import (
        outbound_keepalive_service,
    )

    yaml_settings = get_keepalive_settings()
    service = outbound_keepalive_service
    return {
        **yaml_settings,
        "running": service.is_running,
        "env_overrides": _keepalive_env_override(),
        "effective_url": (settings.keepalive_url or "").strip(),
        "effective_interval": int(settings.keepalive_interval),
        "effective_allow_private": bool(settings.keepalive_allow_private),
    }''',
    '''def _keepalive_env_override() -> dict[str, str]:
    """环境变量注入展示（AISTUDIO_KEEPALIVE_* 非空时面板标注「环境变量注入」）。"""
    overrides: dict[str, str] = {}
    if os.getenv("AISTUDIO_KEEPALIVE_URL", "").strip():
        overrides["url"] = "AISTUDIO_KEEPALIVE_URL"
    if os.getenv("AISTUDIO_KEEPALIVE_INTERVAL", "").strip():
        overrides["interval"] = "AISTUDIO_KEEPALIVE_INTERVAL"
    if os.getenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE", "").strip():
        overrides["allow_private"] = "AISTUDIO_KEEPALIVE_ALLOW_PRIVATE"
    return overrides


def _keepalive_env_prefill() -> dict[str, object]:
    """按 Settings 同口径解析环境变量当前值（表单预填用）。

    解析口径与 config.Settings 字段构造一致：URL 原样 strip、间隔
    int 失败回退 60、布尔按 not in ("0","false","False") 判真。
    """
    raw_interval = os.getenv("AISTUDIO_KEEPALIVE_INTERVAL")
    try:
        interval = int(raw_interval.strip()) if raw_interval and raw_interval.strip() else 60
    except ValueError:
        interval = 60
    allow_private = os.getenv("AISTUDIO_KEEPALIVE_ALLOW_PRIVATE")
    return {
        "url": os.getenv("AISTUDIO_KEEPALIVE_URL", "").strip(),
        "interval": interval,
        "allow_private": allow_private is not None
        and allow_private not in ("0", "false", "False"),
    }


def _keepalive_payload() -> dict[str, object]:
    """组装保活卡片数据：表单值（env 注入优先预填）+ 注入标注 + 运行态。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_keepalive_settings,
    )
    from aistudio_api.infrastructure.outbound_keepalive import (
        keepalive_runtime_override_active,
        outbound_keepalive_service,
    )

    yaml_settings = get_keepalive_settings()
    env_overrides = _keepalive_env_override()
    env_prefill = _keepalive_env_prefill()
    # 表单初值：环境变量注入的字段预填 env 值（面板保存即覆盖），
    # 未注入字段保持 yaml 值。
    form = {
        "url": str(env_prefill["url"]) if "url" in env_overrides else str(yaml_settings["url"]),
        "interval": (
            int(env_prefill["interval"])  # type: ignore[arg-type]
            if "interval" in env_overrides
            else int(yaml_settings["interval"])  # type: ignore[arg-type]
        ),
        "allow_private": (
            bool(env_prefill["allow_private"]) if "allow_private" in env_overrides else bool(yaml_settings["allow_private"])
        ),
    }
    service = outbound_keepalive_service
    return {
        **form,
        "yaml_url": str(yaml_settings["url"]),
        "yaml_interval": int(yaml_settings["interval"]),  # type: ignore[arg-type]
        "yaml_allow_private": bool(yaml_settings["allow_private"]),
        "running": service.is_running,
        "env_overrides": env_overrides,
        "runtime_override_active": keepalive_runtime_override_active(),
    }''',
    "rc payload rebuild",
)

rw(
    rc,
    """    update_keepalive_settings(
        url=url,
        interval=req.interval,
        allow_private=req.allow_private,
    )
    # 配置已写盘，按新配置重启保活任务（URL 清空时即为停止）。
    await outbound_keepalive_service.restart()
    logger.info(
        "出站保活配置已更新: url=%s, interval=%ds, allow_private=%s",
        url or "(空，停止保活)",
        req.interval,
        req.allow_private,
    )
    return _keepalive_payload()""",
    """    update_keepalive_settings(
        url=url,
        interval=req.interval,
        allow_private=req.allow_private,
    )
    # 运行时覆盖 > 环境变量：面板保存立即生效（含覆盖环境变量注入值），
    # 重启后回退环境变量/配置文件。之后按新配置重启保活任务（URL 清空
    # 时即为停止）。
    set_keepalive_runtime_override(
        url=url,
        interval=req.interval,
        allow_private=req.allow_private,
    )
    await outbound_keepalive_service.restart()
    logger.info(
        "出站保活配置已更新: url=%s, interval=%ds, allow_private=%s",
        url or "(空，停止保活)",
        req.interval,
        req.allow_private,
    )
    return _keepalive_payload()""",
    "rc PUT override",
)

rw(
    rc,
    """    from aistudio_api.infrastructure.outbound_keepalive import (
        outbound_keepalive_service,
        validate_keepalive_url,
    )""",
    """    from aistudio_api.infrastructure.outbound_keepalive import (
        outbound_keepalive_service,
        set_keepalive_runtime_override,
        validate_keepalive_url,
    )""",
    "rc PUT import",
)

print("batch-A done")
