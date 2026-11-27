"""Restore exact upstream model turns echoed by a compatible client.

Adapted from Mag1cFall/AIStudio2API's model_turns.go (MIT). The package's
THIRD_PARTY_NOTICES.md retains its license. Entries are scoped to the client,
upstream account, model and instructions, and live in bounded process memory.

上限按容器内存预算自适应（cgroup 限额 / AISTUDIO_MEMORY_BUDGET_MB）：
默认档 64MB/1000 条/单条 4MB（大内存部署维持历史取值）；≤1GB 档
32MB/500 条/2MB；≤512MB 档（Render 免费档等）12MB/300 条/1MB——小容器
上回放缓存与请求峰值（长代理会话正文 MB 级 ×多份拷贝）叠加曾直接把
RSS 顶过 OOMKiller 阈值。AISTUDIO_MODEL_TURN_CACHE_MB 显式覆盖总量，
条数/单条按比例折算（保留下限）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import repeat
from typing import TYPE_CHECKING

from aistudio_api.config import detect_memory_budget_mb
from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    TurnSignatureProof,
    get_signature_cache,
    valid_thought_signature_or_none,
)
from aistudio_api.infrastructure.signature_trace import get_signature_trace

from .wire_codec import AistudioWireCodec
from .wire_parser import parse_response_part, wire_size_estimate
from .wire_spec import PartIndex
from .wire_types import AistudioContent, AistudioPart, wire_function_name

if TYPE_CHECKING:
    from aistudio_api.domain.models import ResponsePart

_TOKEN_RE = re.compile(r"v1_[A-Za-z0-9+/=_-]{16,}")
_DEFAULT_MAX_TURN_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_CACHE_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_ENTRIES = 1000
_MAX_TURN_PARTS = 16384

# 媒体指纹分块尺寸：base64 字母表全 ASCII，分块 encode 的字节串拼接与
# 整串 encode 恒等；64KB 块让 MB 级媒体哈希的瞬时拷贝从"整份"降到常数。
_MEDIA_HASH_CHUNK = 65536


@lru_cache(maxsize=1)
def _turn_limits() -> tuple[int, int, int]:
    """(cache_bytes, max_entries, max_turn_bytes) 按内存预算/环境变量分档。"""
    override = os.getenv("AISTUDIO_MODEL_TURN_CACHE_MB")
    if override:
        try:
            megabytes = int(override)
        except ValueError:
            megabytes = 0
        if megabytes > 0:
            cache_bytes = megabytes * 1024 * 1024
            scale = cache_bytes / _DEFAULT_MAX_CACHE_BYTES
            return (
                cache_bytes,
                max(
                    64,
                    min(_DEFAULT_MAX_ENTRIES, int(_DEFAULT_MAX_ENTRIES * scale)),
                ),
                max(
                    256 * 1024,
                    min(_DEFAULT_MAX_TURN_BYTES, int(_DEFAULT_MAX_TURN_BYTES * scale)),
                ),
            )
    budget = detect_memory_budget_mb()
    if budget is None or budget > 1024:
        return (_DEFAULT_MAX_CACHE_BYTES, _DEFAULT_MAX_ENTRIES, _DEFAULT_MAX_TURN_BYTES)
    if budget <= 512:
        return (12 * 1024 * 1024, 300, 1024 * 1024)
    return (32 * 1024 * 1024, 500, 2 * 1024 * 1024)


def _hash_media_b64(data: str) -> str:
    """对 base64 媒体串做稳定指纹，分块 encode 避免整份拷贝。

    旧实现 ``sha256(data.encode())`` 在多模态代理会话里对每张历史图片
    （MB 级 base64）整体 encode 一次；``_digest`` 每请求走全对话，N 张
    图片 × M 轮次的瞬时拷贝是 512MB 容器上请求峰值的主要放大器之一。
    """
    digest = hashlib.sha256()
    for index in range(0, len(data), _MEDIA_HASH_CHUNK):
        digest.update(data[index : index + _MEDIA_HASH_CHUNK].encode("ascii"))
    return digest.hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _inline_identity(wire: list[object], media: tuple[str, str]) -> list[object]:
    mime, data = media
    inline = wire[PartIndex.INLINE_DATA]
    assert isinstance(inline, list)
    identity: list[object] = [mime, _hash_media_b64(data), *inline[2:]]
    while identity[-1] is None:
        identity.pop()
    return identity


@dataclass(frozen=True, slots=True)
class _ResultOwners:
    by_id: dict[str, tuple[int, str]]
    count: int


def _result_owners(parts: list[AistudioPart]) -> _ResultOwners:
    calls = [part.function_call for part in parts if part.function_call is not None]
    counts = Counter(call[2] for call in calls if len(call) > 2 and call[2])
    return _ResultOwners(
        {
            call[2]: (index, wire_function_name(call[0]))
            for index, call in enumerate(calls)
            if len(call) > 2 and counts[call[2]] == 1
        },
        len(calls),
    )


def _digest(
    previous: str,
    model: bool,
    parts: list[AistudioPart],
    owners: _ResultOwners | None = None,
    *,
    legacy_prefix: str | None = None,
) -> tuple[str, str | None]:
    items: list[list[object]] = []
    explicit_results: list[tuple[int, list[object]]] = []
    legacy_media: list[list[object]] = []
    text: list[str] = []

    def flush_text() -> None:
        if text:
            value = "".join(text)
            if model:
                value = value.strip()
            if value:
                items.append(["text", value])
            text.clear()

    for part in parts:
        # raw_wire 仅被只读检查（parse_response_part 不改写入参，签名位
        # 归零在浅拷贝 visible 上进行），无需 to_wire 的整树 deepcopy——
        # store() 对捕获轮的每个 part 深拷贝曾占摘要计算的大头。
        wire = part.raw_wire if part.raw_wire is not None else part.to_wire()
        parsed = parse_response_part(wire)
        if parsed.text and not parsed.thought:
            text.append(parsed.text)
        for kind, payload in (
            ("call", parsed.function_call),
            ("result", parsed.function_response),
        ):
            if payload is not None:
                flush_text()
                name = payload.get("name")
                item: list[object] = [
                    kind,
                    name,
                    payload.get("args", payload.get("arguments", {})),
                ]
                if kind == "result":
                    call_id = payload.get("call_id") or payload.get("id")
                    if isinstance(call_id, str) and call_id:
                        owner = (
                            owners.by_id.get(call_id) if owners is not None else None
                        )
                        if owner is not None and owner[1] == name:
                            item.append(["call", owner[0]])
                            explicit_results.append((owner[0], item))
                        else:
                            # Unknown or ambiguous IDs must not be dropped from
                            # identity or mapped by their name alone.
                            item.append(["unbound", call_id])
                items.append(item)
        if parsed.inline_data is not None:
            flush_text()
            media = ["media", *_inline_identity(wire, parsed.inline_data)]
            if legacy_prefix is not None and len(media) > 3:
                legacy_media.append(media)
            items.append(media)
        elif part.file_id or parsed.executable_code or parsed.code_execution_result:
            flush_text()
            items.append(
                [
                    "other",
                    part.file_id,
                    parsed.executable_code,
                    parsed.code_execution_result,
                ]
            )
        elif (
            not parsed.thought
            and not parsed.text
            and parsed.function_call is None
            and parsed.function_response is None
        ):
            # Signature-only carriers are intentionally invisible to matching.
            visible = list(wire)
            if len(visible) > PartIndex.THOUGHT_SIGNATURE:
                visible[PartIndex.THOUGHT_SIGNATURE] = None
            while visible and visible[-1] is None:
                visible.pop()
            if visible and visible != [None, ""]:
                flush_text()
                items.append(["opaque", visible])
    flush_text()
    if (
        owners is not None
        and 1 < len(items) == len(explicit_results) == owners.count
        and len({index for index, _ in explicit_results}) == owners.count
    ):
        # Completion order is immaterial only for a whole result-only batch
        # with a verified one-to-one ID mapping. The wire order is unchanged.
        items = [item for _, item in sorted(explicit_results)]
    current = _hash([previous, model, items])
    if legacy_prefix is None:
        return current, None
    if legacy_prefix == previous and not legacy_media:
        return current, current
    for media in legacy_media:
        del media[3:]
    return current, _hash([legacy_prefix, model, items])


def _tool_owner(payload: dict[str, object] | None) -> object:
    if payload is None:
        return None
    raw = payload.get("raw")
    return (
        payload.get("name"),
        payload.get("args", payload.get("arguments", {})),
        raw[3:] if isinstance(raw, list) and len(raw) > 3 else None,
    )


_STRUCTURED_SLOTS = frozenset(PartIndex) - {PartIndex.FILE_DATA}


def _carrier_payload(wire: list[object], part: ResponsePart) -> tuple[object, ...]:
    return (
        part.thought,
        part.text if part.text_present else None,
        _tool_owner(part.function_call),
        _tool_owner(part.function_response),
        _inline_identity(wire, part.inline_data) if part.inline_data else None,
        part.executable_code,
        part.code_execution_result,
        [
            (index, value)
            for index, value in enumerate(wire)
            if value is not None and index not in _STRUCTURED_SLOTS
        ],
    )


def _call_cohort(
    wires: list[list[object]], parsed: list[ResponsePart]
) -> tuple[str | None, tuple[str | None, ...]]:
    """Fingerprint every call's stable ID and exact carrier, in batch order."""
    members: list[tuple[str, tuple[object, ...]]] = []
    signatures: list[str | None] = []
    ids: set[str] = set()
    for wire, part in zip(wires, parsed, strict=True):
        if (call := part.function_call) is None:
            continue
        call_id = call.get("call_id")
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_id in ids
            or not isinstance(call.get("name"), str)
            or not isinstance(call.get("args"), dict)
        ):
            return None, ()
        ids.add(call_id)
        members.append((call_id, _carrier_payload(wire, part)))
        signature = valid_thought_signature_or_none(part.thought_signature)
        signatures.append(signature if signature != SKIP_THOUGHT_SIGNATURE else None)
    return _hash(members), tuple(signatures)


