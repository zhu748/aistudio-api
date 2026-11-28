"""The common SSE Part retains its identity without entering payload projection."""

from unittest.mock import call, patch

import pytest

from aistudio_api.application import compat_shared


@pytest.mark.parametrize(
    "key", ["inlineData", "functionCall", "executableCode", "codeExecutionResult"]
)
def test_frame_parts_shortcut_depends_on_key_absence(key: str) -> None:
    plain = {"text": "line\u2028break", "thought": True, "thoughtSignature": "opaque"}
    metadata = {"extra": {"opaque": 3}}
    explicit_null = {key: None}
    inline = {"mimeType": "image/png", "data": "YQ=="}
    function = {"name": "example", "args": {}}
    executable, result = {"code": "a"}, {"output": "b"}
    compound = {
        "functionCall": function,
        "thoughtSignature": "opaque",
        "codeExecutionResult": result,
        "inlineData": inline,
        "text": "caption",
        "executableCode": executable,
        "thought": True,
    }
    raw = [plain, None, explicit_null, compound, metadata]
    with patch.object(
        compat_shared, "_code_text", wraps=compat_shared._code_text
    ) as code:
        parts = compat_shared._frame_parts({"content": {"parts": raw}})

    assert code.call_args_list == [call(None, None), call(executable, result)]
    assert parts[0] is plain
    assert parts[1] is explicit_null
    assert parts[2:-1] == [
        {"text": "caption", "thought": True},
        {"inlineData": inline, "thought": True},
        {"text": "a\nb"},
        {"functionCall": function},
    ]
    assert parts[-1] is metadata
