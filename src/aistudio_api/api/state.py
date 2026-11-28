"""Shared API runtime state."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
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

# 模型统计条目上限：model 键来自请求路径（normalize 只做去前缀/小写，
# 不校验模型清单），任意已鉴权调用方都能用伪造模型名让每次失败请求
# 永久登记一个新键（内存 + stats.json 双重无界增长）。超限后已跟踪
# 模型继续计数，新键不再登记；请求上下文计量（record_result）不受影响。
_MAX_TRACKED_MODELS = 1000


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
    # model_stats 变更计数（record() 递增）：防抖 _flush 用它判断写入期间
    # 是否有新变更，替代每轮第二份 asdict 全量快照 + 深比较
    _stats_version: int = field(default=0, repr=False)
    # 保护 model_stats 与内部 ModelStatsItem：事件循环线程写（record/load），
    # 工作线程读（/stats asyncio.to_thread(stats_response)）。
    _stats_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

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
        with self._stats_lock:
            for model, item in data.items():
                if len(self.model_stats) >= _MAX_TRACKED_MODELS:
                    break
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
                        last_used=str(item["last_used"])
                        if item.get("last_used")
                        else None,
                    )
                except Exception as e:
                    # 单个模型条目损坏不应连累其余模型的统计加载
                    logger.warning("模型统计条目 %s 损坏，已跳过: %s", model, e)

    def snapshot_model_stats(self) -> dict[str, ModelStatsItem]:
        """线程安全地返回 model_stats 的隔离副本（供工作线程聚合 /stats 使用）。"""
        with self._stats_lock:
            return {
                model: replace(item, finish_reasons=dict(item.finish_reasons))
                for model, item in self.model_stats.items()
            }

    def save_stats(self) -> None:
        """将模型调用与 Token 统计持久化到文件。"""
        with self._stats_lock:
            payload = {model: asdict(item) for model, item in self.model_stats.items()}
        self._persist_stats(payload)

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
                # 写前记录脏版本号：此前用第二份 asdict + 深比较判断写入
                # 期间是否有新变更，模型数撑大后是每轮两次全量快照的
                # 周期性循环停顿；record() 里 +1 的整数计数等价且 O(1)。
                with self._stats_lock:
                    version_at_write = self._stats_version
                    payload = {
                        model: asdict(item) for model, item in self.model_stats.items()
                    }
                write = asyncio.create_task(
                    asyncio.to_thread(self._persist_stats, payload)
                )
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    # Cancelling to_thread does not stop its worker. Drain it
                    # before shutdown writes a newer synchronous snapshot.
                    await write
                    raise
                with self._stats_lock:
                    if self._stats_version == version_at_write:
                        return
                # 新请求在文件写入期间发生时，合并到下一轮快照中。
                await asyncio.sleep(delay)

        self._save_task = loop.create_task(_flush(), context=contextvars.Context())

    def record(
        self,
        model: str,
        result: str,
        usage: dict[str, object] | None = None,
        finish_reason: str | None = None,
    ) -> None:
        # 统计键归一化（去 models/ 前缀 + 小写）：成功路径记 normalized.model
        # （带前缀），错误路径可能记原始 model_path（不带前缀），同一模型
        # 会在 stats.json 中分裂成两条记录；面板 /stats 虽按 removeprefix
        # 展示，但持久化与 _MAX_TRACKED_MODELS 配额均按原始键计算。
        model = model.removeprefix("models/").lower()
        now_str = datetime.now(_STATS_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with self._stats_lock:
            if (
                model not in self.model_stats
                and len(self.model_stats) >= _MAX_TRACKED_MODELS
            ):
                # 统计表已满（键来自客户端可控的模型路径）：不再为新模型登记
                # 条目，防止恶意/异常请求无界撑大内存与 stats.json；请求上下文
                # 计量照常执行。
                capped = True
            else:
                capped = False
                stats = self.model_stats[model]
                self._stats_version += 1
                stats.requests += 1
                stats.last_used = now_str

                if result == "success":
                    stats.success += 1
                elif result == "rate_limited":
                    stats.rate_limited += 1
                elif result in ("error", "errors"):
                    stats.errors += 1

                if finish_reason:
                    clean_fr = str(finish_reason).strip().upper()
                    stats.finish_reasons[clean_fr] = (
                        stats.finish_reasons.get(clean_fr, 0) + 1
                    )

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

        record_result(
            "error" if result == "errors" else result,
            usage=usage,
            finish_reason=finish_reason or "",
        )
        if capped:
            return

        # 统计只用于面板展示，不属于选路状态；429/错误在重试链里可能连续发生，
        # 每次都同步全量落盘会在事件循环里堆积写放大，统一走防抖合并写入。
        self._schedule_debounced_save()


runtime_state = RuntimeState()
