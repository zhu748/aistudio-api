"""Shared protocol state for tool calls and stop sequences."""

from __future__ import annotations

import json
import math
import re
import secrets
from array import array
from collections.abc import AsyncGenerator, AsyncIterable, Generator
from dataclasses import dataclass, field

from aistudio_api.infrastructure.gateway.wire_parser import PositionalTextCall
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("stream.utils")

_MAX_TOOL_ARGUMENT_BYTES = 16 * 1024 * 1024
_MAX_TOOL_ARGUMENT_FRAGMENTS = 16_384
_MAX_TOOL_RESPONSE_BYTES = 64 * 1024 * 1024


class SSEEventBuffer:
    """Scan each fragment once and join only completed lines/events.

    SSE accepts LF, CRLF and CR line endings, including a CRLF split across
    fragments. Bound every event before parsing it, even when its delimiter
    arrives in the same fragment as the payload.
    """

    _line_ending = re.compile(r"\r\n|[\r\n]")

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._size = 0
        self._line: list[str] = []
        self._lines: list[str] = []
        self._skip_lf = False

    def _append(self, text: str) -> None:
        if not text:
            return
        # ASCII 快路径：isascii() 是无分配的快速扫描，纯 ASCII 分片免去
        # 一次全量 encode 分配；非 ASCII 仍按字节数计，限额语义不变。
        self._size += len(text) if text.isascii() else len(text.encode("utf-8"))
        if self._size > self.max_bytes:
            raise ValueError("Gemini SSE event exceeds configured size limit")
        self._line.append(text)

    def feed(self, text: str) -> Generator[str, None, None]:
        if not text:
            return
        start = 0
        if self._skip_lf:
            start = int(text.startswith("\n"))
            self._skip_lf = False
        for match in self._line_ending.finditer(text, start):
            self._append(text[start : match.start()])
            start = match.end()
            self._skip_lf = match.group() == "\r" and start == len(text)
            if not self._line:
                event = "\n".join(self._lines)
                self._lines.clear()
                self._size = 0
                if event:
                    yield event
            else:
                self._lines.append("".join(self._line))
                self._line.clear()
                self._size += len(match.group())
                if self._size > self.max_bytes:
                    raise ValueError("Gemini SSE event exceeds configured size limit")
        self._append(text[start:])

    def finish(self) -> str:
        """Preserve the existing EOF-tail recovery for an unterminated event."""
        if self._line:
            self._lines.append("".join(self._line))
        event = "\n".join(self._lines)
        self._line.clear()
        self._lines.clear()
        self._size = 0
        self._skip_lf = False
        return event


class StopSequenceBuffer:
    """Keep only a suffix which could become a stop sequence in the next frame."""

    def __init__(self, sequences: list[str] | None) -> None:
        self.sequences = [value for value in sequences or [] if value]
        self._prefixes: list[tuple[str, array[int]]] = []
        for value in self.sequences:
            if len(value) <= 1:
                continue
            prefix = array("I", [0]) * len(value)
            matched = 0
            for index in range(1, len(value)):
                while matched and value[index] != value[matched]:
                    matched = prefix[matched - 1]
                if value[index] == value[matched]:
                    matched += 1
                prefix[index] = matched
            self._prefixes.append((value, prefix))
        self.pending = ""
        self.hit: str | None = None

    def feed(self, text: str, *, final: bool = False) -> str:
        if self.hit is not None:
            return ""
        text = self.pending + text
        matches: list[tuple[int, str]] = []
        for value in self.sequences:
            # 单次 find：原先 ``if value in text`` + ``text.find(value)``
            # 对同一序列做两次 C 级扫描，序列数多时每帧翻倍浪费。
            index = text.find(value)
            if index >= 0:
                matches.append((index, value))
        if matches:
            index, self.hit = min(matches, key=lambda match: match[0])
            self.pending = ""
            return text[:index]
        hold = 0
        if not final:
            for value, prefix in self._prefixes:
                if len(value) - 1 <= hold:
                    continue
                # Only this suffix can be a partial stop. KMP avoids building
                # and testing every increasingly long prefix (quadratic copies).
                matched = 0
                for char in text[-(len(value) - 1) :]:
                    while matched and char != value[matched]:
                        matched = prefix[matched - 1]
                    if char == value[matched]:
                        matched += 1
                hold = max(hold, matched)
        self.pending = text[-hold:] if hold else ""
        return text[:-hold] if hold else text


