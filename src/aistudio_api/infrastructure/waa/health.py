"""WAA 签名器健康跟踪与熔断（signer-grade attestation 质量降级检测）。

## 背景（2026-11-12 逆向实测证据链，详见 docs/CAPTURE_ALIGNMENT_2026-11.md §9）

403「The caller does not have permission」存在四种已证语义：

1. proof 失效/摘要失配 —— 重签有效；
2. 远程签名服务单次使用/质量抖动 —— 重签有效；
3. 账号级生成限流 —— 同一账号所有 proof（含浏览器级）全 403，
   轻量端点（CountTokens 等）仍 200；
4. **签名器 attestation 质量降级（本模块针对的新语义）** —— 签名服务
   mint 成功（ok:true）但产出的 proof 被上游系统性拒绝。实测案例：
   spinsnow 全节点使用过期挑战构建（challenge_version 3109903533，
   而浏览器当日为 3153048796；环境载荷 1015B vs 1352-1436B），
   同期同 Cookie 同代理下浏览器级 proof 重放 100% 200、
   mint proof 100% 403。

语义 3 与 4 的单请求内表现完全相同（403 且轻量端点 200），无法在
请求内区分；两者的可观测差异在**跨账号维度**：

- 语义 3：仅受影响账号 403，切换账号立即恢复；
- 语义 4：所有账号的新铸 proof 全 403（签名器是公共依赖）。

另注（2026-11-12 服务 E2E）：语义 4 的上游拒绝存在概率性 —— 同一枚
劣化 proof 第 3 次重放实测恢复 200，403 阶梯保留的前两跳免费重放
具有真实恢复价值，故熔断仅在 count≥3（重签/切号阶段）短路。

因此熔断判据采用「跨账号关联拒绝」：滑动窗口内 ≥N 次新铸 proof
被拒且涉及 ≥2 个不同账号，即判签名器降级并进入冷却。降级期间：

- 403 阶梯跳过重签与切号（对语义 4 二者均无效且消耗账号池）；
- 首个 403 不再对账号记 auth_error 惩罚（避免无辜账号降权）；
- 任何一次成功（proof 被上游接受）立即解除降级（半开探测语义）。
"""

from __future__ import annotations

import os
import time
from collections import deque
from typing import Any

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("waa.health")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


