"""An ID-less legacy Chat result must not take a current call's identity."""

import json

import pytest
from openai.types.chat import ChatCompletionFunctionMessageParam
from test_r15_result_owners import _assert_replay, _call

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.schemas_compat import OpenAIChatCompletionRequest
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.compat_openai import openai_to_gemini_request
from aistudio_api.domain.tool_signatures import tool_history_text
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_MODEL = "gemini-3.8-flash"
_OLD = {"name": "read", "arguments": ' {"partial":'}


def _legacy_result(name, payload, owner=None):
    message: ChatCompletionFunctionMessageParam = {
        "role": "function",
        "name": name,
        "content": json.dumps(payload, ensure_ascii=False),
    }
    part = (
        AistudioPart(function_response=(name, payload, owner))
        if owner
        else AistudioPart(text=tool_history_text("tool_result", name, None, payload))
    )
    return message, [part]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("layout", ["same", "neighbor", "standalone"])
async def test_deprecated_owner_is_scoped_to_its_pending_model_group(
    monkeypatch, stream, layout
):
    read, a = _call("chat", "read", 1, "A")
    write, b = _call("chat", "write", 2, "B")
    if layout == "same":
        turns = [({**read, "function_call": _OLD}, a), (write, b)]
    elif layout == "neighbor":
        turns = [({**write, "function_call": _OLD}, b), (read, a)]
    else:
        turns = [
            (
                {"role": "assistant", "function_call": _OLD},
                [
                    AistudioPart(
                        text=tool_history_text(
                            "tool_call", "read", None, _OLD["arguments"]
                        )
                    )
                ],
            ),
            (write, b),
            (read, a),
        ]
    exact, exact_parts = _legacy_result("read", {"exact": True}, "D")
    last, d = _call("chat", "read", 4, "D")
    await _assert_replay(
        monkeypatch,
        "chat",
        stream,
        [
            *turns,
            ({"role": "user", "content": ""}, []),
            _legacy_result("read", {"result": "旧", "rows": [1, {"a": "é"}]}),
            _legacy_result("write", {"result": "unrelated fallback"}, "B"),
            _call("chat", "read", 3, "C"),
            _legacy_result("read", {"result": "new turn"}, "C"),
            ({**last, "function_call": _OLD}, d),
            ({**exact, "tool_call_id": "D"}, exact_parts),
        ],
    )


@pytest.mark.parametrize(
    ("legacy", "role", "name", "blocked"),
    [
        ({"name": "unknown", "arguments": ""}, "function", "unknown", True),
        ({"name": "unknown", "arguments": ""}, "function", None, False),
        ({"name": "unknown", "arguments": ""}, "function", "", False),
        ({"name": "unknown", "arguments": ""}, "tool", "unknown", False),
        ({"name": "Unknown", "arguments": ""}, "function", "unknown", False),
        ({"name": "unknown"}, "function", "unknown", False),
    ],
)
async def test_only_literal_names_on_typed_legacy_records_count(
    legacy, role, name, blocked
):
    modern, _ = _call("chat", "unknown", 1, "A")
    converted, model = await openai_to_gemini_request(
        OpenAIChatCompletionRequest.model_validate(
            {
                "model": _MODEL,
                "messages": [
                    {
                        **modern,
                        "function_call": legacy,
                        # Visible transcript text must never create an owner.
                        "content": tool_history_text(
                            "tool_call", "unknown", None, "{}"
                        ),
                    },
                    {
                        "role": role,
                        "content": "42",
                        **({"name": name} if name is not None else {}),
                    },
                ],
                "tools": [],
            }
        )
    )
    payload = {"result": "42"}
    native_part = AistudioPart(function_response=("unknown", payload, "A"))
    transcript = AistudioPart(
        text=tool_history_text("tool_result", "unknown", None, payload)
    )
    assert normalize_gemini_request(converted, model).contents[-1].parts == [
        transcript if blocked else native_part
    ]

    # A native request with the same public content has no Chat import hints.
    # Neither serialization nor client's private-looking fields carry provenance.
    public = converted.model_dump(exclude_none=True)
    assert "_compat_owner" not in json.dumps(public)
    public["contents"][0]["_compat_owner"] = {"legacy_name": "unknown"}
    public["contents"][1]["parts"][0]["functionResponse"]["_compat_owner"] = {
        "legacy_name": "unknown"
    }
    native = GeminiGenerateContentRequest.model_validate(public)
    assert native == converted
    assert normalize_gemini_request(native, model).contents[-1].parts == [native_part]
