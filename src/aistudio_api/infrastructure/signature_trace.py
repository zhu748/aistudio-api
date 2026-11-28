"""Per-request thoughtSignature observability and structured runtime logging.

Tracks the full thoughtSignature lifecycle for every model call without
retaining prompts, tool arguments, or full raw signature strings:
  1. client_in    — what the client echoed back and from which carrier
  2. replay       — warm/cold cache hits, SKIP fallbacks, conflicts, miss causes
  3. upstream_out — what actually entered the MakerSuite wire slot 14
  4. upstream_in  — what upstream returned and whether warm/cold cache stored it
  5. client_out   — what the protocol adapter emitted back to the client (or dropped)

Every signature is summarized as an 8-hex-char SHA-256 fingerprint (canonicalized
across URL-safe / unpadded Base64) plus length and a short 12-char prefix, so
operators can trace the exact same signature across turns in runtime logs and
the request detail drawer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from aistudio_api.domain.tool_signatures import (
    SKIP_THOUGHT_SIGNATURE,
    valid_thought_signature,
)
from aistudio_api.infrastructure.request_context import (
    RequestContext,
    current_request,
)
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.domain.models import ResponsePart
    from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

sig_logger = get_logger("thought_signature")

_MAX_TRACE_ITEMS = 32
_MAX_FINGERPRINTS = 16
_MAX_REASONS = 16


def canonical_signature_text(signature: str) -> str:
    """Canonicalize valid Base64 signatures so equivalent encodings share one fingerprint."""
    if signature == SKIP_THOUGHT_SIGNATURE:
        return SKIP_THOUGHT_SIGNATURE
    cleaned = "".join(signature.split())
    if not cleaned:
        return ""
    standard = cleaned.replace("-", "+").replace("_", "/")
    pad_len = (-len(standard)) % 4
    if pad_len:
        standard += "=" * pad_len
    return standard


def signature_fingerprint(signature: str) -> str:
    """Return an 8-char hex fingerprint (or 'SKIP' for the skip sentinel)."""
    if not signature:
        return ""
    if signature == SKIP_THOUGHT_SIGNATURE:
        return "SKIP"
    canonical = canonical_signature_text(signature)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:8]


def signature_head(signature: str) -> str:
    """Return a short prefix preview suitable for manual snapshot cross-checks."""
    if not signature:
        return ""
    if signature == SKIP_THOUGHT_SIGNATURE:
        return "SKIP"
    return signature[:12]


def classify_signature(signature: object) -> str:
    """Classify a candidate signature into missing / skip_sentinel / real / invalid."""
    if not isinstance(signature, str) or not signature:
        return "missing"
    if signature == SKIP_THOUGHT_SIGNATURE:
        return "skip_sentinel"
    if valid_thought_signature(signature):
        return "real"
    return "invalid"


def _add_unique(target: list[str], value: str, *, limit: int = _MAX_REASONS) -> None:
    if value and value not in target and len(target) < limit:
        target.append(value)


def _part_carrier(
    *,
    has_fc: bool = False,
    has_fr: bool = False,
    has_inline: bool = False,
    has_code: bool = False,
    is_thought: bool = False,
    text: str | None = None,
) -> str:
    if has_fc:
        return "function_call"
    if has_fr:
        return "function_response"
    if has_inline:
        return "inline_data"
    if has_code:
        return "code"
    if is_thought:
        return "thought"
    if text == "" or text is None:
        return "tail_empty"
    return "text"


def _make_item(
    *,
    carrier: str,
    kind: str,
    signature: str = "",
    source: str = "",
    call_id: str = "",
    call_name: str = "",
    turn_index: int | None = None,
    lead_call: bool | None = None,
    note: str = "",
) -> dict[str, object]:
    item: dict[str, object] = {
        "carrier": carrier,
        "kind": kind,
        "len": len(signature) if signature else 0,
        "fp": signature_fingerprint(signature) if signature else "",
        "head": signature_head(signature) if signature else "",
    }
    if source:
        item["source"] = source
    if call_id:
        item["call_id"] = call_id[:64]
    if call_name:
        item["call_name"] = call_name[:64]
    if turn_index is not None:
        item["turn_index"] = turn_index
    if lead_call is not None:
        item["lead_call"] = lead_call
    if note:
        item["note"] = note[:120]
    return item


@dataclass
class ThoughtSignatureTrace:
    """Compact per-request thoughtSignature diagnostic trace."""

    protocol: str = "gemini"
    attempt: int = 0
    # Keep the owner alive for this request so its id cannot be reused.
    _inbound_sources: dict[int, tuple[object, str]] = field(
        default_factory=dict, repr=False
    )
    # Stage 1: client_in
    client_model_turns: int = 0
    client_fc_total: int = 0
    client_fc_signed: int = 0
    client_fc_skip: int = 0
    client_fc_missing: int = 0
    client_fc_unsigned_lead_turns: int = 0
    _client_last_fc_turn: int | None = field(default=None, repr=False)
    client_thought_total: int = 0
    client_thought_signed: int = 0
    client_tail_signed: int = 0
    client_other_signed: int = 0
    client_dropped: int = 0
    # tool_result blocks whose tool_use is absent from the client history:
    # they are forwarded as transcript text (no functionResponse), which is a
    # client-side history mismatch worth surfacing, not a signature risk.
    client_orphan_results: int = 0
    client_sources: list[str] = field(default_factory=list)
    client_in_items: list[dict[str, object]] = field(default_factory=list)
    _client_in_normalized: bool = field(default=False, repr=False)

    # Stage 2: replay (per attempt)
    warm_hits: int = 0
    cold_hits: int = 0
    skip_fills: int = 0
    misses: int = 0
    conflicts: int = 0
    continuation_reused: bool = False
    miss_reasons: list[str] = field(default_factory=list)
    replay_items: list[dict[str, object]] = field(default_factory=list)

    # Stage 3: upstream_out (per attempt)
    upstream_out_real: int = 0
    upstream_out_skip: int = 0
    upstream_out_fc_total: int = 0
    upstream_out_fc_unsigned: int = 0
    # Model turns whose FIRST functionCall goes upstream without any signature.
    # A parallel batch normally carries its signature on the first call.
    # Use the lead count for the "400 missing thought_signature" warning.
    upstream_out_turns_unsigned_lead: int = 0
    upstream_out_continuation: bool = False
    upstream_out_fps: list[str] = field(default_factory=list)
    upstream_out_items: list[dict[str, object]] = field(default_factory=list)

    # Stage 4: upstream_in (per attempt)
    upstream_in_total: int = 0
    upstream_in_by_carrier: dict[str, int] = field(default_factory=dict)
    cached_warm: bool = False
    cached_cold: bool = False
    cache_status: str = ""
    cache_note: str = ""
    upstream_in_fps: list[str] = field(default_factory=list)
    upstream_in_items: list[dict[str, object]] = field(default_factory=list)

    # Stage 5: client_out
    client_out_total: int = 0
    client_out_carriers: list[str] = field(default_factory=list)
    client_out_dropped: int = 0
    client_out_drop_reasons: list[str] = field(default_factory=list)
    client_out_fps: list[str] = field(default_factory=list)
    client_out_items: list[dict[str, object]] = field(default_factory=list)
    _inbound_logged: bool = field(default=False, repr=False)

    def reset_upstream_in(self) -> None:
        """Reset upstream response signature fields when retrying a prepared request."""
        self.upstream_in_total = 0
        self.upstream_in_by_carrier.clear()
        self.cached_warm = False
        self.cached_cold = False
        self.cache_status = ""
        self.cache_note = ""
        self.upstream_in_fps.clear()
        self.upstream_in_items.clear()

    def reset_attempt(self) -> None:
        """Reset per-attempt upstream/replay fields while keeping client_in intact."""
        self.attempt += 1
        self.warm_hits = 0
        self.cold_hits = 0
        self.skip_fills = 0
        self.misses = 0
        self.conflicts = 0
        self.continuation_reused = False
        self.miss_reasons.clear()
        self.replay_items.clear()

        self.upstream_out_real = 0
        self.upstream_out_skip = 0
        self.upstream_out_fc_total = 0
        self.upstream_out_fc_unsigned = 0
        self.upstream_out_turns_unsigned_lead = 0
        self.upstream_out_continuation = False
        self.upstream_out_fps.clear()
        self.upstream_out_items.clear()

        self.upstream_in_total = 0
        self.upstream_in_by_carrier.clear()
        self.cached_warm = False
        self.cached_cold = False
        self.cache_status = ""
        self.cache_note = ""
        self.upstream_in_fps.clear()
        self.upstream_in_items.clear()

        self.client_out_total = 0
        self.client_out_carriers.clear()
        self.client_out_dropped = 0
        self.client_out_drop_reasons.clear()
        self.client_out_fps.clear()
        self.client_out_items.clear()
        self._inbound_logged = False

    def tag_inbound_source(self, part_obj: object, source: str) -> None:
        """Remember only the latest nonempty source for this exact part."""
        if source:
            self._inbound_sources[id(part_obj)] = (part_obj, source)

    def lookup_inbound_source(self, part_obj: object) -> str:
        tagged = self._inbound_sources.get(id(part_obj))
        if tagged is not None and tagged[0] is part_obj:
            return tagged[1]
        return (
            "client_native" if self.protocol == "gemini" else f"client_{self.protocol}"
        )

    def record_client_drop(
        self,
        *,
        carrier: str,
        raw_signature: object = "",
        source: str = "",
        call_id: str = "",
        call_name: str = "",
        turn_index: int | None = None,
        note: str = "",
    ) -> None:
        self.client_dropped += 1
        sig_str = raw_signature if isinstance(raw_signature, str) else ""
        kind = classify_signature(sig_str) if sig_str else "invalid"
        if len(self.client_in_items) < _MAX_TRACE_ITEMS:
            self.client_in_items.append(
                _make_item(
                    carrier=carrier,
                    kind=kind,
                    signature=sig_str,
                    source=source,
                    call_id=call_id,
                    call_name=call_name,
                    turn_index=turn_index,
                    note=note,
                )
            )

    def record_client_orphan_result(
        self, *, call_id: str = "", call_name: str = "", payload_chars: int = 0
    ) -> None:
        """A client tool_result had no matching tool_use in the history."""
        self.client_orphan_results += 1
        if len(self.client_in_items) < _MAX_TRACE_ITEMS:
            self.client_in_items.append(
                _make_item(
                    carrier="tool_result",
                    kind="orphan",
                    call_id=call_id,
                    call_name=call_name,
                    note=f"无对应 tool_use，已转为文本 {payload_chars} 字",
                )
            )

    def record_client_part(
        self,
        *,
        carrier: str,
        raw_signature: object,
        source: str,
        call_id: str = "",
        call_name: str = "",
        turn_index: int | None = None,
        stripped_note: str = "",
    ) -> None:
        kind = classify_signature(raw_signature)
        sig_str = raw_signature if isinstance(raw_signature, str) else ""
        lead_call = None

        if carrier == "function_call":
            if turn_index is not None:
                lead_call = turn_index != self._client_last_fc_turn
                self._client_last_fc_turn = turn_index
            if lead_call and (kind in ("missing", "invalid") or stripped_note):
                self.client_fc_unsigned_lead_turns += 1
            self.client_fc_total += 1
            if kind == "real" and not stripped_note:
                self.client_fc_signed += 1
                _add_unique(self.client_sources, source)
            elif kind == "skip_sentinel" and not stripped_note:
                self.client_fc_skip += 1
                _add_unique(self.client_sources, source)
            elif kind == "invalid" or stripped_note:
                self.client_fc_missing += 1
                self.client_dropped += 1
            else:
                self.client_fc_missing += 1
        elif carrier == "thought":
            self.client_thought_total += 1
            if kind == "real" and not stripped_note:
                self.client_thought_signed += 1
                _add_unique(self.client_sources, source)
            elif kind == "invalid" or (sig_str and stripped_note):
                self.client_dropped += 1
        elif carrier == "tail_empty":
            if kind in ("real", "skip_sentinel") and not stripped_note:
                self.client_tail_signed += 1
                _add_unique(self.client_sources, source)
            elif kind == "invalid" or (sig_str and stripped_note):
                self.client_dropped += 1
        elif kind in ("real", "skip_sentinel") and not stripped_note:
            self.client_other_signed += 1
            _add_unique(self.client_sources, source)
        elif kind == "invalid" or (sig_str and stripped_note):
            self.client_dropped += 1

        # Record notable entries (any signature, any function_call, or any drop)
        should_record = (
            carrier == "function_call"
            or kind != "missing"
            or bool(stripped_note)
            or (carrier == "thought" and self.client_thought_total <= 4)
        )
        if should_record and len(self.client_in_items) < _MAX_TRACE_ITEMS:
            note = stripped_note or ("invalid_base64" if kind == "invalid" else "")
            self.client_in_items.append(
                _make_item(
                    carrier=carrier,
                    kind=kind,
                    signature=sig_str,
                    source=source if sig_str else "",
                    call_id=call_id,
                    call_name=call_name,
                    turn_index=turn_index,
                    lead_call=lead_call,
                    note=note,
                )
            )

    def record_replay_turn(
        self,
        *,
        turn_index: int,
        action: str,
        reason: str = "",
        fc_count: int = 0,
        restored_sigs: int = 0,
        token_reused: bool = False,
    ) -> None:
        if action == "warm_hit":
            self.warm_hits += 1
        elif action == "cold_hit":
            self.cold_hits += 1
        elif action == "skip_fill":
            self.skip_fills += max(1, restored_sigs) if fc_count else 0
            if fc_count == 0:
                self.misses += 1
        elif action == "conflict":
            self.conflicts += 1
        elif action == "miss":
            self.misses += 1

        if token_reused:
            self.continuation_reused = True
        if reason:
            _add_unique(self.miss_reasons, reason)
        if len(self.replay_items) < _MAX_TRACE_ITEMS:
            entry: dict[str, object] = {
                "turn_index": turn_index,
                "action": action,
                "fc_count": fc_count,
                "restored_sigs": restored_sigs,
            }
            if reason:
                entry["reason"] = reason
            if token_reused:
                entry["continuation_reused"] = True
            self.replay_items.append(entry)

    def record_upstream_out(
        self, contents: list[AistudioContent], continuation_sent: bool
    ) -> None:
        self.upstream_out_continuation = continuation_sent
        model_turn = 0
        in_model_turn = False
        lead_fc_seen = False
        for content in contents:
            if content.role != "model":
                in_model_turn = False
                continue
            if not in_model_turn:
                model_turn += 1
                lead_fc_seen = False
                in_model_turn = True
            for part in content.parts:
                sig = part.thought_signature or ""
                kind = classify_signature(sig)
                has_fc = part.function_call is not None
                lead_call = not lead_fc_seen if has_fc else None
                carrier = _part_carrier(
                    has_fc=has_fc,
                    has_fr=part.function_response is not None,
                    has_inline=part.inline_data is not None,
                    has_code=(
                        part.executable_code is not None
                        or part.code_execution_result is not None
                    ),
                    is_thought=bool(part.thought),
                    text=part.text,
                )
                call_name = part.function_call[0] if part.function_call else ""
                call_id = (
                    str(part.function_call[2])
                    if part.function_call and len(part.function_call) > 2
                    else ""
                )
                if has_fc:
                    self.upstream_out_fc_total += 1
                    if kind == "missing":
                        self.upstream_out_fc_unsigned += 1
                    if lead_call:
                        lead_fc_seen = True
                        if kind == "missing":
                            self.upstream_out_turns_unsigned_lead += 1
                if kind == "real":
                    self.upstream_out_real += 1
                    _add_unique(
                        self.upstream_out_fps,
                        signature_fingerprint(sig),
                        limit=_MAX_FINGERPRINTS,
                    )
                elif kind == "skip_sentinel":
                    self.upstream_out_skip += 1
                if (has_fc or kind != "missing") and len(
                    self.upstream_out_items
                ) < _MAX_TRACE_ITEMS:
                    self.upstream_out_items.append(
                        _make_item(
                            carrier=carrier,
                            kind=kind,
                            signature=sig,
                            source=(
                                "warm_cache"
                                if part.raw_wire is not None
                                else "skip_fallback"
                                if kind == "skip_sentinel"
                                else "outbound_wire"
                            ),
                            call_id=call_id,
                            call_name=call_name,
                            turn_index=model_turn,
                            lead_call=lead_call,
                        )
                    )

    def record_upstream_in_parts(self, parts: list[ResponsePart]) -> None:
        for part in parts:
            sig = part.thought_signature or ""
            if not sig:
                continue
            kind = classify_signature(sig)
            fc = part.function_call
            carrier = _part_carrier(
                has_fc=fc is not None,
                has_fr=part.function_response is not None,
                has_inline=part.image is not None,
                has_code=(
                    part.executable_code is not None
                    or part.code_execution_result is not None
                ),
                is_thought=bool(part.thought),
                text=part.text,
            )
            self.upstream_in_total += 1
            self.upstream_in_by_carrier[carrier] = (
                self.upstream_in_by_carrier.get(carrier, 0) + 1
            )
            fp = signature_fingerprint(sig)
            _add_unique(self.upstream_in_fps, fp, limit=_MAX_FINGERPRINTS)
            if len(self.upstream_in_items) < _MAX_TRACE_ITEMS:
                self.upstream_in_items.append(
                    _make_item(
                        carrier=carrier,
                        kind=kind,
                        signature=sig,
                        source="upstream_wire",
                        call_id=str(fc.get("id") or "") if isinstance(fc, dict) else "",
                        call_name=(
                            str(fc.get("name") or "") if isinstance(fc, dict) else ""
                        ),
                    )
                )

    def record_cache_store(
        self,
        *,
        cached_warm: bool,
        cached_cold: bool,
        status: str,
        note: str = "",
    ) -> None:
        self.cached_warm = cached_warm
        self.cached_cold = cached_cold
        self.cache_status = status
        if note:
            self.cache_note = note

    def record_client_out_sig(
        self,
        *,
        carrier: str,
        wire_field: str,
        signature: str,
        call_id: str = "",
        call_name: str = "",
    ) -> None:
        if not signature:
            return
        self.client_out_total += 1
        _add_unique(self.client_out_carriers, wire_field)
        fp = signature_fingerprint(signature)
        _add_unique(self.client_out_fps, fp, limit=_MAX_FINGERPRINTS)
        if len(self.client_out_items) < _MAX_TRACE_ITEMS:
            self.client_out_items.append(
                _make_item(
                    carrier=carrier,
                    kind=classify_signature(signature),
                    signature=signature,
                    source=wire_field,
                    call_id=call_id,
                    call_name=call_name,
                )
            )

    def record_client_out_drop(
        self,
        *,
        carrier: str,
        signature: str,
        reason: str,
        call_id: str = "",
        call_name: str = "",
    ) -> None:
        if not signature:
            return
        self.client_out_dropped += 1
        _add_unique(self.client_out_drop_reasons, reason)
        if len(self.client_out_items) < _MAX_TRACE_ITEMS:
            self.client_out_items.append(
                _make_item(
                    carrier=carrier,
                    kind=classify_signature(signature),
                    signature=signature,
                    source="dropped",
                    call_id=call_id,
                    call_name=call_name,
                    note=reason,
                )
            )

    def to_dict(self) -> dict[str, object]:
        """Serialize into a redaction-safe dict for RequestContext and SQLite."""
        return {
            "protocol": self.protocol,
            "attempt": self.attempt,
            "client_in": {
                "model_turns": self.client_model_turns,
                "fc_total": self.client_fc_total,
                "fc_signed": self.client_fc_signed,
                "fc_skip": self.client_fc_skip,
                "fc_missing": self.client_fc_missing,
                "fc_unsigned_lead_turns": self.client_fc_unsigned_lead_turns,
                "thought_total": self.client_thought_total,
                "thought_signed": self.client_thought_signed,
                "tail_signed": self.client_tail_signed,
                "other_signed": self.client_other_signed,
                "dropped": self.client_dropped,
                "orphan_results": self.client_orphan_results,
                "sources": list(self.client_sources),
                "items": list(self.client_in_items),
            },
            "replay": {
                "warm_hits": self.warm_hits,
                "cold_hits": self.cold_hits,
                "skip_fills": self.skip_fills,
                "misses": self.misses,
                "conflicts": self.conflicts,
                "continuation_reused": self.continuation_reused,
                "miss_reasons": list(self.miss_reasons),
                "items": list(self.replay_items),
            },
            "upstream_out": {
                "real_sigs": self.upstream_out_real,
                "skip_sigs": self.upstream_out_skip,
                "fc_total": self.upstream_out_fc_total,
                "fc_unsigned": self.upstream_out_fc_unsigned,
                "fc_unsigned_lead_turns": self.upstream_out_turns_unsigned_lead,
                "continuation_sent": self.upstream_out_continuation,
                "fingerprints": list(self.upstream_out_fps),
                "items": list(self.upstream_out_items),
            },
            "upstream_in": {
                "total_sigs": self.upstream_in_total,
                "by_carrier": dict(self.upstream_in_by_carrier),
                "cached_warm": self.cached_warm,
                "cached_cold": self.cached_cold,
                "cache_status": self.cache_status,
                "cache_note": self.cache_note,
                "fingerprints": list(self.upstream_in_fps),
                "items": list(self.upstream_in_items),
            },
            "client_out": {
                "total_sigs": self.client_out_total,
                "carriers": list(self.client_out_carriers),
                "dropped": self.client_out_dropped,
                "drop_reasons": list(self.client_out_drop_reasons),
                "fingerprints": list(self.client_out_fps),
                "items": list(self.client_out_items),
            },
        }

    def sync(self, context: RequestContext | None = None) -> None:
        ctx = context or current_request.get()
        if ctx is not None:
            ctx.thought_signature = self.to_dict()

    def emit_outbound_log(self) -> None:
        self.sync()
        has_warn = (
            self.upstream_out_turns_unsigned_lead > 0
            or self.conflicts > 0
            or self.client_dropped > 0
        )
        status = "warn" if has_warn else ("fallback" if self.skip_fills > 0 else "ok")
        fp_text = ",".join(self.upstream_out_fps[:6]) if self.upstream_out_fps else "无"
        if self.client_model_turns == 0:
            client_part = "首轮无历史"
        else:
            client_part = (
                f"历史轮={self.client_model_turns} "
                f"FC={self.client_fc_signed}/{self.client_fc_total}"
                + (f"(SKIP:{self.client_fc_skip})" if self.client_fc_skip else "")
                + f" 首FC无可用签名轮={self.client_fc_unsigned_lead_turns}"
                + f" thought={self.client_thought_signed}/{self.client_thought_total}"
                + (
                    f" 尾载体={self.client_tail_signed}"
                    if self.client_tail_signed
                    else ""
                )
                + (f" 丢弃={self.client_dropped}" if self.client_dropped else "")
                + (
                    f" 孤儿结果={self.client_orphan_results}"
                    if self.client_orphan_results
                    else ""
                )
            )
        replay_part = (
            f"温命中={self.warm_hits} 冷命中={self.cold_hits} "
            f"SKIP补填={self.skip_fills}"
            + (f" 冲突={self.conflicts}" if self.conflicts else "")
            + (f" 原因={','.join(self.miss_reasons[:3])}" if self.miss_reasons else "")
        )
        upstream_part = (
            f"签名={self.upstream_out_real}(fp:{fp_text}) "
            f"SKIP={self.upstream_out_skip} 未签FC={self.upstream_out_fc_unsigned} "
            f"发送续传={'是' if self.upstream_out_continuation else '否'}"
            + (
                f"(首FC裸发轮={self.upstream_out_turns_unsigned_lead})"
                if self.upstream_out_turns_unsigned_lead
                else ""
            )
        )
        sig_meta: dict[str, object] = {
            "direction": "outbound",
            "protocol": self.protocol,
            "status": status,
            "attempt": self.attempt,
            "client_model_turns": self.client_model_turns,
            "client_fc_total": self.client_fc_total,
            "client_fc_signed": self.client_fc_signed,
            "client_fc_missing": self.client_fc_missing,
            "client_fc_unsigned_lead": self.client_fc_unsigned_lead_turns,
            "client_thought_signed": self.client_thought_signed,
            "client_tail_signed": self.client_tail_signed,
            "client_dropped": self.client_dropped,
            "client_orphan_results": self.client_orphan_results,
            "client_sources": list(self.client_sources),
            "warm_hits": self.warm_hits,
            "cold_hits": self.cold_hits,
            "skip_fills": self.skip_fills,
            "conflicts": self.conflicts,
            "misses": self.misses,
            "continuation_reused": self.continuation_reused,
            "continuation_sent": self.upstream_out_continuation,
            "upstream_real": self.upstream_out_real,
            "upstream_skip": self.upstream_out_skip,
            "upstream_fc_unsigned": self.upstream_out_fc_unsigned,
            "upstream_fc_unsigned_lead": self.upstream_out_turns_unsigned_lead,
            "fingerprints": list(self.upstream_out_fps[:6]),
        }
        msg = (
            f"[thoughtSignature][出站] 协议={self.protocol} | "
            f"客户端回传: {client_part} | 恢复: {replay_part} | "
            f"发往上游: {upstream_part}"
        )
        if has_warn:
            sig_logger.warning("%s", msg, extra={"sig_meta": sig_meta})
        else:
            sig_logger.info("%s", msg, extra={"sig_meta": sig_meta})
        if self.upstream_out_items:
            sig_logger.debug(
                "[thoughtSignature][出站明细] %s",
                self.upstream_out_items[:8],
                extra={"sig_meta": sig_meta},
            )

    def emit_inbound_log(self) -> None:
        if self._inbound_logged:
            return
        self._inbound_logged = True
        self.sync()
        if self.upstream_in_total > 0 and self.client_out_total == 0:
            status = "dropped"
        elif self.client_out_dropped > 0:
            status = "partial"
        else:
            status = "ok"

        carrier_summary = (
            ", ".join(f"{k}:{v}" for k, v in self.upstream_in_by_carrier.items())
            if self.upstream_in_by_carrier
            else "无"
        )
        up_fp_text = (
            ",".join(self.upstream_in_fps[:6]) if self.upstream_in_fps else "无"
        )
        out_carriers = (
            ",".join(self.client_out_carriers) if self.client_out_carriers else "无"
        )
        cache_text = (
            f"温={'是' if self.cached_warm else '否'},"
            f"冷={'是' if self.cached_cold else '否'}"
            + (f"({self.cache_note})" if self.cache_note else "")
        )
        drop_text = (
            f" 协议丢弃={self.client_out_dropped}({','.join(self.client_out_drop_reasons[:3])})"
            if self.client_out_dropped
            else ""
        )
        sig_meta: dict[str, object] = {
            "direction": "inbound",
            "protocol": self.protocol,
            "status": status,
            "attempt": self.attempt,
            "upstream_total": self.upstream_in_total,
            "by_carrier": dict(self.upstream_in_by_carrier),
            "cached_warm": self.cached_warm,
            "cached_cold": self.cached_cold,
            "cache_status": self.cache_status,
            "cache_note": self.cache_note,
            "client_total": self.client_out_total,
            "client_carriers": list(self.client_out_carriers),
            "client_dropped": self.client_out_dropped,
            "drop_reasons": list(self.client_out_drop_reasons),
            "fingerprints": list(self.client_out_fps[:6] or self.upstream_in_fps[:6]),
        }
        msg = (
            f"[thoughtSignature][入站] 协议={self.protocol} | "
            f"上游返回: {self.upstream_in_total}({carrier_summary}, fp:{up_fp_text}) "
            f"缓存[{cache_text}] | "
            f"下发客户端: {self.client_out_total}({out_carriers}){drop_text}"
        )
        sig_logger.info("%s", msg, extra={"sig_meta": sig_meta})
        if self.upstream_in_items or self.client_out_items:
            sig_logger.debug(
                "[thoughtSignature][入站明细] upstream=%s client=%s",
                self.upstream_in_items[:8],
                self.client_out_items[:8],
                extra={"sig_meta": sig_meta},
            )


def get_signature_trace(*, create: bool = False) -> ThoughtSignatureTrace | None:
    """Return the active request's ThoughtSignatureTrace, optionally initializing it."""
    ctx = current_request.get()
    if ctx is None:
        return None
    existing = cast(
        ThoughtSignatureTrace | None, getattr(ctx, "_signature_trace", None)
    )
    if existing is not None:
        return existing
    if not create:
        return None
    protocol = ctx.protocol or "gemini"
    trace = ThoughtSignatureTrace(protocol=protocol)
    object.__setattr__(ctx, "_signature_trace", trace)
    return trace


def set_trace_protocol(protocol: str) -> ThoughtSignatureTrace | None:
    """Ensure trace exists for the active request and bind its protocol label."""
    trace = get_signature_trace(create=True)
    if trace is not None:
        trace.protocol = protocol
    return trace


__all__ = [
    "ThoughtSignatureTrace",
    "canonical_signature_text",
    "classify_signature",
    "get_signature_trace",
    "set_trace_protocol",
    "sig_logger",
    "signature_fingerprint",
    "signature_head",
]
