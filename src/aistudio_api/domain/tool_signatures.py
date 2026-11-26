"""Preserve native tool history with exact signature restoration or a skip sentinel.

Following AIStudio2API's MakerSuite wire implementation, missing signatures
use the literal ``skip_thought_signature_validator`` in Part slot 14. This
special value is not a cryptographic signature or a Base64 byte string.
Missing signatures must never cause tool calls or their results to be deleted.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

SKIP_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


def valid_thought_signature(signature: object) -> bool:
    """Accept an encoded signature or the protocol's explicit skip literal."""
    if signature == SKIP_THOUGHT_SIGNATURE:
        return True
    if not isinstance(signature, str) or not signature:
        return False
    try:
        base64.b64decode(signature, validate=True)
        return True
    except (ValueError, UnicodeEncodeError):
        pass
    # URL-safe（-/_）或缺 padding 变体：换写标准字母表并补齐 padding 后再
    # 校验。与 wire 编码侧 _normalize_standard_base64 的容忍语义对齐——否则
    # 出现「编码层愿意归一化发送、校验层却判无效丢弃」的自相矛盾，客户端
    # 原样回传的签名会被静默丢弃。len%4==1 不可能是合法 Base64，直接拒绝。
    if len(signature) % 4 == 1:
        return False
    normalized = signature.replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        base64.b64decode(normalized, validate=True)
    except (ValueError, UnicodeEncodeError):
        return False
    return True


def valid_thought_signature_or_none(signature: object) -> str | None:
    """签名合法则原样返回，否则返回 None（丢弃签名字段本身）。"""
    if valid_thought_signature(signature):
        assert isinstance(signature, str)
        return signature
    return None