def _thought_digest(parts: Iterable[ResponsePart]) -> str | None:
    texts = [part.text for part in parts if part.thought]
    return _hash("".join(texts)) if texts else None


def _signed_owners(
    wires: Iterable[list[object]], parts: Iterable[ResponsePart]
) -> Iterator[str]:
    """Signed carriers, with stable offsets despite split unsigned text."""
    text_offset = thought_offset = call_ordinal = 0
    for wire, part in zip(wires, parts, strict=True):
        signature = part.thought_signature
        if signature:
            # A call's own ordinal is stable if compatibility formats hoist
            # reasoning or visible text. Text and detached carriers must also
            # still occupy their original place within that response lane.
            offset = (
                None
                if part.function_call is not None
                else thought_offset
                if part.thought
                else text_offset
            )
            yield _hash(
                (
                    signature.rstrip("=").replace("-", "+").replace("_", "/"),
                    call_ordinal,
                    offset,
                    *_carrier_payload(wire, part),
                )
            )
        if part.function_call is not None:
            call_ordinal += 1
        if part.thought:
            thought_offset += len(part.text)
        else:
            text_offset += len(part.text)


def _owners_match(echoed: Iterable[str], saved: Iterable[str]) -> bool:
    remaining = iter(saved)
    return all(any(candidate == owner for candidate in remaining) for owner in echoed)