_SURROGATE = re.compile(r"[\ud800-\udfff]")
_JSON_DELIMITERS = '{}[],:"'
_JSON_DECODER = json.JSONDecoder()
_STRICT_COMPACT_JSON = json.JSONEncoder(
    ensure_ascii=False, separators=(",", ":"), allow_nan=False
)


def _argument_snapshot(value: object) -> tuple[object, bool]:
    """Decode complete objects; distinguish deltas that cannot be cumulative."""
    if not isinstance(value, str):
        return value, True
    start = len(value) - len(value.lstrip(" \t\r\n"))
    if not value.startswith("{", start):
        return value, True
    if _utf8_size(value) > _MAX_TOOL_ARGUMENT_BYTES:
        raise ValueError("Tool arguments exceed configured size limit")
    if value.rstrip(" \t\r\n").endswith("}"):
        try:
            snapshot, end = _JSON_DECODER.raw_decode(value, start)
        except ValueError:
            pass
        else:
            if end == len(value) or not value[end:].strip(" \t\r\n"):
                return snapshot, True
            # The root object has already closed. Extra tokens cannot extend
            # a cumulative snapshot, even if the text begins like the prefix.
            return value, False
    return value, True


def _matches_signed_prefix(fragments: list[str], snapshot: str) -> bool:
    """Compare to compact JSON without interpreting escapes outside strings."""
    prefix = "".join(fragments)
    if snapshot.startswith(prefix) and "\\" not in prefix:
        return bool(prefix)

    # A synthetic closing quote lets the JSON decoder read a trailing partial
    # string, but cannot repair a dangling backslash or truncated Unicode escape.
    limit = len(prefix)
    prefix += '"'
    i = offset = 0
    while i < limit:
        char = prefix[i]
        if char in " \t\r\n":
            # Whitespace is optional between tokens, never inside e.g. 1 2.
            if (
                0 < offset < len(snapshot)
                and snapshot[offset - 1] not in _JSON_DELIMITERS
                and snapshot[offset] not in _JSON_DELIMITERS
            ):
                return False
            i += 1
        elif char == '"':
            try:
                decoded, i = _JSON_DECODER.raw_decode(prefix, i)
            except ValueError:
                return False
            if _SURROGATE.search(decoded):
                return False
            token = _STRICT_COMPACT_JSON.encode(decoded)
            if i > limit:
                return snapshot.startswith(token[:-1], offset)
            if not snapshot.startswith(token, offset):
                return False
            offset += len(token)
        elif offset < len(snapshot) and char == snapshot[offset]:
            i += 1
            offset += 1
        else:
            return False
    return offset > 0