def tool_history_text(
    kind: str, name: str, call_id: str | None, payload: object
) -> str:
    """Retain an orphan tool result as text without dropping its contents."""
    return json.dumps(
        {kind: {"name": name, "id": call_id, "payload": payload}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class TurnSignatureProof:
    """One observed complete turn: only digests and per-call signatures."""

    state: str
    cohort: str | None
    thought: str | None
    owners: tuple[str, ...]
    signatures: tuple[str | None, ...] = field(repr=False)

    def as_record(self) -> tuple[object, ...]:
        return self.state, self.cohort, self.thought, self.owners, self.signatures


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _read_turn_proof(value: object) -> TurnSignatureProof | None:
    if not isinstance(value, list) or len(value) != 5:
        return None
    state, cohort, thought, owners, signatures = value
    if (
        not _is_digest(state)
        or (cohort is not None and not _is_digest(cohort))
        or (thought is not None and not _is_digest(thought))
        or not isinstance(owners, list)
        or not all(_is_digest(owner) for owner in owners)
        or not isinstance(signatures, list)
        or not all(
            sig is None
            or (sig != SKIP_THOUGHT_SIGNATURE and valid_thought_signature(sig))
            for sig in signatures
        )
    ):
        return None
    return TurnSignatureProof(state, cohort, thought, tuple(owners), tuple(signatures))


class ThoughtSignatureCache:
    """Bounded persistent cache with a separate replay-provenance namespace.

    Keys are hashed to avoid persisting prompts or tool arguments. Legacy
    records have no history scope and can never be promoted to replay records.
    """

    _MAX_ENTRIES = 2048
    _MAX_REPLAY_BYTES = 8 * 1024 * 1024
    # 记住签名后的落盘去抖：缓存最多 2048 条（数百 KB～MB 级 JSON），
    # 每次全量序列化 + 原子重写是同步 I/O，remember 跑在事件循环线程的
    # 热路径上（每个 tool_calls 帧一次），串行锁 + 重复落盘会把并发请求
    # 的停顿叠到数百 ms。窗口内多次 remember 只在首次落盘，其余置脏；
    # 进程退出时 atexit 兜底刷盘（窗口内丢的签名仅退化为 SKIP 字面量，
    # 非关键数据）。
    _SAVE_DEBOUNCE_SECONDS = 2.0
    _REPLAY_NAMESPACE = "by_scoped_turn_v2"
    _OLD_REPLAY_NAMESPACE = "by_scoped_call_v1"

    def __init__(self, path=None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._signatures: OrderedDict[str, str] = OrderedDict()
        # Proofs fit in their own byte budget; a None proof is a conflict
        # marker, not a miss. Older per-call rows can only withhold fallback.
        self._turns: OrderedDict[str, tuple[TurnSignatureProof | None, int]] = (
            OrderedDict()
        )
        self._old_scoped: OrderedDict[str, None] = OrderedDict()
        self._replay_bytes = 0
        self._loaded = False
        self._load_error_logged = False
        self._dirty = False
        self._last_save = 0.0
        atexit.register(self._atexit_flush)

    # -- 公共接口 ----------------------------------------------------------

    @staticmethod
    def _key(
        call_id: str | None,
        name: str | None,
        arguments: object,
        old_position: tuple[str, str, int] | None = None,
    ) -> str | None:
        if not call_id or not name:
            return None
        fields = [call_id, name, arguments if arguments is not None else {}]
        if old_position is not None:
            fields.extend(old_position)
        canonical = json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def remember(
        self, call_id: str | None, name: str | None, arguments: object, signature: str
    ) -> None:
        """Record legacy response metadata without replay provenance."""
        key = self._key(call_id, name, arguments)
        if (
            key is None
            or signature == SKIP_THOUGHT_SIGNATURE
            or not valid_thought_signature(signature)
        ):
            return
        with self._lock:
            self._ensure_loaded()
            self._signatures[key] = signature
            self._signatures.move_to_end(key)
            self._mark_dirty_locked()

    @staticmethod
    def _turn_key(scope: str, turn: str) -> str:
        return hashlib.sha256(json.dumps([scope, turn]).encode()).hexdigest()

    def remember_turn(self, scope: str, turn: str, proof: TurnSignatureProof) -> bool:
        """Record a complete cohort; False means conflicting observations."""
        key = self._turn_key(scope, turn)
        size = self._proof_size(proof)
        with self._lock:
            self._ensure_loaded()
            old = self._turns.get(key)
            if old is not None:
                self._turns.move_to_end(key)
                if old[0] == proof or old[0] is None:
                    return old[0] is not None
                self._replay_bytes -= old[1]
                size = self._proof_size(None)
                self._turns[key] = (None, size)
            elif size > self._MAX_REPLAY_BYTES:
                # Oversize records remain eligible for the bounded hot LRU.
                return True
            else:
                self._turns[key] = (proof, size)
            self._replay_bytes += size
            # A known conflict must not leave an older value usable on restart.
            self._mark_dirty_locked(force=old is not None)
            return old is None

    @staticmethod
    def _proof_size(proof: TurnSignatureProof | None) -> int:
        # Digests and valid Base64 signatures are ASCII. Include framing and
        # their key in the budget without serializing twice in the hot path.
        if proof is None:
            return 72
        return (
            290
            + 67 * len(proof.owners)
            + sum(5 if sig is None else len(sig) + 3 for sig in proof.signatures)
        )

    def _trim_locked(self) -> None:
        while self._turns and self._replay_bytes > self._MAX_REPLAY_BYTES:
            _, (_, size) = self._turns.popitem(last=False)
            self._replay_bytes -= size
        while (
            len(self._signatures) + len(self._old_scoped) + len(self._turns)
            > self._MAX_ENTRIES
        ):
            # Legacy metadata must not evict replay provenance.
            if self._signatures:
                self._signatures.popitem(last=False)
            elif self._old_scoped:
                self._old_scoped.popitem(last=False)
            else:
                _, (_, size) = self._turns.popitem(last=False)
                self._replay_bytes -= size

    def _mark_dirty_locked(self, *, force: bool = False) -> None:
        self._trim_locked()
        # Bind the data directory now, not after the request/test environment
        # disappears at atexit. _ensure_loaded has already resolved the path.
        self._dirty = True
        now = time.monotonic()
        if (
            force or now - self._last_save >= self._SAVE_DEBOUNCE_SECONDS
        ) and self._save_locked():
            self._last_save = now
            self._dirty = False

    def flush(self) -> None:
        """落盘窗口内累积的脏数据（优雅关停或退出钩子调用）。"""
        with self._lock:
            if self._dirty and self._save_locked():
                self._dirty = False
            self._last_save = time.monotonic()

    def _atexit_flush(self) -> None:
        """进程退出兑底：丢窗口内未落盘的脏数据。"""
        # 解释器关闭阶段（模块可能已半拆）：签名缓存非关键数据，静默放弃
        with contextlib.suppress(Exception):
            self.flush()

    def lookup(
        self, call_id: str | None, name: str | None, arguments: object
    ) -> str | None:
        """Inspect legacy, unscoped metadata; not suitable for replay."""
        key = self._key(call_id, name, arguments)
        if key is None:
            return None
        with self._lock:
            self._ensure_loaded()
            sig = self._signatures.get(key)
            if sig is not None:
                self._signatures.move_to_end(key)
            return sig

    def lookup_turn(
        self, scope: str, turn: str
    ) -> tuple[bool, TurnSignatureProof | None]:
        """Distinguish a miss from withheld, conflicting turn evidence."""
        key = self._turn_key(scope, turn)
        with self._lock:
            self._ensure_loaded()
            record = self._turns.get(key)
            if record is None:
                return False, None
            self._turns.move_to_end(key)
            return True, record[0]

    def has_old_observations(self) -> bool:
        with self._lock:
            self._ensure_loaded()
            return bool(self._old_scoped)

    def saw_old_call(
        self,
        scope: str,
        turn: str,
        ordinal: int,
        call_id: str | None,
        name: str,
        arguments: object,
    ) -> bool:
        """A v1 row cannot be replayed or treated as a new fallback case."""
        key = self._key(call_id, name, arguments, (scope, turn, ordinal))
        with self._lock:
            self._ensure_loaded()
            if key is None or key not in self._old_scoped:
                return False
            self._old_scoped.move_to_end(key)
            return True

    # -- 持久化 ------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._resolve_path()
        if path is None:
            return
        try:
            with Path(path).open(encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("by_call", {}) if isinstance(data, dict) else {}
            if isinstance(entries, dict):
                for key, value in entries.items():
                    if (
                        isinstance(key, str)
                        and isinstance(value, str)
                        and value != SKIP_THOUGHT_SIGNATURE
                        and valid_thought_signature(value)
                    ):
                        self._signatures[key] = value
                        if len(self._signatures) > self._MAX_ENTRIES:
                            self._signatures.popitem(last=False)
            if not isinstance(data, dict):
                return
            old_scoped = data.get(self._OLD_REPLAY_NAMESPACE)
            if isinstance(old_scoped, dict):
                for key in old_scoped:
                    if isinstance(key, str) and _is_digest(key):
                        self._old_scoped[key] = None
                        self._trim_locked()
            scoped = data.get(self._REPLAY_NAMESPACE)
            if isinstance(scoped, dict):
                for key, value in scoped.items():
                    if not isinstance(key, str) or not _is_digest(key):
                        continue
                    # A malformed observed row can never authorize replay.
                    proof = _read_turn_proof(value)
                    size = self._proof_size(proof)
                    if size > self._MAX_REPLAY_BYTES:
                        continue
                    self._turns[key] = (proof, size)
                    self._replay_bytes += size
                    self._trim_locked()
        except (OSError, ValueError):
            # 首次运行 / 文件损坏：从空缓存开始即可
            pass

    def _save_locked(self) -> bool:
        """落盘当前缓存，返回是否成功（失败时调用方须保留 _dirty 标记，
        否则退出兑底 flush 会因「已落盘」误判跳过补写——瞬时磁盘故障
        窗口内的签名将永久丢失）。"""
        path = self._resolve_path()
        if path is None:
            return False
        try:
            from aistudio_api.infrastructure.utils.common import atomic_write_json

            atomic_write_json(
                path,
                {
                    "by_call": dict(self._signatures),
                    self._OLD_REPLAY_NAMESPACE: dict(self._old_scoped),
                    self._REPLAY_NAMESPACE: {
                        key: proof.as_record() if proof is not None else None
                        for key, (proof, _) in self._turns.items()
                    },
                },
                private=True,
                compact=True,
            )
            return True
        except (OSError, ValueError) as exc:
            if not self._load_error_logged:
                self._load_error_logged = True
                # 持久化失败不阻断请求路径，仅记录一次
                try:
                    from aistudio_api.infrastructure.utils.logger import get_logger

                    get_logger("tool-signatures").warning(
                        "签名缓存持久化失败（继续使用内存缓存）: %s", exc
                    )
                except Exception:
                    pass
            return False

    def _resolve_path(self) -> Path | None:
        if self._path is not None:
            return self._path
        # 延迟解析：domain 层不静态依赖 infrastructure/config（避免环）
        try:
            from aistudio_api.config import resolve_data_dir

            path = resolve_data_dir() / "tool_signature_cache.json"
            self._path = path
            return path
        except Exception:
            return None


_cache_instance: ThoughtSignatureCache | None = None
_cache_instance_lock = threading.Lock()


def get_signature_cache() -> ThoughtSignatureCache:
    """进程级单例（延迟初始化，路径由 config 数据目录解析）。"""
    global _cache_instance
    with _cache_instance_lock:
        if _cache_instance is None:
            _cache_instance = ThoughtSignatureCache()
        return _cache_instance


def remember_function_signature(
    call_id: str | None, name: str | None, arguments: object, signature: object
) -> None:
    """缓存模型下发的真实签名（应用层响应路径统一调用）。"""
    if valid_thought_signature(signature):
        assert isinstance(signature, str)
        get_signature_cache().remember(call_id, name, arguments, signature)