class SignerHealthTracker:
    """滑动窗口 + 冷却式熔断器，跟踪「新铸 proof 被上游 403 拒绝」的跨账号关联。

    判据（全部可通过环境变量调节）：
    - ``AISTUDIO_SIGNER_CB_MIN_REJECTS``（默认 3）：窗口内拒绝次数阈值；
    - ``AISTUDIO_SIGNER_CB_MIN_ACCOUNTS``（默认 2）：涉及不同账号数阈值
      （单账号部署永不熔断 —— 无法与语义 3 区分，保守放行全阶梯）；
    - ``AISTUDIO_SIGNER_CB_WINDOW_S``（默认 300）：滑动窗口秒数；
    - ``AISTUDIO_SIGNER_CB_COOLDOWN_S``（默认 60）：熔断冷却秒数。

    线程安全说明：FastAPI 单进程事件循环内调用，无需锁；计数路径
    均为内存操作，无 IO。
    """

    def __init__(
        self,
        *,
        min_rejects: int = 3,
        min_accounts: int = 2,
        window_seconds: float = 300.0,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.min_rejects = max(1, min_rejects)
        self.min_accounts = max(1, min_accounts)
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        # (monotonic_ts, account_id) —— 只保留拒绝事件；成功事件直接清窗
        self._rejects: deque[tuple[float, str | None]] = deque()
        self._degraded_until: float = 0.0
        self._degraded_at: float | None = None
        self._last_trip_accounts: tuple[int, int] | None = None

    # ------------------------------------------------------------------ 状态

    def is_degraded(self) -> bool:
        """当前是否处于熔断降级（冷却期内）。"""
        return time.monotonic() < self._degraded_until

    def cooldown_remaining(self) -> float:
        """熔断冷却剩余秒数（未降级为 0）。"""
        return max(0.0, self._degraded_until - time.monotonic())

    def state_summary(self) -> dict[str, Any]:
        """供管理端点 / 日志输出的状态快照。

        返回值形状为运行时诊断快照（嵌套结构用于 JSON 序列化），
        声明为 dict[str, Any] 以便调用方直接索引嵌套字段。
        """
        now = time.monotonic()
        recent = [
            (ts, acc) for ts, acc in self._rejects if now - ts <= self.window_seconds
        ]
        accounts = {acc for _, acc in recent if acc}
        return {
            "degraded": self.is_degraded(),
            "cooldownRemainingSec": round(self.cooldown_remaining(), 1),
            "recentRejects": len(recent),
            "recentAccounts": len(accounts),
            "tripThreshold": {
                "rejects": self.min_rejects,
                "accounts": self.min_accounts,
                "windowSec": self.window_seconds,
                "cooldownSec": self.cooldown_seconds,
            },
            "lastTrip": (
                {
                    "accountsInvolved": self._last_trip_accounts[0],
                    "rejects": self._last_trip_accounts[1],
                    "ageSec": round(now - self._degraded_at, 1),
                }
                if self._degraded_at is not None and self._last_trip_accounts
                else None
            ),
        }

    # ------------------------------------------------------------------ 事件

    def record_proof_rejected(self, account_id: str | None) -> None:
        """记一次「签名器产出的 proof 被上游 403 拒绝」。

        由 Auth403RetryManager.handle_403 在每次 403 时调用（携带当次
        实际服务账号）。注意语义 1/2/3 也会触发本调用 —— 跨账号判据
        负责把它们与语义 4 区分开。
        """
        now = time.monotonic()
        self._prune(now)
        self._rejects.append((now, account_id))
        if self.is_degraded():
            return  # 已在熔断中，无需重复判定

        accounts = {acc for _, acc in self._rejects if acc}
        if (
            len(self._rejects) >= self.min_rejects
            and len(accounts) >= self.min_accounts
        ):
            self._degraded_until = now + self.cooldown_seconds
            self._degraded_at = now
            self._last_trip_accounts = (len(accounts), len(self._rejects))
            logger.error(
                "WAA 签名器疑似降级（熔断 %ss）：窗口内 %d 次新铸 proof 被拒，"
                "涉及 %d 个账号。账号与轻量端点可能仍健康，403 根因大概率在"
                "签名服务（如远程 VM 挑战过期）。期间跳过重签/切号，成功即解除。",
                self.cooldown_seconds,
                len(self._rejects),
                len(accounts),
            )

    def record_proof_accepted(self, account_id: str | None = None) -> None:
        """记一次 proof 被上游接受（成功生成），立即解除熔断。

        半开语义：冷却期内哪怕一次成功也证明签名器可用（或抖动恢复），
        清空窗口并解除降级，避免误杀。
        """
        was_degraded = self.is_degraded()
        self._rejects.clear()
        self._degraded_until = 0.0
        if was_degraded:
            logger.info(
                "WAA 签名器熔断解除（proof 被上游接受）: account=%s", account_id
            )

    # ------------------------------------------------------------------ 内部

    def _prune(self, now: float) -> None:
        while self._rejects and now - self._rejects[0][0] > self.window_seconds:
            self._rejects.popleft()


_singleton: SignerHealthTracker | None = None


def get_signer_health() -> SignerHealthTracker:
    """进程级单例（事件循环内共享，跨请求聚合跨账号信号）。"""
    global _singleton
    if _singleton is None:
        _singleton = SignerHealthTracker(
            min_rejects=_env_int("AISTUDIO_SIGNER_CB_MIN_REJECTS", 3),
            min_accounts=_env_int("AISTUDIO_SIGNER_CB_MIN_ACCOUNTS", 2),
            window_seconds=_env_float("AISTUDIO_SIGNER_CB_WINDOW_S", 300.0),
            cooldown_seconds=_env_float("AISTUDIO_SIGNER_CB_COOLDOWN_S", 60.0),
        )
    return _singleton


def reset_signer_health_for_tests() -> None:
    """测试辅助：重置单例（单元测试隔离用）。"""
    global _singleton
    _singleton = None
