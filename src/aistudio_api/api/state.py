"""Shared API runtime state."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aistudio_api.infrastructure.gateway.client import AIStudioClient


@dataclass
class RuntimeState:
    client: AIStudioClient | None = None
    busy_lock: asyncio.Lock | None = None
    camoufox_port: int = 9222
    snapshot_cache: object | None = None  # SnapshotCache 实例
    account_service: object | None = None  # AccountService 实例
    rotator: object | None = None  # AccountRotator 实例
    model_stats: dict[str, dict] = field(
        default_factory=lambda: defaultdict(
            lambda: {
                "requests": 0,
                "success": 0,
                "rate_limited": 0,
                "errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "last_used": None,
            }
        )
    )

    def record(self, model: str, event: str, usage: dict | None = None):
        stats = self.model_stats[model]
        stats["requests"] += 1
        stats[event] += 1
        stats["last_used"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        if usage and event == "success":
            stats["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
            stats["completion_tokens"] += usage.get("completion_tokens", 0) or 0
            stats["total_tokens"] += usage.get("total_tokens", 0) or 0


runtime_state = RuntimeState()

