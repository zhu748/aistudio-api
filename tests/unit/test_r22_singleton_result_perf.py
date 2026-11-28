"""An explicit one-to-one result doesn't need the batch matcher's ID counts."""

import pytest

from aistudio_api.api.schemas import (
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiPart,
)
from aistudio_api.application import chat_service
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


def _result(call_id: str | None, name: str = "read") -> GeminiPart:
    return GeminiPart(functionResponse=GeminiFunctionResponse(id=call_id, name=name))


def test_exact_singletons_skip_batch_counts_and_preserve_wire(monkeypatch):
    def unexpected_count(*args, **kwargs):
        pytest.fail("a single exact pair does not need batch ID counts")

    monkeypatch.setattr(chat_service, "Counter", unexpected_count)
    contents = []
    expected = []
    for i, label in enumerate(("read", "display label", "")):
        call_id = f"sample-{i}"
        args = {"index": i}
        payload = {"rows": [{"id": "data, not an owner", "label": "青"}]}
        contents.extend(
            [
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"id": call_id, "name": "read", "args": args}}
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": call_id,
                                "name": label,
                                "response": payload,
                                "parts": [{"text": "attachment"}],
                            }
                        }
                    ],
                },
            ]
        )
        expected.extend(
            [
                [AistudioPart(function_call=("read", args, call_id)).to_wire()],
                [
                    AistudioPart(
                        function_response=("read", payload, call_id)
                    ).to_wire(),
                    AistudioPart(text="attachment").to_wire(),
                ],
            ]
        )
    request = GeminiGenerateContentRequest.model_validate(
        {"tools": [], "contents": contents}
    )
    # Compatibility hints restrict guesses, not an exact ID in the current turn.
    for model, user in zip(request.contents[::2], request.contents[1::2], strict=True):
        model._compat_owner.legacy_name = "read"
        result = user.parts[0].functionResponse
        assert result is not None
        result._compat_owner.legacy_name = "read"
        result._compat_owner.exact_id_only = True
    public_replay = GeminiGenerateContentRequest.model_validate_json(
        request.model_dump_json()
    )
    for replay in (request, public_replay):
        normalized = chat_service.normalize_gemini_request(
            replay, "gemini-3.1-pro-preview"
        )
        assert [
            [p.to_wire() for p in turn.parts] for turn in normalized.contents
        ] == expected


@pytest.mark.parametrize(
    ("pending", "parts", "owners"),
    [
        ([], [_result("A")], {}),
        ([("A", "read")], [GeminiPart(text="no result")], {}),
        ([(None, "read")], [_result(None, "different")], {}),
        ([("", "read")], [_result("", "different")], {}),
        ([("A", "read")], [_result(None)], {0: ("A", "read")}),
        ([(None, "read")], [_result("B")], {0: ("B", "read")}),
        ([("A", "read")], [_result("B")], {}),
        # Neither a nested result's ID nor another operation's ID owns this result.
        (
            [("A", "read")],
            [
                GeminiPart.model_validate(
                    {
                        "functionResponse": {
                            "id": "B",
                            "name": "read",
                            "parts": [{"functionResponse": {"id": "A"}}],
                        },
                        "codeExecutionResult": {
                            "id": "A",
                            "outcome": "OUTCOME_OK",
                            "output": "independent",
                        },
                    }
                )
            ],
            {},
        ),
        (
            [("A", "read")],
            [GeminiPart(text="before"), _result("A")],
            {1: ("A", "read")},
        ),
        ([("A", "read")], [_result("A"), _result("A")], {}),
        ([("A", "read"), ("A", "other")], [_result("A")], {}),
        (
            [("A", "read"), ("B", "read")],
            [_result("B"), _result("A")],
            {0: ("B", "read"), 1: ("A", "read")},
        ),
    ],
)
def test_non_singletons_and_unreliable_ids_keep_general_ownership(
    pending, parts, owners
):
    assert chat_service._match_pending_calls(parts, pending, set()) == owners