def _finite_argument_number(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON argument")
    return value


def _unique_argument_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("Duplicate JSON argument key")
    return obj


_LITERAL_DECODER = json.JSONDecoder(
    parse_float=_finite_argument_number,
    parse_constant=_finite_argument_number,
    object_pairs_hook=_unique_argument_keys,
)


def _fragment_object(
    fragments: list[str], *, literal: bool = False
) -> dict[str, object] | None:
    raw = "".join(fragments)
    try:
        value = (_LITERAL_DECODER if literal else _JSON_DECODER).decode(raw)
        if (
            literal
            and "\\u" in raw
            and _SURROGATE.search(_STRICT_COMPACT_JSON.encode(value))
        ):
            return None
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(slots=True)
class _LiteralArguments:
    """One append-only interpretation of an explicitly identified wire call."""

    fragments: list[str]
    size: int
    signature: object
    signature_size: int


@dataclass(slots=True, eq=False)
class _ToolCall:
    call_id: str
    envelope: dict[str, object]
    name: str = ""
    args: dict[str, object] | None = None
    fragments: list[str] = field(default_factory=list)
    size: int = 0
    invalid: bool = False
    literal_only: bool = False
    explicit_chain: bool = True
    needs_signed_snapshot: bool = False
    wire_prefix: bool = False
    literal: _LiteralArguments | None = None

    @property
    def retained_size(self) -> int:
        return self.size + (
            self.literal.size + self.literal.signature_size
            if self.literal is not None
            else 0
        )

    def matches(self, name: str) -> bool:
        return self.name.startswith(name) or name.startswith(self.name)

    def awaiting_args(self) -> bool:
        if self.invalid or self.literal_only or self.args is not None:
            return False
        if not self.fragments or not self.fragments[-1].rstrip().endswith("}"):
            return True
        return self.complete_args() is None

    def matches_argument_prefix(self, snapshot: str) -> bool:
        """Check whether an object snapshot can complete the existing fragments."""
        if not self.fragments or not snapshot:
            return False
        quoted = escaped = False
        offset = 0
        for fragment in self.fragments:
            for char in fragment:
                if not quoted and char in " \t\r\n":
                    continue
                if offset >= len(snapshot) or char != snapshot[offset]:
                    return False
                offset += 1
                if escaped:
                    escaped = False
                elif quoted and char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = not quoted
        return offset > 0

    def _can_grow_name(self, args: object) -> bool:
        if (
            self.invalid
            or self.literal_only
            or self.args is not None
            or isinstance(args, dict)
        ):
            return False
        if not self.fragments or (isinstance(args, str) and args.strip()):
            # A nonempty suffix appended to a completed object could not form
            # another valid object. Normal argument validation still applies.
            return True
        # A name-only update must not borrow an already complete argument
        # object. On an ambiguous tail, fail closed rather than rescan history.
        tail = self.fragments[-1].rstrip()
        return bool(tail) and not tail.endswith("}")

    def _update_identity(
        self,
        call_id: str,
        name: str,
        args: object,
        signature: object,
        same_envelope: bool,
    ) -> object:
        """Return only a new signature attributable to the resulting call."""
        self.explicit_chain &= bool(
            call_id and name and (not self.name or self.name == name)
        )
        stable_id = bool(call_id and call_id == self.call_id)
        had_args = self.args is not None or bool(self.fragments)
        if (call_id and not self.call_id) or (name and not self.name and had_args):
            # A newly supplied ID/name can identify an unfinished call, not
            # a previous anonymous shard's proof.
            self.envelope.pop("thoughtSignature", None)
            self.needs_signed_snapshot = True
        if self.name and name and name != self.name:
            self.envelope.pop("thoughtSignature", None)
            if not same_envelope or not self.matches(name):
                self.invalid = True
                return None
            if not (stable_id and isinstance(args, dict)):
                if (
                    call_id != self.call_id
                    or not name.startswith(self.name)
                    or not self._can_grow_name(args)
                ):
                    self.invalid = True
                    return None
                self.needs_signed_snapshot = True
        if name:
            # A complete snapshot supplies its own exact name, not the longest
            # name ever observed for the ID.
            self.name = name
        if self.needs_signed_snapshot:
            if not (call_id and name and isinstance(args, dict) and same_envelope):
                return None
            self.needs_signed_snapshot = False
        if not same_envelope or not signature:
            return None
        if args is None or (isinstance(args, str) and not args.strip()):
            return signature if name and not had_args else None
        if not name and not (stable_id and self.fragments and isinstance(args, str)):
            # Only a sparse suffix to an existing stable chain has an owner;
            # a differently scoped proof cannot sign a borrowed name.
            return None
        return signature

    def update(
        self,
        call_id: str,
        name: str,
        args: object,
        signature: object,
        *,
        same_envelope: bool,
        may_be_cumulative: bool,
        wire_text: str | None,
    ) -> None:
        exact_owner = bool(
            wire_text is not None
            and self.explicit_chain
            and not self.invalid
            and not self.needs_signed_snapshot
            and same_envelope
            and call_id
            and call_id == self.call_id
            and name
            and (not self.name or name == self.name)
        )
        new_size = _utf8_size(args) if isinstance(args, str) else 0
        cumulative = (
            isinstance(args, str)
            and may_be_cumulative
            and bool(self.fragments)
            and new_size >= self.size
            and args.startswith(self.fragments[0])
            and args.startswith("".join(self.fragments))
        )
        if not exact_owner:
            self.literal = None
            self.wire_prefix = False
        elif wire_text is not None:
            # A complete object or array-shaped string can itself be a value
            # inside an open object; even a repeated opener can be a delta.
            # Fork only where the normal snapshot rule would lose that text.
            if (
                self.literal is None
                and self.wire_prefix
                and not self.literal_only
                and self.fragments
                and (isinstance(args, (dict, list)) or cumulative)
                and self.fragments[0].lstrip(" \t\r\n").startswith("{")
            ):
                marker = self.envelope.get("thoughtSignature")
                self.literal = _LiteralArguments(
                    self.fragments.copy(),
                    self.size,
                    marker,
                    _retained_size(marker) if marker else 0,
                )
            if self.literal is not None:
                self.literal.size += (
                    new_size if wire_text is args else _utf8_size(wire_text)
                )
                self.literal.fragments.append(wire_text)
        signature = self._update_identity(call_id, name, args, signature, same_envelope)
        if isinstance(args, dict):
            try:
                snapshot = _STRICT_COMPACT_JSON.encode(args)
            except ValueError:
                # NaN/Infinity aren't JSON. Keep the existing argument handling
                # but never attach a proof to such a snapshot.
                self.envelope.pop("thoughtSignature", None)
                signature = None
                snapshot = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
            if "thoughtSignature" in self.envelope:
                if self.args is not None:
                    # Python equality merges JSON types such as true and 1.
                    changed = args != self.args or json.dumps(
                        args, sort_keys=True
                    ) != json.dumps(self.args, sort_keys=True)
                elif self.fragments and self.explicit_chain:
                    changed = not _matches_signed_prefix(self.fragments, snapshot)
                else:
                    changed = bool(self.fragments) and not self.matches_argument_prefix(
                        snapshot
                    )
                if changed:
                    self.envelope.pop("thoughtSignature")
            self.size = _utf8_size(snapshot)
            self.args = args
            self.fragments.clear()
            self.wire_prefix = False
        elif isinstance(args, str) and args and (self.fragments or args.strip()):
            if self.args is not None:
                self.envelope.pop("thoughtSignature", None)
                self.args = None
                self.size = 0
            if cumulative:
                self.fragments.clear()
                self.size = 0
            if not self.fragments:
                self.wire_prefix = exact_owner
            self.size += new_size
            if len(self.fragments) >= _MAX_TOOL_ARGUMENT_FRAGMENTS:
                raise ValueError(
                    "Tool argument fragment count exceeds configured size limit"
                )
            self.fragments.append(args)
        elif args is not None and not isinstance(args, str):
            if isinstance(args, list) and self.literal is not None:
                # Invalid as root arguments. Only the already established
                # literal chain may prove this positional array was a value.
                self.literal_only = True
            else:
                self.invalid = True
        if self.size > _MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("Tool arguments exceed configured size limit")
        if self.literal is not None and (
            self.literal.size + self.size > _MAX_TOOL_ARGUMENT_BYTES
            or len(self.literal.fragments) + len(self.fragments)
            > _MAX_TOOL_ARGUMENT_FRAGMENTS
        ):
            # The optional interpretation must not displace a valid normal
            # snapshot, or expand the existing per-call retention limits.
            self.literal = None
        if isinstance(signature, str) and signature:
            self.envelope["thoughtSignature"] = signature

    def complete_args(self) -> dict[str, object] | None:
        if self.invalid or self.literal_only:
            return None
        if self.args is not None:
            return self.args
        if not self.fragments:
            return {}
        return _fragment_object(self.fragments)

    def part(self, *, synthesize_ids: bool) -> dict[str, object] | None:
        args = self.complete_args()
        envelope = self.envelope
        if (
            not self.invalid
            and self.literal is not None
            and (literal_args := _fragment_object(self.literal.fragments, literal=True))
            is not None
        ):
            # Never choose between two complete interpretations. An inner
            # standalone snapshot's proof cannot sign an augmented outer call.
            if args is not None:
                return None
            args = literal_args
            envelope = _without_call(self.envelope)
            if self.literal.signature:
                envelope["thoughtSignature"] = self.literal.signature
        if not self.name or args is None:
            return None
        call: dict[str, object] = {"name": self.name, "args": args}
        if self.call_id:
            call["id"] = self.call_id
        elif synthesize_ids:
            call["id"] = f"call_{secrets.token_hex(12)}"
        return {**envelope, "functionCall": call}


def has_response_content(part: dict[str, object]) -> bool:
    return bool(
        part.get("text")
        or part.get("inlineData")
        or part.get("functionCall")
        or part.get("functionResponse")
        or part.get("executableCode")
        or part.get("codeExecutionResult")
    )


def part_payload_signature(part: object, payload_key: str) -> str:
    """A projection can carry the signature only for the whole owning Part."""

    def get(key: str) -> object:
        return part.get(key) if isinstance(part, dict) else getattr(part, key, None)

    signature = get("thoughtSignature")
    if not isinstance(signature, str) or not signature:
        return ""
    if payload_key == "functionCall" and get("thought") is True:
        return ""
    if get(payload_key) is None or any(
        key != payload_key and get(key) is not None
        for key in (
            "text",
            "inlineData",
            "fileData",
            "functionCall",
            "functionResponse",
            "executableCode",
            "codeExecutionResult",
        )
    ):
        return ""
    return signature


def _without_call(part: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in part.items()
        if key not in {"functionCall", "thoughtSignature"}
    }


def _utf8_size(value: str) -> int:
    return len(value) if value.isascii() else len(value.encode("utf-8"))


def _retained_size(value: object) -> int:
    if isinstance(value, str):
        return _utf8_size(value) + 2
    if isinstance(value, dict):
        return 2 + sum(
            _retained_size(key) + _retained_size(entry) + 2
            for key, entry in value.items()
        )
    if isinstance(value, list):
        return 2 + sum(_retained_size(entry) + 1 for entry in value)
    return 16


class ToolResponseAssembler:
    """Hold executable calls and the suffix they precede until confirmed STOP.

    Plain leading content remains live. A stable ID anchors the first Part's
    position across snapshots; anonymous shards join only an unambiguous
    pending chain. Detached signature carriers are ordinary ordered Parts,
    never implicit updates to the last tool.
    """

    def __init__(self, *, synthesize_ids: bool = False) -> None:
        self.synthesize_ids = synthesize_ids
        self._ordered: list[dict[str, object] | _ToolCall] = []
        self._by_id: dict[str, _ToolCall] = {}
        self._recent: list[_ToolCall] = []
        self._calls: list[_ToolCall] = []
        self._retained_bytes = 0
        self._argument_bytes = 0
        self._delivered = False
        self._interrupted = False

    def block_tools(self) -> list[dict[str, object]]:
        kept, _ = self.finish("OTHER")
        self._ordered.clear()
        self._by_id.clear()
        self._recent.clear()
        self._calls.clear()
        self._retained_bytes = self._argument_bytes = 0
        self._interrupted = True
        return kept

    def _within_byte_budget(self) -> bool:
        if self._argument_bytes + self._retained_bytes > _MAX_TOOL_RESPONSE_BYTES:
            # Reclaim optional branches before rejecting ordinary snapshots.
            for pending in self._calls:
                self._argument_bytes -= pending.retained_size - pending.size
                pending.literal = None
        return self._argument_bytes + self._retained_bytes <= _MAX_TOOL_RESPONSE_BYTES

    def _retain(self, part: dict[str, object] | _ToolCall, size: int) -> None:
        self._retained_bytes += size
        if len(self._ordered) >= 16_384 or not self._within_byte_budget():
            raise ValueError("Ordered tool response exceeds configured size limit")
        self._ordered.append(part)

    def feed(self, parts: list[dict[str, object]]) -> list[dict[str, object]]:
        leading: list[dict[str, object]] = []
        seen: set[_ToolCall] = set()
        for part in parts:
            raw = part.get("functionCall")
            if self._interrupted and isinstance(raw, dict):
                part = _without_call(part)
                raw = None
                if not has_response_content(part) and "text" not in part:
                    continue
            if not isinstance(raw, dict):
                if has_response_content(part):
                    self._recent.clear()
                if self._ordered:
                    self._retain(part, _retained_size(part))
                else:
                    self._delivered |= has_response_content(part)
                    leading.append(part)
                continue

            raw_id, raw_name = raw.get("id"), raw.get("name")
            call_id = raw_id if isinstance(raw_id, str) else ""
            name = raw_name if isinstance(raw_name, str) else ""
            args, may_be_cumulative = _argument_snapshot(raw.get("args"))
            signature = part.get("thoughtSignature")
            # Complete named objects are independent unless there is an
            # identifiable chain of incomplete argument shards to replace.
            tool = self._by_id.get(call_id) if call_id else None
            if tool is None:
                snapshot = (
                    json.dumps(args, ensure_ascii=False, separators=(",", ":"))
                    if not call_id
                    and name
                    and isinstance(args, dict)
                    and any(pending.fragments for pending in self._recent)
                    else ""
                )
                matches = [
                    pending
                    for pending in self._recent
                    if pending not in seen
                    and (not call_id or not pending.call_id)
                    and (
                        call_id
                        or not name
                        or not isinstance(args, dict)
                        or (
                            not pending.call_id
                            and pending.name == name
                            and pending.matches_argument_prefix(snapshot)
                        )
                    )
                    and pending.matches(name)
                    and (
                        pending.awaiting_args()
                        or (not name and args is None)
                        or not pending.name
                    )
                ]
                if len(matches) == 1:
                    tool = matches[0]
                elif len(matches) > 1:
                    # No identity information makes this update unsafe to
                    # assign. Do not guess among parallel calls.
                    for pending in matches:
                        pending.invalid = True
            same_envelope = True
            if tool is None:
                if len(self._calls) >= 256:
                    raise ValueError("Too many tool calls in one response")
                envelope = _without_call(part)
                tool = _ToolCall(call_id, envelope)
                self._calls.append(tool)
                self._recent.append(tool)
                self._retain(tool, _retained_size(envelope) + _utf8_size(call_id))
            else:
                caption = _without_call(part)
                if has_response_content(caption) or "text" in caption:
                    self._retain(caption, _retained_size(caption))
                same_envelope = caption == _without_call(tool.envelope)
            seen.add(tool)
            if call_id:
                self._by_id[call_id] = tool
            old_size = tool.retained_size
            old_fields_size = _retained_size(tool.name) + _retained_size(
                tool.envelope.get("thoughtSignature")
            )
            tool.update(
                call_id,
                name,
                args,
                signature,
                same_envelope=same_envelope,
                may_be_cumulative=may_be_cumulative,
                wire_text=raw.argument_text
                if isinstance(raw, PositionalTextCall)
                else None,
            )
            if call_id and not tool.call_id:
                tool.call_id = call_id
                self._retained_bytes += _utf8_size(call_id)
            self._retained_bytes += (
                _retained_size(tool.name)
                + _retained_size(tool.envelope.get("thoughtSignature"))
                - old_fields_size
            )
            self._argument_bytes += tool.retained_size - old_size
            if not self._within_byte_budget():
                raise ValueError(
                    "Total tool arguments and ordered response exceed configured size limit"
                )
        return leading

    def finish(
        self, reason: str, *, error_on_empty_invalid: bool = False
    ) -> tuple[list[dict[str, object]], str]:
        calls: dict[_ToolCall, dict[str, object]] = {}
        invalid: _ToolCall | None = None
        if reason == "STOP":
            for tool in self._calls:
                part = tool.part(synthesize_ids=self.synthesize_ids)
                if part is None:
                    invalid = tool
                    reason = "MALFORMED_FUNCTION_CALL"
                    break
                calls[tool] = part
        kept: list[dict[str, object]] = []
        for value in self._ordered:
            if isinstance(value, _ToolCall):
                if reason == "STOP":
                    kept.append(calls[value])
                else:
                    caption = _without_call(value.envelope)
                    if has_response_content(caption) or "text" in caption:
                        kept.append(caption)
            else:
                kept.append(value)
        if invalid is not None:
            ident = invalid.name or invalid.call_id or "unnamed call"
            if error_on_empty_invalid and not (
                self._delivered or any(has_response_content(part) for part in kept)
            ):
                raise ValueError(f"Incomplete tool arguments for {ident}")
            logger.warning(
                "Incomplete tool arguments for %s; tool batch discarded", ident
            )
        return kept, reason


async def coalesce_tool_frames(
    source: AsyncIterable[dict[str, object]],
) -> AsyncGenerator[dict[str, object], None]:
    """Normalize Gemini-compatible SSE input not produced by the local gateway."""
    assembler = ToolResponseAssembler(synthesize_ids=True)
    terminal: dict[str, object] = {}
    reason = "OTHER"
    async for frame in source:
        if "error" in frame:
            kept, _ = assembler.finish("OTHER")
            # The retained display Parts no longer need the assembly state.
            del assembler
            if kept:
                yield {"candidates": [{"content": {"parts": kept}}]}
            yield frame
            return
        candidates = frame.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        if not isinstance(candidate, dict):
            yield frame
            continue
        content = candidate.get("content")
        raw_parts = content.get("parts", []) if isinstance(content, dict) else []
        parts = (
            [p for p in raw_parts if isinstance(p, dict)]
            if isinstance(raw_parts, list)
            else []
        )
        finish = candidate.get("finishReason")
        leading = []
        if isinstance(finish, str) and finish:
            reason = finish
            if finish != "STOP":
                leading = assembler.block_tools()
            terminal = {
                key: value for key, value in candidate.items() if key != "content"
            }
        elif any(
            has_response_content(part) or "functionCall" in part for part in parts
        ):
            reason = "OTHER"
            terminal = {}
        leading.extend(assembler.feed(parts))
        if leading or not parts or any(key != "candidates" for key in frame):
            yield {
                **frame,
                "candidates": [
                    {
                        **{
                            key: value
                            for key, value in candidate.items()
                            if key != "finishReason"
                        },
                        "content": {
                            **(content if isinstance(content, dict) else {}),
                            "parts": leading,
                        },
                    }
                ],
            }
    kept, reason = assembler.finish(reason, error_on_empty_invalid=True)
    # The terminal frame owns its Parts; don't keep assembly state while
    # waiting for the client to read it.
    del assembler
    yield {
        "candidates": [{**terminal, "content": {"parts": kept}, "finishReason": reason}]
    }
