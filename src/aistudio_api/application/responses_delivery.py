"""Small delivery cursor for the Responses SSE encoder and its outer ASGI sender."""

from collections import deque
from enum import Enum, auto


class PartStage(Enum):
    ABSENT = auto()
    OPEN = auto()
    TEXT_DONE = auto()
    PART_DONE = auto()


class ResponsesStreamDelivery:
    """Count only accepted semantic frames. Never retain a payload or wire frame.

    The encoder knows the character length of a delta without parsing the JSON
    back out of the socket write. Keep only those lengths between encoding and
    the matching accepted send; the streaming transport provides backpressure.
    """

    def __init__(self) -> None:
        self.sequence = -1
        self.output_index = -1
        self.last = b""
        self.characters = 0
        self._delta_lengths: deque[int] = deque()
        self._stopped = False

    def encoded_delta(self, text: str) -> None:
        if not self._stopped:
            self._delta_lengths.append(len(text))

    def accept(self, frame: bytes) -> None:
        if self._stopped:
            return
        # The gateway emits one semantic event per write. Only read its short
        # header, never decode or duplicate a potentially large data payload.
        end = frame.find(b"\n", 7, 96)
        if end < 0:
            return
        self.last = frame[7:end]
        self.sequence += 1
        if self.last == b"response.output_item.added":
            self.output_index += 1
            self.characters = 0
        elif self.last.endswith(b".delta"):
            self.characters += self._delta_lengths.popleft()

    def begin_failure(self) -> bool:
        if self._stopped:
            return False
        self._stopped = True
        self._delta_lengths.clear()
        return True

    def part_stage(self) -> PartStage:
        if self.last == b"response.output_item.added":
            return PartStage.ABSENT
        if self.last in (
            b"response.output_text.done",
            b"response.reasoning_summary_text.done",
        ):
            return PartStage.TEXT_DONE
        if self.last in (
            b"response.content_part.done",
            b"response.reasoning_summary_part.done",
        ):
            return PartStage.PART_DONE
        return PartStage.OPEN
