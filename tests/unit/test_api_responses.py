import json

from aistudio_api.api.responses import chat_completion_response, normalize_usage, sse_chunk, sse_usage_chunk, to_gemini_parts


def test_sse_chunk_includes_null_usage_when_requested():
    payload = sse_chunk("chatcmpl-test", "models/gemma-4-31b-it", "你好", include_usage=True)
    data = json.loads(payload.removeprefix("data: ").strip())

    assert data["choices"][0]["delta"]["content"] == "你好"
    assert "usage" in data
    assert data["usage"] is None


def test_sse_usage_chunk_matches_openai_style_shape():
    payload = sse_usage_chunk(
        "chatcmpl-test",
        "models/gemma-4-31b-it",
        {
            "prompt_tokens": 5,
            "completion_tokens": 161,
            "total_tokens": 166,
            "completion_tokens_details": {"reasoning_tokens": 153},
        },
    )
    data = json.loads(payload.removeprefix("data: ").strip())

    assert data["choices"] == []
    assert data["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 161,
        "total_tokens": 166,
        "completion_tokens_details": {"reasoning_tokens": 153},
    }


def test_normalize_usage_defaults_missing_values_to_zero():
    assert normalize_usage(None) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def test_chat_completion_response_maps_function_calls_to_openai_tool_calls():
    response = chat_completion_response(
        model="models/gemma-4-31b-it",
        content="",
        function_calls=[{"name": "getWeather", "args": {"city": "Shanghai"}}],
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "getWeather"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {"city": "Shanghai"}


def test_sse_chunk_can_emit_tool_calls_delta():
    payload = sse_chunk(
        "chatcmpl-test",
        "models/gemma-4-31b-it",
        "",
        tool_calls=[
            {
                "id": "call_test",
                "type": "function",
                "function": {"name": "getWeather", "arguments": "{\"city\":\"Shanghai\"}"},
            }
        ],
        include_usage=True,
    )
    data = json.loads(payload.removeprefix("data: ").strip())

    assert data["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "getWeather"
    assert data["usage"] is None


def test_to_gemini_parts_keeps_function_call_and_response_parts():
    parts = to_gemini_parts(
        "",
        function_calls=[{"name": "getWeather", "args": {"city": "Shanghai"}}],
        function_responses=[{"name": "getWeather", "args": {"temperature": "24C"}}],
    )

    assert parts == [
        {"functionCall": {"name": "getWeather", "args": {"city": "Shanghai"}}},
        {"functionResponse": {"name": "getWeather", "response": {"temperature": "24C"}}},
    ]
