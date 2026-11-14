"""Shared protocol state for tool calls and stop sequences."""

from __future__ import annotations

import json
import re
import secrets
from array import array
from collections.abc import AsyncGenerator, AsyncIterable, Generator
from dataclasses import dataclass, field

_MAX_TOOL_ARGUMENT_BYTES = 16 * 1024 * 1024
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
        self._size += len(text.encode("utf-8"))
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
        matches = [
            (text.find(value), value) for value in self.sequences if value in text
        ]
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


@dataclass
class _ToolCall:
    call_id: str
    name: str
    args: dict[str, object] | None = None
    fragments: list[str] = field(default_factory=list)
    size: int = 0
    signature: str = ""

    def update(self, call: dict[str, object], signature: str) -> None:
        name = call.get("name")
        if isinstance(name, str) and len(name) >= len(self.name):
            self.name = name
        if signature:
            self.signature = signature
        args = call.get("args")
        if isinstance(args, dict):
            self.size = len(
                json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            if self.size > _MAX_TOOL_ARGUMENT_BYTES:
                raise ValueError("Tool arguments exceed configured size limit")
            self.args = args
            self.fragments.clear()
        elif isinstance(args, str):
            if self.args is not None:
                if not args.strip():
                    return  # Empty/default updates carry no replacement arguments.
                # An explicit-ID call can change representations while pending.
                # The latest snapshot/shards replace an earlier object; keeping
                # the object would execute stale arguments after a new update.
                self.args = None
                self.fragments.clear()
                self.size = 0
            fragment_size = len(args.encode("utf-8"))
            if (
                self.fragments
                and fragment_size >= self.size
                and args.startswith(self.fragments[0])
            ):
                previous = "".join(self.fragments)
                if args.startswith(previous):
                    self.fragments.clear()
                    self.size = 0
            self.size += fragment_size
            if self.size > _MAX_TOOL_ARGUMENT_BYTES:
                raise ValueError("Tool arguments exceed configured size limit")
            if not self.fragments:
                try:
                    parsed = json.loads(args)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    # Complete JSON objects are calls/snapshots, rather than
                    # argument shards belonging to the next ID-less call.
                    self.args = parsed
                    return
            self.fragments.append(args)
        elif args is not None and not isinstance(args, (dict, str)):
            raise ValueError(
                "Tool arguments must be an object or JSON object fragments"
            )

    def part(self) -> dict[str, object]:
        args = self.args
        if args is None:
            raw = "".join(self.fragments)
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError as exc:
                raise ValueError(f"Incomplete tool arguments for {self.name}") from exc
            if not isinstance(args, dict):
                raise ValueError(f"Tool arguments must be an object: {self.name}")
        part: dict[str, object] = {
            "functionCall": {
                "id": self.call_id,
                "name": self.name or "unknown",
                "args": args,
            }
        }
        if self.signature:
            part["thoughtSignature"] = self.signature
        return part


def _separate_part_payloads(parts: list[object]) -> list[object]:
    """Keep captions before media/calls when an upstream part combines fields."""
    separated: list[object] | None = None
    payload_fields = ("text", "inlineData", "functionCall")
    for index, part in enumerate(parts):
        if (
            isinstance(part, dict)
            and "functionCall" not in part
            and sum(part.get(key) is not None for key in payload_fields) > 1
        ):
            if separated is None:
                separated = list(parts[:index])
            metadata = {
                key: value for key, value in part.items() if key not in payload_fields
            }
            separated.extend(
                {**metadata, key: part[key]}
                for key in payload_fields
                if part.get(key) is not None
            )
        elif separated is not None:
            separated.append(part)
    return parts if separated is None else separated


async def coalesce_tool_frames(
    source: AsyncIterable[dict[str, object]],
) -> AsyncGenerator[dict[str, object], None]:
    """Merge explicit-ID snapshots/shards; preserve independent ID-less calls.

    Calls close at a content boundary, finish or EOF. This avoids exposing a
    partial JSON argument as an executable call. Plain text frames pass through.
    """
    pending: list[_ToolCall] = []
    by_id: dict[str, _ToolCall] = {}
    sent: dict[str, dict[str, object]] = {}
    total_size = 0

    def flush(*, before: _ToolCall | None = None) -> dict[str, object] | None:
        parts: list[dict[str, object]] = []
        count = pending.index(before) if before is not None else len(pending)
        ready_tools = pending[:count]
        del pending[:count]
        for tool in ready_tools:
            by_id.pop(tool.call_id, None)
            part = tool.part()
            previous = sent.get(tool.call_id)
            if previous is not None:
                if previous.get("functionCall") != part.get("functionCall"):
                    raise ValueError("Upstream changed an already completed tool call")
                continue
            sent[tool.call_id] = part
            parts.append(part)
        return {"candidates": [{"content": {"parts": parts}}]} if parts else None

    async for frame in source:
        if "error" in frame:
            # Never flush uncompleted tool calls after an upstream failure.
            yield frame
            return
        candidates = frame.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        if not isinstance(candidate, dict):
            yield frame
            continue
        content = candidate.get("content")
        parts = content.get("parts", []) if isinstance(content, dict) else []
        if not isinstance(parts, list):
            parts = []
        parts = _separate_part_payloads(parts)
        # The fast path yields the frame directly; include any separated fields
        # there as well, before interrupted calls are filtered below.
        if isinstance(content, dict) and parts is not content.get("parts"):
            candidate = {**candidate, "content": {**content, "parts": parts}}
            frame = {**frame, "candidates": [candidate]}
        interrupted = (
            bool(candidate.get("finishReason")) and candidate["finishReason"] != "STOP"
        )
        if interrupted:
            # Decide before any content boundary can flush pending calls.
            total_size -= sum(tool.size for tool in pending if tool.call_id not in sent)
            pending.clear()
            by_id.clear()
            parts = _separate_part_payloads(
                [
                    {key: value for key, value in part.items() if key != "functionCall"}
                    if isinstance(part, dict) and "functionCall" in part
                    else part
                    for part in parts
                    if not isinstance(part, dict)
                    or "functionCall" not in part
                    or part.get("text") is not None
                    or part.get("inlineData") is not None
                ]
            )
            candidate = {**candidate, "content": {"parts": parts}}
            frame = {**frame, "candidates": [candidate]}
        if not pending and not any(
            isinstance(p, dict) and "functionCall" in p for p in parts
        ):
            yield frame
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            call = part.get("functionCall")
            if isinstance(call, dict):
                raw_id = call.get("id")
                call_id = raw_id if isinstance(raw_id, str) else ""
                name = str(call.get("name") or "")
                tool = by_id.get(call_id) if call_id else None
                # A name/argument shard without an ID may extend the last
                # incomplete call. Complete object arguments denote a new call.
                if tool is None and not call_id and pending:
                    last = pending[-1]
                    if last.args is None and (
                        last.name.startswith(name) or name.startswith(last.name)
                    ):
                        tool = last
                if tool is None:
                    if len(pending) + len(sent) >= 256:
                        raise ValueError("Too many tool calls in one response")
                    tool = _ToolCall(call_id or f"call_{secrets.token_hex(12)}", name)
                    pending.append(tool)
                    if call_id:
                        by_id[call_id] = tool
                sig = part.get("thoughtSignature")
                previous_size = tool.size
                tool.update(call, sig if isinstance(sig, str) else "")
                if tool.call_id not in sent:
                    total_size += tool.size - previous_size
                    if total_size > _MAX_TOOL_RESPONSE_BYTES:
                        raise ValueError(
                            "Total tool arguments exceed configured response size limit"
                        )
                if part.get("text") is not None or part.get("inlineData") is not None:
                    # A caption accompanying a shard is before this call, not a
                    # boundary forcing its incomplete arguments to execute.
                    ready = flush(before=tool)
                    if ready:
                        yield ready
                    accompanying = {
                        key: value
                        for key, value in part.items()
                        if key != "functionCall"
                    }
                    yield {
                        "candidates": [
                            {
                                "content": {
                                    "parts": _separate_part_payloads([accompanying])
                                }
                            }
                        ]
                    }
            else:
                if pending and set(part) == {"thoughtSignature"}:
                    signature = part.get("thoughtSignature")
                    if isinstance(signature, str):
                        pending[-1].signature = signature
                    continue
                ready = flush()
                if ready:
                    yield ready
                yield {"candidates": [{"content": {"parts": [part]}}]}
        if candidate.get("finishReason"):
            if candidate["finishReason"] == "STOP":
                ready = flush()
                if ready:
                    yield ready
            else:
                # An interrupted/blocked call must never become executable.
                pending.clear()
                by_id.clear()
        # Preserve usage, response ID and finish metadata without replaying parts.
        yield {**frame, "candidates": [{**candidate, "content": {"parts": []}}]}
    ready = flush()
    if ready:
        yield ready
