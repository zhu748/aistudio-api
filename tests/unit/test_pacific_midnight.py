"""Quota reset countdowns follow elapsed time across 23/25-hour Pacific days."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from aistudio_api.application import account_rotator as module


def _load_la_zone() -> ZoneInfo | None:
    """北美太平洋时区对象；宿主缺 IANA 数据库（Windows/精简镜像无 tzdata）时返回 None。

    项目运行时对此是健壮的（``_pacific_zone`` 失败即走纯算术 DST 回退），
    但本文件的断言需要与 ZoneInfo 逐秒对齐，故缺库时跳过 zoneinfo 分支。
    """
    try:
        return ZoneInfo("America/Los_Angeles")
    except Exception:
        return None


_LA_ZONE = _load_la_zone()
_requires_tzdata = pytest.mark.skipif(
    _LA_ZONE is None, reason="宿主缺少 IANA 时区数据库（需安装 tzdata）"
)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-03-08T08:30:00", "2026-03-09T07:00:00"),
        ("2026-11-01T07:30:00", "2026-11-02T08:00:00"),
        ("2026-07-01T12:00:00", "2026-07-02T07:00:00"),
        ("2026-12-01T12:00:00", "2026-12-02T08:00:00"),
    ],
)
def test_midnight_countdown_matches_utc_elapsed_time(monkeypatch, fallback, start, end):
    if not fallback and _LA_ZONE is None:
        pytest.skip("宿主缺少 IANA 时区数据库（需安装 tzdata）")
    now = datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp()
    midnight = datetime.fromisoformat(end).replace(tzinfo=UTC).timestamp()
    monkeypatch.setattr(module.time, "time", lambda: now)
    monkeypatch.setattr(
        module,
        "_pacific_zone",
        lambda: None if fallback else ZoneInfo("America/Los_Angeles"),
    )
    assert module.get_seconds_until_pacific_midnight() == midnight - now


# ───────────── 日期键缓存（性能优化：选路/配额打分每请求多次调用） ─────────────


def _pin_la_zone(monkeypatch) -> None:
    if _LA_ZONE is None:
        pytest.skip("宿主缺少 IANA 时区数据库（需安装 tzdata）")
    monkeypatch.setattr(module, "_pacific_date_key_cache", None)
    monkeypatch.setattr(
        module, "_pacific_zone", lambda: ZoneInfo("America/Los_Angeles")
    )


def test_date_key_cache_reuses_within_day_and_refreshes_after_midnight(monkeypatch):
    """默认路径缓存：同一太平洋日内仅一次时区换算，跨午夜自动重算。"""
    _pin_la_zone(monkeypatch)
    calls: list[float | None] = []
    real_pacific_datetime = module._pacific_datetime

    def counting(ts: float | None = None):
        calls.append(ts)
        return real_pacific_datetime(ts)

    monkeypatch.setattr(module, "_pacific_datetime", counting)

    day1 = datetime.fromisoformat("2026-03-10T12:00:00").replace(tzinfo=UTC).timestamp()
    monkeypatch.setattr(module.time, "time", lambda: day1)
    assert module.get_pacific_date_key() == "2026-03-10"
    assert len(calls) == 1
    # 同日内重复调用：纯缓存命中，不再触发换算
    assert module.get_pacific_date_key() == "2026-03-10"
    assert module.get_pacific_date_key() == "2026-03-10"
    assert len(calls) == 1
    # 越过太平洋午夜（2026-03-11 07:30 UTC = 当地 00:30）：首个调用重算
    day2 = datetime.fromisoformat("2026-03-11T07:30:00").replace(tzinfo=UTC).timestamp()
    monkeypatch.setattr(module.time, "time", lambda: day2)
    assert module.get_pacific_date_key() == "2026-03-11"
    assert len(calls) == 2


def test_date_key_explicit_ts_bypasses_cache(monkeypatch):
    """显式 ts（测试/回填历史）精确计算：既不读也不写默认路径缓存。"""
    _pin_la_zone(monkeypatch)
    old = datetime.fromisoformat("2026-03-09T12:00:00").replace(tzinfo=UTC).timestamp()
    monkeypatch.setattr(module.time, "time", lambda: old + 86400)
    assert module.get_pacific_date_key() == "2026-03-10"
    # 历史时间戳应返回真实历史键，而非缓存中的"今天"
    assert module.get_pacific_date_key(old) == "2026-03-09"
    # 显式调用未污染缓存：默认路径仍是 2026-03-10
    assert module.get_pacific_date_key() == "2026-03-10"


def test_date_key_cache_handles_dst_transition(monkeypatch):
    """DST 切换日：缓存阈值按 UTC 时间戳差计算，不因墙钟小时偏差多锁/少锁一天。"""
    _pin_la_zone(monkeypatch)
    # 2026-11-01 07:30 UTC = 太平洋 00:30（DST 回拨日凌晨，PDT）
    dst_day = (
        datetime.fromisoformat("2026-11-01T07:30:00").replace(tzinfo=UTC).timestamp()
    )
    monkeypatch.setattr(module.time, "time", lambda: dst_day)
    assert module.get_pacific_date_key() == "2026-11-01"
    # 次日当地 00:30 = 2026-11-02 08:30 UTC（DST 后 UTC-8）→ 自动翻到新键
    next_day = (
        datetime.fromisoformat("2026-11-02T08:30:00").replace(tzinfo=UTC).timestamp()
    )
    monkeypatch.setattr(module.time, "time", lambda: next_day)
    assert module.get_pacific_date_key() == "2026-11-02"
