"""Bounded inline PDFs belong to their own Claude user tool results."""

import base64
from unittest.mock import AsyncMock

import anthropic
import pytest
from test_compat_media_routes import _PNG, _client_options, _serialized_contents
from test_signed_part_roundtrip import MODEL, _claude_sdk_turn, route as route

from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application import compat_shared
from aistudio_api.application.compat_claude import claude_to_gemini_request
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_PROMPT = {"role": "user", "content": "Compare the reports."}


def _pdf(label):
    """Small, distinct blank PDFs without external fixtures or dependencies."""
    data = bytearray(f"%PDF-1.4\n% {label}\n".encode())
    offsets = []
    for i, body in enumerate(
        (
            "<</Type/Catalog/Pages 2 0 R>>",
            "<</Type/Pages/Count 1/Kids[3 0 R]>>",
            "<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]/Resources<<>>>>",
        ),
        1,
    ):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = len(data)
    data.extend(b"xref\n0 4\n0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<</Size 4/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode())
    return base64.b64encode(data).decode("ascii")


_PDF = {key: _pdf(key) for key in "AB"}


def _document(source):
    # A public display title is not a filename in the provider's wire format.
    return {"type": "document", "title": "Display title", "source": source}


def _base64(mime, data):
    return {"type": "base64", "media_type": mime, "data": data}


def _pdf_block(key):
    return _document(_base64("application/pdf", _PDF[key]))


def _result(key, content):
    return {"type": "tool_result", "tool_use_id": key, "content": content}


@pytest.fixture(autouse=True)
def _no_media_fetch(monkeypatch):
    get_client = AsyncMock(side_effect=AssertionError("unexpected media download"))
    monkeypatch.setattr(MediaSession, "get_client", get_client)
    yield
    get_client.assert_not_awaited()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
async def test_strict_messages_keeps_each_pdf_with_its_result(route, stream, mixed):
    seen = _serialized_contents(route)
    route.wire.reply(
        *(AistudioPart(function_call=(f"read_{key}", {}, key)) for key in "AB")
    )
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as sdk:
        args = {"model": MODEL, "max_tokens": 48, "messages": [_PROMPT]}
        first = await _claude_sdk_turn(sdk, args, stream)
        assert [(b.name, b.id) for b in first.content] == [
            ("read_A", "A"),
            ("read_B", "B"),
        ]
        echo = [b.model_dump(exclude_none=True) for b in first.content]
        results = []
        expected = []
        # Results return in reverse call order.
        for key in "BA":
            content = [_pdf_block(key)]
            media = [AistudioPart(inline_data=("application/pdf", _PDF[key]))]
            payload = {}
            if mixed:
                # The two results have opposite attachment order.
                pos = 0 if key == "B" else 1
                image = {"type": "image", "source": _base64("image/png", _PNG[key])}
                content.insert(pos, image)
                media.insert(pos, AistudioPart(inline_data=("image/png", _PNG[key])))
                content.insert(0, {"type": "text", "text": f"Before {key}"})
                note = {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": f"café {key}",
                }
                content.append(_document(note))
                content.append({"type": "text", "text": f"After {key}"})
                payload["result"] = f"Before {key}\ncafé {key}\nAfter {key}"
            if key == "B":
                payload["is_error"] = True
            results.append({**_result(key, content), "is_error": key == "B"})
            expected.extend(
                [AistudioPart(function_response=(f"read_{key}", payload, key)), *media]
            )
        results.extend([{"type": "text", "text": "Outer caption"}, _pdf_block("A")])
        expected.extend(
            [
                AistudioPart(text="Outer caption"),
                AistudioPart(inline_data=("application/pdf", _PDF["A"])),
            ]
        )
        args["messages"] = [
            _PROMPT,
            {"role": "assistant", "content": echo},
            {"role": "user", "content": results},
        ]
        route.wire.reply(AistudioPart(text="Noted."))
        last = await _claude_sdk_turn(sdk, args, stream)
        assert last.content[0].text == "Noted." and last.stop_reason == "end_turn"
    assert seen[-1][-1] == [[part.to_wire() for part in expected], "user"]


async def _post_results(route, contents):
    calls = [
        {"type": "tool_use", "id": key, "name": f"read_{key}", "input": {}}
        for key in contents
    ]
    results = [_result(key, items) for key, items in contents.items()]
    return await route.raw.post(
        "/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 48,
            "messages": [
                _PROMPT,
                {"role": "assistant", "content": calls},
                {"role": "user", "content": results},
            ],
        },
    )


async def test_unavailable_or_malformed_documents_do_not_expand(route):
    seen = _serialized_contents(route)
    pdf = {"type": "base64", "media_type": "application/pdf"}
    sources = [
        {"type": "url", "url": "https://fixture.invalid/report.pdf"},
        {"type": "file", "file_id": "offline-file-reference"},
        {"type": "content", "content": [{"type": "text", "text": "not flattened"}]},
        {"type": "base64", "data": _PDF["A"]},
        {**pdf, "media_type": "image/png", "data": _PNG["A"]},
        pdf,
        {**pdf, "data": ""},
        {**pdf, "data": 42},
    ]
    route.wire.reply(AistudioPart(text="Noted."))
    response = await _post_results(
        route,
        {"A": [{"type": "text", "text": "Keep"}, *(_document(s) for s in sources)]},
    )
    assert response.status_code == 200, response.text
    expected = AistudioPart(function_response=("read_A", {"result": "Keep"}, "A"))
    assert seen[-1][-1] == [[expected.to_wire()], "user"]


async def test_assistant_result_documents_do_not_gain_user_attachment_support():
    message = {"role": "assistant", "content": [_result("A", [_pdf_block("A")])]}
    req = ClaudeMessagesRequest.model_validate({"model": MODEL, "messages": [message]})
    converted, _ = await claude_to_gemini_request(req)
    (only_part,) = converted.contents[0].parts
    assert only_part.functionResponse is not None and only_part.inlineData is None


@pytest.mark.parametrize("bound", ["per-file", "aggregate", "exact"])
async def test_nested_pdfs_share_existing_media_budgets(route, monkeypatch, bound):
    seen = _serialized_contents(route)
    size = len(base64.b64decode(_PDF["A"]))
    monkeypatch.setattr(compat_shared, "_MAX_MEDIA_BYTES", size - (bound == "per-file"))
    monkeypatch.setattr(
        settings, "max_request_bytes", size * 2 - (bound == "aggregate")
    )
    route.wire.reply(AistudioPart(text="Noted."))
    response = await _post_results(route, {key: [_pdf_block(key)] for key in "AB"})
    if bound == "exact":
        assert response.status_code == 200, response.text
        assert len(seen[-1][-1][0]) == 4
    else:
        assert response.status_code == 400
        assert "limit" in response.text and not seen
