"""Shared API runtime state."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aistudio_api.config import resolve_stats_file, settings
from aistudio_api.infrastructure.request_context import record_result
from aistudio_api.infrastructure.utils.common import atomic_write_json

if TYPE_CHECKING:
    from aistudio_api.application.account_rotator import AccountRotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("state")


def _resolve_stats_timezone() -> tzinfo:
    """统计时间戳使用的时区：优先 ``settings.timezone``，不可用时退回本地时区。"""
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        local = datetime.now().astimezone().tzinfo
        return local or UTC


_STATS_TZ = _resolve_stats_timezone()


@dataclass
class ModelStatsItem:
    requests: int = 0
    success: int = 0
    rate_limited: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    finish_reasons: dict[str, int] = field(default_factory=dict)
    last_used: str | None = None


@dataclass
class RuntimeState:
    client: AIStudioClient | None = None
    account_service: AccountService | None = None
    rotator: AccountRotator | None = None
    model_stats: dict[str, ModelStatsItem] = field(
        default_factory=lambda: defaultdict(ModelStatsItem)
    )
    _last_saved_time: float = field(default=0.0, repr=False)
    _save_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.load_stats()

    def load_stats(self) -> None:
        """从持久化文件加载历史调用统计。"""
        if not settings.persist_stats:
            return
        stats_path = resolve_stats_file()
        if not stats_path.is_file():
            return
        try:
            raw = stats_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            logger.warning("从 %s 读取模型统计失败: %s", stats_path, e)
            return
        if not isinstance(data, dict):
            return
        for model, item in data.items():
            if not isinstance(item, dict):
                continue
            try:
                raw_finish = item.get("finish_reasons")
                finish_reasons = (
                    {str(k): int(str(v)) for k, v in raw_finish.items()}
                    if isinstance(raw_finish, dict)
                    else {}
                )
                self.model_stats[model] = ModelStatsItem(
                    requests=int(item.get("requests", 0)),
                    success=int(item.get("success", 0)),
                    rate_limited=int(item.get("rate_limited", 0)),
                    errors=int(item.get("errors", 0)),
                    prompt_tokens=int(item.get("prompt_tokens", 0)),
                    completion_tokens=int(item.get("completion_tokens", 0)),
                    reasoning_tokens=int(item.get("reasoning_tokens", 0)),
                    cached_tokens=int(item.get("cached_tokens", 0)),
                    total_tokens=int(item.get("total_tokens", 0)),
                    finish_reasons=finish_reasons,
                    last_used=str(item["last_used"]) if item.get("last_used") else None,
                )
            except Exception as e:
                # 单个模型条目损坏不应连累其余模型的统计加载
                logger.warning("模型统计条目 %s 损坏，已跳过: %s", model, e)

    def save_stats(self) -> None:
        """将模型调用与 Token 统计持久化到文件。"""
        self._persist_stats(
            {model: asdict(item) for model, item in self.model_stats.items()}
        )

    def _persist_stats(self, payload: Mapping[str, object]) -> None:
        if not settings.persist_stats:
            return
        stats_path = resolve_stats_file()
        try:
            atomic_write_json(stats_path, payload)
            self._last_saved_time = time.time()
        except Exception as e:
            logger.warning("持久化模型统计到 %s 失败: %s", stats_path, e)

    def _schedule_debounced_save(self, delay: float = 2.0) -> None:
        if not settings.persist_stats:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无事件循环（脚本 / 同步测试）时退化为同步写盘
            self.save_stats()
            return

        task = self._save_task
        if task is not None and not task.done():
            return

        async def _flush() -> None:
            if time.time() - self._last_saved_time < 5.0:
                await asyncio.sleep(delay)
            while True:
                # 落盘是阻塞 I/O，放到线程池执行，避免阻塞事件循环。
                payload = {
                    model: asdict(item) for model, item in self.model_stats.items()
                }
                await asyncio.to_thread(self._persist_stats, payload)
                current = {
                    model: asdict(item) for model, item in self.model_stats.items()
                }
                if payload == current:
                    return
                # 新请求在文件写入期间发生时，合并到下一轮快照中。
                await asyncio.sleep(delay)

        self._save_task = loop.create_task(_flush())

    def record(
        self,
        model: str,
        result: str,
        usage: dict[str, object] | None = None,
        finish_reason: str | None = None,
    ) -> None:
        stats = self.model_stats[model]
        stats.requests += 1
        stats.last_used = datetime.now(_STATS_TZ).strftime("%Y-%m-%d %H:%M:%S")

        if result == "success":
            stats.success += 1
        elif result == "rate_limited":
            stats.rate_limited += 1
        elif result in ("error", "errors"):
            stats.errors += 1

        record_result(
            "error" if result == "errors" else result,
            usage=usage,
            finish_reason=finish_reason or "",
        )

        if finish_reason:
            clean_fr = str(finish_reason).strip().upper()
            stats.finish_reasons[clean_fr] = stats.finish_reasons.get(clean_fr, 0) + 1

        if usage:
            pt = int(str(usage.get("prompt_tokens") or 0))
            ct = int(str(usage.get("completion_tokens") or 0))
            tt = int(str(usage.get("total_tokens") or 0))
            details = usage.get("completion_tokens_details")
            rt = 0
            if isinstance(details, dict):
                rt = int(str(details.get("reasoning_tokens") or 0))
            cached = int(str(usage.get("cached_tokens") or 0))

            stats.prompt_tokens += pt
            stats.completion_tokens += ct
            stats.reasoning_tokens += rt
            stats.cached_tokens += cached
            stats.total_tokens += tt if tt else (pt + ct)

        # 统计只用于面板展示，不属于选路状态；429/错误在重试链里可能连续发生，
        # 每次都同步全量落盘会在事件循环里堆积写放大，统一走防抖合并写入。
        self._schedule_debounced_save()


runtime_state = RuntimeState()
