"""Restore exact upstream model turns echoed by a compatible client.

Adapted from Mag1cFall/AIStudio2API's model_turns.go (MIT). The package's
THIRD_PARTY_NOTICES.md retains its license. Entries are scoped to the client,
upstream account, model and instructions, and live in bounded process memory.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from aistudio_api.domain.tool_signatures import SKIP_THOUGHT_SIGNATURE

from .wire_codec import AistudioWireCodec
from .wire_parser import parse_response_part, wire_size_estimate
from .wire_spec import PartIndex
from .wire_types import AistudioContent, AistudioPart

_TOKEN_RE = re.compile(r"v1_[A-Za-z0-9+/=_-]{16,}")
_MAX_TURN_BYTES = 4 * 1024 * 1024
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 1000
_MAX_TURN_PARTS = 16384


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(previous: str, model: bool, parts: list[AistudioPart]) -> str:
    items: list[object] = []
    text: list[str] = []

    def flush_text() -> None:
        if text:
            value = "".join(text).strip()
            if value:
                items.append(["text", value])
            text.clear()

    for part in parts:
        # raw_wire 仅被只读检查（parse_response_part 不改写入参，签名位
        # 归零在浅拷贝 visible 上进行），无需 to_wire 的整树 deepcopy——
        # store() 对捕获轮的每个 part 深拷贝曾占摘要计算的大头。
        wire = part.raw_wire if part.raw_wire is not None else part.to_wire()
        parsed = parse_response_part(wire)
        if parsed.thought:
            continue
        if parsed.text:
            text.append(parsed.text)
        for kind, payload in (
            ("call", parsed.function_call),
            ("result", parsed.function_response),
        ):
            if payload is not None:
                flush_text()
                items.append(
                    [
                        kind,
                        payload.get("name"),
                        payload.get("args", payload.get("arguments", {})),
                    ]
                )
        if parsed.inline_data is not None:
            flush_text()
            mime, data = parsed.inline_data
            items.append(["media", mime, hashlib.sha256(data.encode()).hexdigest()])
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
            not parsed.text
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
    return hashlib.sha256(_json([previous, model, items]).encode()).hexdigest()


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
                self.parts.clear()
                return
            if parsed.function_call is not None and not isinstance(
                parsed.function_call.get("args"), dict
            ):
                self._disabled = True
                self.parts.clear()
                return
            if parsed.function_call is not None and not isinstance(
                parsed.function_call.get("raw"), list
            ):
                self._disabled = True
                self.parts.clear()
                return
            # 尺寸用廉价估计（流式每 chunk 走此路径）：精确上限由 store()
            # 的整树 _json 复核兜底。
            self._size += wire_size_estimate(wire)
            if self._size > _MAX_TURN_BYTES or len(self.parts) >= _MAX_TURN_PARTS:
                self._disabled = True
                self.parts.clear()
                return
            self.parts.append(wire)

    def finish(self, reason: int | None) -> None:
        # A truncated, blocked, failed or cancelled response must not replace
        # a complete historical turn. Each retry gets its own capture.
        if self._disabled or reason != 1 or not self.parts:
            return
        if self.token is None and not any(
            (part := parse_response_part(wire)).thought or part.thought_signature
            for wire in self.parts
        ):
            return
        self.context.history.store(self.context, self.parts, self.token)


class ModelTurnHistory:
    """Bounded LRU of complete signed turns, keyed by cumulative history."""

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str], tuple[str, str | None, int]] = (
            OrderedDict()
        )
        self._bytes = 0
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
        while start < len(contents):
            model = contents[start].role == "model"
            end = start + 1
            while end < len(contents) and (contents[end].role == "model") == model:
                end += 1
            parts = [part for content in contents[start:end] for part in content.parts]
            previous = _digest(previous, model, parts)
            entry = None
            if model:
                with self._lock:
                    entry = self._entries.get((scope, previous)) if compatible else None
                    if entry is not None:
                        self._entries.move_to_end((scope, previous))
                token = None
            if entry is not None:
                raw, saved_token, _ = entry
                saved: list[list[object]] = json.loads(raw)
                known = {parse_response_part(wire).thought_signature for wire in saved}
                if any(
                    part.thought_signature not in known
                    for part in parts
                    if part.thought_signature
                    and part.thought_signature != SKIP_THOUGHT_SIGNATURE
                ):
                    # An explicit different state may belong to another roll
                    # with identical visible text. Keep the client's state.
                    restored.extend(contents[start:end])
                    compatible = False
                    start = end
                    continue
                ids = [
                    part.function_call[2] if len(part.function_call) > 2 else None
                    for part in parts
                    if part.function_call
                ]
                rebuilt: list[AistudioPart] = []
                for wire in saved:
                    part = self._codec.decode_part(wire)
                    if part.function_call is not None:
                        call_id = ids.pop(0)
                        name, args = part.function_call[:2]
                        part.function_call = (
                            (name, args, call_id) if call_id else (name, args)
                        )
                        index = PartIndex.FUNCTION_CALL_ALT
                        if len(wire) <= index or not isinstance(wire[index], list):
                            index = PartIndex.FUNCTION_CALL
                        payload = wire[index]
                        assert isinstance(payload, list)
                        wire[index] = payload[:2] + ([call_id] if call_id else [])
                    part.raw_wire = wire
                    rebuilt.append(part)
                restored.append(AistudioContent("model", rebuilt))
                token = saved_token
            else:
                restored.extend(contents[start:end])
            start = end
        context = (
            ModelTurnContext(self, scope, previous)
            if contents and contents[-1].role != "model"
            else None
        )
        return restored, token, context

    def store(
        self, context: ModelTurnContext, parts: list[list[object]], token: str | None
    ) -> None:
        decoded = [self._codec.decode_part(wire) for wire in parts]
        # Preserve exact response slots for hashing and subsequent replay.
        for part, wire in zip(decoded, parts, strict=True):
            part.raw_wire = wire
        key = (context.scope, _digest(context.prefix, True, decoded))
        raw = _json(parts)
        size = len(raw.encode())
        if size > _MAX_TURN_BYTES:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[2]
            self._entries[key] = (raw, token, size)
            self._bytes += size
            while len(self._entries) > _MAX_ENTRIES or self._bytes > _MAX_CACHE_BYTES:
                _, (_, _, removed) = self._entries.popitem(last=False)
                self._bytes -= removed

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0
