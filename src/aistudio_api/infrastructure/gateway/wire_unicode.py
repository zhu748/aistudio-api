"""Validate JSON Unicode escapes at the strictly decoded upstream boundary."""

from __future__ import annotations

import re

from aistudio_api.domain.errors import TransportError

_SURROGATE_ESCAPE = re.compile(r"\\u[dD]([89a-fA-F])[0-9a-fA-F]{2}")
# A high surrogate followed by zero to five characters of a possible low
# surrogate escape, at physical EOF. Not a complete pair or an unrelated escape.
INCOMPLETE_SURROGATE_PAIR = re.compile(
    r"\\u[dD][89aAbB][0-9a-fA-F]{2}"
    r"(?:\\|\\u|\\u[dD]|\\u[dD][c-fC-F][0-9a-fA-F]?)?\Z"
)


def is_json_escape(text: str, start: int) -> bool:
    """Is this backslash an escape introducer, rather than literal text?"""
    before = start
    while before and text[before - 1] == "\\":
        before -= 1
    return (start - before) % 2 == 0


def validate_json_surrogates(text: str, *, allow_eof_pair: bool = False) -> None:
    """Reject escapes that JSON would decode to unencodable Python strings.

    Strict UTF-8 bytes cannot carry a surrogate directly. Inspect just these
    escapes, including in keys/metadata, without copying or walking a decoded
    response tree. A literal backslash followed by 'u' is ordinary text.
    """
    if r"\u" not in text:
        return
    high_end = -1
    for match in _SURROGATE_ESCAPE.finditer(text):
        if not is_json_escape(text, match.start()):
            continue
        high = match[1] in "89aAbB"
        if high_end < 0 and high:
            high_end = match.end()
        elif match.start() == high_end and not high:
            high_end = -1
        else:
            raise TransportError("Invalid upstream JSON: unpaired Unicode surrogate")
    if high_end >= 0 and not (
        allow_eof_pair and INCOMPLETE_SURROGATE_PAIR.fullmatch(text, high_end - 6)
    ):
        raise TransportError("Invalid upstream JSON: unpaired Unicode surrogate")