def _explicit_owners_match(
    parts: list[AistudioPart], saved: list[list[object]]
) -> bool:
    if not any(part.thought_signature for part in parts):
        return True
    wires = [part.to_wire() for part in parts]
    return _owners_match(
        _signed_owners(wires, map(parse_response_part, wires)),
        _signed_owners(saved, map(parse_response_part, saved)),
    )


def _verified_cohort(
    parts: list[AistudioPart], owners: _ResultOwners, proof: TurnSignatureProof
) -> bool:
    if proof.cohort is None or len(proof.signatures) != owners.count:
        return False
    wires = [part.to_wire() for part in parts]
    parsed = [parse_response_part(wire) for wire in wires]
    cohort, signatures = _call_cohort(wires, parsed)
    return (
        cohort == proof.cohort
        and len(signatures) == len(proof.signatures)
        and ((thought := _thought_digest(parsed)) is None or thought == proof.thought)
        and _owners_match(_signed_owners(wires, parsed), proof.owners)
    )


def _with_observed_turn(turn: str, state: str, owners: _ResultOwners) -> str:
    # A later record must depend on the actual earlier occurrence. A warm
    # restoration and its exact persisted proof contribute the same state.
    return _hash([turn, "observed", state, owners.by_id])


def _echoed_call_ids_match(
    parts: list[AistudioPart], saved: list[list[object]], owners: _ResultOwners
) -> bool:
    echoed = [
        call[2]
        for part in parts
        if (call := part.function_call) is not None and len(call) > 2 and call[2]
    ]
    if not echoed:
        return True
    if len(echoed) != len(owners.by_id):
        return False
    # Allow an alias introduced by the compatibility client, but not the
    # movement of an ID already belonging to a different captured call.
    ordinal = 0
    for wire in saved:
        call = parse_response_part(wire).function_call
        if call is not None:
            call_id = call.get("call_id")
            if (
                isinstance(call_id, str)
                and (owner := owners.by_id.get(call_id)) is not None
                and owner[0] != ordinal
            ):
                return False
            ordinal += 1
    return ordinal == owners.count


