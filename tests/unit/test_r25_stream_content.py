"""Delivery counts payloads, not mere field presence or signature carriers."""

from aistudio_api.application.stream_utils import has_response_content


def test_response_content_truthiness():
    empty: dict[str, object] = {
        "text": "",
        "inlineData": {},
        "functionCall": None,
        "functionResponse": {},
        "executableCode": {},
        "codeExecutionResult": {},
        "thought": True,
        "thoughtSignature": "opaque",
        "fileData": {"fileUri": "fixture"},
    }
    assert has_response_content({}) is False
    assert has_response_content(empty) is False
    for key, value in (
        ("text", "a"),
        ("inlineData", {"data": "YQ=="}),
        ("functionCall", {"name": "read"}),
        ("functionResponse", {"response": {}}),
        ("executableCode", {"code": "1"}),
        ("codeExecutionResult", {"output": "1"}),
    ):
        assert has_response_content({**empty, key: value}) is True
