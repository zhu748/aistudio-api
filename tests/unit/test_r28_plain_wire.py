"""Only the canonical two-slot text part can bypass the general wire codec."""

import pytest

from aistudio_api.domain.models import ResponsePart
from aistudio_api.infrastructure.gateway import wire_parser
from aistudio_api.infrastructure.gateway.wire_spec import JspbArray
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


def test_plain_text_round_trips_without_sparse_or_structural_work(monkeypatch):
    def unexpected(*args):
        pytest.fail("ordinary text has no structural or sparse slots")

    monkeypatch.setattr(JspbArray, "__setitem__", unexpected)
    monkeypatch.setattr(wire_parser, "_coerce_structural_payload", unexpected)
    for text in ("", "model", "user", "assistant", "system", "雪🙂\n"):
        part = AistudioPart(text=text)
        wire = part.to_wire()
        assert type(wire) is list and wire == [None, text]
        assert wire_parser.parse_response_part(wire) == ResponsePart(
            text=text, text_present=True
        )
        wire.clear()
        assert part.to_wire() == [None, text]


def test_list_and_text_subclasses_keep_observable_parser_behavior():
    class MarkAfterReadingLead(list):
        def __getitem__(self, index):
            value = super().__getitem__(index)
            if index == 0:
                self[0] = True
            return value

    class CheckedText(str):
        def __hash__(self):
            raise TypeError("role lookup")

    # The normal parser checks for a content block before reading the thought flag.
    assert wire_parser.parse_response_part(MarkAfterReadingLead([None, "model"])) == (
        ResponsePart(text="model", text_present=True, thought=True)
    )
    with pytest.raises(TypeError, match="role lookup"):
        wire_parser.parse_response_part([None, CheckedText("model")])


def test_noncanonical_compound_and_raw_parts_keep_their_fields_and_errors():
    for nonpart in ([[], "model"], ["assistant", "user"], [None, False], []):
        assert wire_parser.parse_response_part(nonpart) == ResponsePart()
    compound = AistudioPart(
        text="", thought_signature="c2ln", function_call=("read", {"field": "☑"}, "A")
    )
    decoded = wire_parser.parse_response_part(compound.to_wire())
    assert decoded.text_present and decoded.text == ""
    assert decoded.thought_signature == "c2ln"
    assert decoded.function_call is not None
    assert decoded.function_call["call_id"] == "A"
    assert decoded.function_call["thought_signature"] == "c2ln"
    assert decoded.function_call["args"] == {"field": "☑"}

    # A text value must not swallow an error from an earlier, orthogonal field.
    part = AistudioPart(text="ignore", function_call=("\udfff", {}))
    with pytest.raises(UnicodeEncodeError):
        part.to_wire()
    raw: list[object] = [None, "saved", ["opaque"]]
    part.raw_wire = raw
    wire = part.to_wire()
    assert wire == raw and wire is not raw and wire[2] is raw[2]