def _fill_missing_calls(
    contents: list[AistudioContent], signatures: Iterator[str | None]
) -> list[AistudioContent]:
    """Omissions can be filled without changing an explicitly echoed owner."""
    result: list[AistudioContent] = []
    for content in contents:
        updated: list[AistudioPart] | None = None
        for index, part in enumerate(content.parts):
            if part.function_call is None:
                continue
            signature = next(signatures)
            if signature and not part.thought_signature and part.raw_wire is None:
                if updated is None:
                    updated = list(content.parts)
                updated[index] = replace(part, thought_signature=signature)
        result.append(
            replace(content, parts=updated) if updated is not None else content
        )
    return result


@dataclass(frozen=True)
class ModelTurnContext:
    history: ModelTurnHistory
    scope: str
    prefix: str

    def capture(self) -> ModelTurnCapture:
        return ModelTurnCapture(self)


@dataclass
class ModelTurnCapture:
    context: ModelTurnContext
    parts: list[list[object]] = field(default_factory=list)
    token: str | None = None
    _size: int = 0
    _disabled: bool = False
    _disable_reason: str = ""
    # 进程启动时解析一次的档位上限（流式每 chunk 走 add，避免热路径重复
    # 读环境变量/env 解析；env 不会在运行中变更语义）。
    _max_turn_bytes: int = field(
        default_factory=lambda: _turn_limits()[2], init=False, repr=False
    )

    def add(self, parts: list[list[object]], token: object = None) -> None:
        if self._disabled:
            return
        if (
            isinstance(token, str)
            and 20 <= len(token) <= 4096
            and _TOKEN_RE.fullmatch(token)
        ):
            self.token = token
        for wire in parts:
            parsed = parse_response_part(wire)
            # Media can be large, and server-executed tool results are not
            # client-replayable function results. Do not restore such turns.
            if parsed.inline_data is not None or parsed.function_response is not None:
                self._disabled = True
                self._disable_reason = "media_or_server_tool"
                self.parts.clear()
                return
            if parsed.function_call is not None and not isinstance(
                parsed.function_call.get("args"), dict
            ):
                self._disabled = True
                self._disable_reason = "malformed_call"
                self.parts.clear()
                return
            if parsed.function_call is not None and not isinstance(
                parsed.function_call.get("raw"), list
            ):
                self._disabled = True
                self._disable_reason = "malformed_call"
                self.parts.clear()
                return
            # 尺寸用廉价估计（流式每 chunk 走此路径）：精确上限由 store()
            # 的整树 _json 复核兜底。
            self._size += wire_size_estimate(wire)
            if self._size > self._max_turn_bytes or len(self.parts) >= _MAX_TURN_PARTS:
                self._disabled = True
                self._disable_reason = "too_large"
                self.parts.clear()
                return
            self.parts.append(wire)

    def finish(self, reason: int | None) -> None:
        # A truncated, blocked, failed or cancelled response must not replace
        # a complete historical turn. Each retry gets its own capture.
        if self._disabled or reason != 1 or not self.parts:
            if (trace := get_signature_trace()) is not None:
                if self._disabled:
                    trace.record_cache_store(
                        cached_warm=False,
                        cached_cold=False,
                        status="disabled",
                        note=self._disable_reason or "disabled",
                    )
                elif reason != 1:
                    trace.record_cache_store(
                        cached_warm=False,
                        cached_cold=False,
                        status="skipped",
                        note=f"finish_reason_{reason}",
                    )
                else:
                    trace.record_cache_store(
                        cached_warm=False,
                        cached_cold=False,
                        status="skipped",
                        note="empty_parts",
                    )
            return
        self.context.history.store(self.context, self.parts, self.token)


