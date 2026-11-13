"""Shared protocol state for tool calls and stop sequences."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field


class StopSequenceBuffer:
    """Keep only a suffix which could become a stop sequence in the next frame."""

    def __init__(self, sequences: list[str] | None) -> None:
        self.sequences = [value for value in sequences or [] if value]
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
            for value in self.sequences:
                for length in range(min(len(value) - 1, len(text)), hold, -1):
                    if text.endswith(value[:length]):
                        hold = length
                        break
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
            self.args = args
            self.fragments.clear()
            self.size = 0
        elif isinstance(args, str) and self.args is None:
            if (
                self.fragments
                and len(args) >= self.size
                and args.startswith(self.fragments[0])
            ):
                previous = "".join(self.fragments)
                if args.startswith(previous):
                    self.fragments.clear()
                    self.size = 0
            self.size += len(args)
            if self.size > 16 * 1024 * 1024:
                raise ValueError("Tool arguments exceed 16 MiB limit")
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

    def flush() -> dict[str, object] | None:
        parts: list[dict[str, object]] = []
        for tool in pending:
            part = tool.part()
            previous = sent.get(tool.call_id)
            if previous is not None:
                if previous.get("functionCall") != part.get("functionCall"):
                    raise ValueError("Upstream changed an already completed tool call")
                continue
            sent[tool.call_id] = part
            parts.append(part)
        pending.clear()
        by_id.clear()
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
                tool.update(call, sig if isinstance(sig, str) else "")
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
