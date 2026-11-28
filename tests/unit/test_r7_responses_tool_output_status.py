"""Only finished function results belong in replayed Responses history."""

import pytest

from aistudio_api.api.schemas_compat import OpenAIResponsesRequest
from aistudio_api.application.responses_service import responses_to_gemini_request


@pytest.mark.parametrize("draft_status", ["in_progress", "incomplete"])
@pytest.mark.parametrize(
    "final",
    [None, {"output": "9 am"}, {"output": "9 am", "status": "completed"}],
    ids=["draft-only", "legacy-final", "completed-final"],
)
async def test_tool_output_status_is_independent_of_completed_call(
    draft_status: str, final: dict[str, str] | None
) -> None:
    result = {"type": "function_call_output", "call_id": "hours-1"}
    history: list[dict[str, object]] = [
        {"role": "user", "content": "When does the museum open?"},
        {
            "type": "function_call",
            "name": "hours",
            "arguments": "{}",
            "call_id": "hours-1",
            "status": "completed",
        },
        {**result, "output": "still checking", "status": draft_status},
    ]
    if final is not None:
        history.append({**result, **final})

    native, _ = await responses_to_gemini_request(
        OpenAIResponsesRequest(model="gemini-3.8-flash", input=history)
    )

    prompt, call, *outputs = native.contents
    assert prompt.parts[0].text == "When does the museum open?"
    assert call.parts[0].functionCall is not None
    assert call.parts[0].functionCall.id == "hours-1"
    if final is None:
        assert not outputs
    else:
        (turn,) = outputs
        (part,) = turn.parts
        assert turn.role == "user"
        assert part.functionResponse is not None
        assert part.functionResponse.id == "hours-1"
        assert part.functionResponse.name == "hours"
        assert part.functionResponse.response == {"result": "9 am"}