class ModelTurnHistory:
    """Bounded LRU of complete signed turns, keyed by cumulative history."""

    def __init__(self) -> None:
        cache_bytes, max_entries, max_turn_bytes = _turn_limits()
        # A None payload marks conflicting captures for the same visible
        # history. The marker uses the same bounded LRU as regular captures.
        self._entries: OrderedDict[
            tuple[str, str], tuple[str | None, str | None, int, str]
        ] = OrderedDict()
        self._bytes = 0
        self._max_bytes = cache_bytes
        self._max_entries = max_entries
        self._max_turn_bytes = max_turn_bytes
        self._lock = threading.Lock()
        self._codec = AistudioWireCodec()

    def prepare(
        self, scope: str, contents: list[AistudioContent]
    ) -> tuple[list[AistudioContent], str | None, ModelTurnContext | None]:
        restored: list[AistudioContent] = []
        previous = ""
        token = None
        start = 0
        compatible = True
        owners = None
        trace = get_signature_trace()
        model_turn_index = 0
        cache = (
            get_signature_cache() if any(c.role == "model" for c in contents) else None
        )
        legacy_previous = (
            "" if cache is not None and cache.has_old_observations() else None
        )
        while start < len(contents):
            if not compatible:
                if trace is not None:
                    rem_start = start
                    while rem_start < len(contents):
                        is_model = contents[rem_start].role == "model"
                        rem_end = rem_start + 1
                        while (
                            rem_end < len(contents)
                            and (contents[rem_end].role == "model") == is_model
                        ):
                            rem_end += 1
                        if is_model:
                            model_turn_index += 1
                            rem_calls = sum(
                                1
                                for content in contents[rem_start:rem_end]
                                for part in content.parts
                                if part.function_call is not None
                            )
                            trace.record_replay_turn(
                                turn_index=model_turn_index,
                                action="miss",
                                reason="chain_broken",
                                fc_count=rem_calls,
                            )
                        rem_start = rem_end
                restored.extend(contents[start:])
                break
            model = contents[start].role == "model"
            end = start + 1
            while end < len(contents) and (contents[end].role == "model") == model:
                end += 1
            parts = [part for content in contents[start:end] for part in content.parts]
            previous, legacy_previous = _digest(
                previous, model, parts, owners, legacy_prefix=legacy_previous
            )
            owners = _result_owners(parts) if model else None
            entry = None
            if model:
                model_turn_index += 1
                with self._lock:
                    entry = self._entries.get((scope, previous))
                    if entry is not None:
                        self._entries.move_to_end((scope, previous))
                token = None
            if entry is not None:
                assert owners is not None
                raw, saved_token, _, state = entry
                fc_count = sum(1 for part in parts if part.function_call is not None)
                if raw is None:
                    if trace is not None:
                        trace.record_replay_turn(
                            turn_index=model_turn_index,
                            action="conflict",
                            reason="warm_conflict_withheld",
                            fc_count=fc_count,
                        )
                    restored.extend(contents[start:end])
                    compatible = False
                    start = end
                    continue
                saved: list[list[object]] = json.loads(raw)
                echoed_thought = [part.text or "" for part in parts if part.thought]
                mismatch_reason = ""
                if echoed_thought and _hash("".join(echoed_thought)) != _thought_digest(
                    map(parse_response_part, saved)
                ):
                    mismatch_reason = "thought_digest_mismatch"
                elif not _explicit_owners_match(parts, saved):
                    mismatch_reason = "explicit_owner_mismatch"
                elif not _echoed_call_ids_match(parts, saved, owners):
                    mismatch_reason = "call_id_mismatch"
                if mismatch_reason:
                    # Echoed thoughts or an explicit signature may identify a
                    # different roll with identical visible text.
                    if trace is not None:
                        trace.record_replay_turn(
                            turn_index=model_turn_index,
                            action="conflict",
                            reason=mismatch_reason,
                            fc_count=fc_count,
                        )
                    restored.extend(contents[start:end])
                    compatible = False
                    start = end
                    continue
                ids = (
                    part.function_call[2] if len(part.function_call) > 2 else None
                    for part in parts
                    if part.function_call
                )
                rebuilt: list[AistudioPart] = []
                for wire in saved:
                    part = self._codec.decode_part(wire)
                    if part.function_call is not None:
                        call_id = next(ids)
                        name, args = part.function_call[:2]
                        part.function_call = (
                            (name, args, call_id) if call_id else (name, args)
                        )
                        index = PartIndex.FUNCTION_CALL
                        if len(wire) <= index or not isinstance(wire[index], list):
                            index = PartIndex.FUNCTION_CALL_ALT
                        payload = wire[index]
                        assert isinstance(payload, list)
                        if len(payload) < 3 and call_id:
                            payload.append(call_id)
                        elif len(payload) >= 3:
                            # Keep any future/opaque function-call fields.
                            payload[2] = call_id
                    part.raw_wire = wire
                    rebuilt.append(part)
                restored.append(AistudioContent("model", rebuilt))
                token = saved_token
                previous = _with_observed_turn(previous, state, owners)
                if trace is not None:
                    restored_sigs = sum(
                        1
                        for p in rebuilt
                        if p.thought_signature
                        and p.thought_signature != SKIP_THOUGHT_SIGNATURE
                    )
                    trace.record_replay_turn(
                        turn_index=model_turn_index,
                        action="warm_hit",
                        fc_count=fc_count,
                        restored_sigs=restored_sigs,
                        token_reused=bool(saved_token),
                    )
            elif model:
                assert owners is not None and cache is not None
                fc_count = sum(1 for part in parts if part.function_call is not None)
                found, proof = cache.lookup_turn(scope, previous)
                if found:
                    if proof is not None and _verified_cohort(parts, owners, proof):
                        restored.extend(
                            _fill_missing_calls(
                                contents[start:end], iter(proof.signatures)
                            )
                        )
                        previous = _with_observed_turn(previous, proof.state, owners)
                        if trace is not None:
                            restored_sigs = sum(
                                1
                                for sig in proof.signatures
                                if sig and sig != SKIP_THOUGHT_SIGNATURE
                            )
                            trace.record_replay_turn(
                                turn_index=model_turn_index,
                                action="cold_hit",
                                fc_count=fc_count,
                                restored_sigs=restored_sigs,
                            )
                    else:
                        # Unverified or conflicting history cannot unlock any
                        # later turn, even though its visible digest matches.
                        if trace is not None:
                            trace.record_replay_turn(
                                turn_index=model_turn_index,
                                action="conflict",
                                reason=(
                                    "cold_conflict_withheld"
                                    if proof is None
                                    else "cold_cohort_mismatch"
                                ),
                                fc_count=fc_count,
                            )
                        restored.extend(contents[start:end])
                        compatible = False
                else:
                    calls = [part.function_call for part in parts if part.function_call]
                    seen_old = legacy_previous is not None and any(
                        len(call) > 2
                        and call[2] in owners.by_id
                        and cache.saw_old_call(
                            scope,
                            legacy_previous,
                            ordinal,
                            call[2],
                            wire_function_name(call[0]),
                            call[1],
                        )
                        for ordinal, call in enumerate(calls)
                    )
                    has_explicit_real_sig = any(
                        part.thought_signature
                        and part.thought_signature != SKIP_THOUGHT_SIGNATURE
                        for part in parts
                    )
                    thoughts = [part.text or "" for part in parts if part.thought]
                    if seen_old or has_explicit_real_sig:
                        if trace is not None:
                            trace.record_replay_turn(
                                turn_index=model_turn_index,
                                action="miss",
                                reason=(
                                    "legacy_seen_old"
                                    if seen_old
                                    else "explicit_sig_unobserved"
                                ),
                                fc_count=len(calls),
                            )
                        restored.extend(contents[start:end])
                        compatible = False
                    elif thoughts:
                        # Unobserved turn with unsigned echoed thoughts (e.g.
                        # after an account switch or dynamic system prompt
                        # update): never fabricate SKIP_THOUGHT_SIGNATURE onto
                        # sibling calls, while binding the exact thought digest
                        # into the unobserved prefix so subsequent turns on the
                        # new scope can still be cached and replayed safely.
                        restored.extend(contents[start:end])
                        previous = _hash(
                            [previous, "unobserved", calls, _hash("".join(thoughts))]
                        )
                        if trace is not None:
                            trace.record_replay_turn(
                                turn_index=model_turn_index,
                                action="miss",
                                reason="unobserved_thought",
                                fc_count=len(calls),
                            )
                    else:
                        # Preserve the existing legacy behavior only for an
                        # unidentified batch, never for a failed proof.
                        missing_fc = sum(
                            1
                            for part in parts
                            if part.function_call is not None
                            and not part.thought_signature
                        )
                        restored.extend(
                            _fill_missing_calls(
                                contents[start:end], repeat(SKIP_THOUGHT_SIGNATURE)
                            )
                        )
                        if calls:
                            previous = _hash([previous, "unobserved", calls])
                        if trace is not None:
                            trace.record_replay_turn(
                                turn_index=model_turn_index,
                                action="skip_fill" if missing_fc > 0 else "miss",
                                reason="scope_miss",
                                fc_count=len(calls),
                                restored_sigs=missing_fc,
                            )
            else:
                restored.extend(contents[start:end])
            start = end
        context = (
            ModelTurnContext(self, scope, previous)
            if compatible and contents and contents[-1].role != "model"
            else None
        )
        return restored, token, context

    def store(
        self, context: ModelTurnContext, parts: list[list[object]], token: str | None
    ) -> None:
        trace = get_signature_trace()
        parsed = [parse_response_part(wire) for wire in parts]
        if token is None and not any(
            part.function_call is not None or part.thought or part.thought_signature
            for part in parsed
        ):
            if trace is not None:
                trace.record_cache_store(
                    cached_warm=False,
                    cached_cold=False,
                    status="skipped",
                    note="no_signable_content",
                )
            return
        decoded = [self._codec.decode_part(wire) for wire in parts]
        # Preserve exact response slots for hashing and subsequent replay.
        for part, wire in zip(decoded, parts, strict=True):
            part.raw_wire = wire
        turn, _ = _digest(context.prefix, True, decoded)
        raw: str | None = _json(parts)
        encoded = raw.encode()
        size = len(encoded)
        if size > self._max_turn_bytes:
            if trace is not None:
                trace.record_cache_store(
                    cached_warm=False,
                    cached_cold=False,
                    status="disabled",
                    note="too_large",
                )
            return
        state = hashlib.sha256(encoded).hexdigest()
        del encoded
        cohort, signatures = _call_cohort(parts, parsed)
        proof = TurnSignatureProof(
            state,
            cohort,
            _thought_digest(parsed),
            tuple(_signed_owners(parts, parsed)),
            signatures,
        )
        cold_stored = get_signature_cache().remember_turn(context.scope, turn, proof)
        if not cold_stored:
            raw, token, size = None, None, 0
        key = (context.scope, turn)
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[2]
                if old[0] != raw:
                    # No occurrence ID distinguishes these captures on replay.
                    # Withhold both, even if another identical reroll arrives.
                    raw, token, size = None, None, 0
                elif old[1] != token:
                    # Identical original Parts can be restored, but there is
                    # no evidence for which continuation token to use.
                    token = None
            self._entries[key] = (raw, token, size, state)
            self._bytes += size
            while (
                len(self._entries) > self._max_entries or self._bytes > self._max_bytes
            ):
                _, (_, _, removed, _) = self._entries.popitem(last=False)
                self._bytes -= removed
        if trace is not None:
            warm_stored = raw is not None
            if warm_stored and cold_stored:
                trace.record_cache_store(
                    cached_warm=True,
                    cached_cold=True,
                    status="stored",
                )
            else:
                trace.record_cache_store(
                    cached_warm=warm_stored,
                    cached_cold=cold_stored,
                    status="conflict",
                    note="conflict_withheld",
                )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0
