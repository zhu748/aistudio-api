"""Offline request regressions for Anthropic plain-text document sources."""

from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from aistudio_api.api import routes_compat
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.schemas import GeminiPart
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application import compat_claude
from aistudio_api.application.chat_service import normalize_gemini_request


def _text(text):
    return {"type": "text", "text": text}


def _document(data):
    return {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": data},
    }


async def test_messages_keeps_user_text_documents_in_order(monkeypatch):
    app = FastAPI()
    app.include_router(routes_compat.router)
    app.dependency_overrides[routes_compat.get_client] = object
    reply = GeminiGenerateContentResponse.model_validate(
        {
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": "OK"}]}}
            ]
        }
    )
    backend = AsyncMock(return_value=reply)
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", backend)
    media = AsyncMock(side_effect=AssertionError("plain text must not fetch media"))
    monkeypatch.setattr(compat_claude, "_media_part_from_url", media)

    body = "  Musée hours:\n09:00–17:00.\n"
    literal_url = "https://example.invalid/museum"
    user_blocks = [
        _text("Before"),
        _document(body),
        _document(literal_url),
        _text("After"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "gemini-3.8-flash",
                "max_tokens": 32,
                "system": "Instructions",
                "messages": [{"role": "user", "content": user_blocks}],
            },
        )
    assert response.status_code == 200, response.text
    backend.assert_awaited_once()
    model, request, _ = backend.await_args_list[0].args
    normalized = normalize_gemini_request(request, model)
    assert normalized.system_instruction is not None
    assert [p.text for p in normalized.system_instruction.parts] == ["Instructions"]
    assert [(c.role, [p.text for p in c.parts]) for c in normalized.contents] == [
        ("user", ["Before", body, literal_url, "After"]),
    ]
    media.assert_not_awaited()


async def test_document_recognition_leaves_other_sources_unchanged(monkeypatch):
    url = "https://example.invalid/fixture.pdf"
    fetch = AsyncMock(return_value=GeminiPart(text="stub PDF"))
    monkeypatch.setattr(compat_claude, "_media_part_from_url", fetch)
    sources = [
        {"type": "text", "media_type": "application/pdf", "data": "wrong type"},
        {"type": "text", "data": "no media type"},
        {"type": "text", "media_type": "text/plain", "data": {"text": "not a string"}},
        {"type": "content", "content": [_text("nested blocks")]},
        {"type": "url", "url": url},
        {"type": "text", "media_type": "text/plain", "data": "User document"},
        {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0="},
    ]
    blocks = [{"type": "document", "source": s} for s in sources]
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "system": [_text("Instructions"), _document("Not an instruction")],
            "messages": [
                {
                    "role": "assistant",
                    "content": [_text("History"), _document("Not model text")],
                },
                {"role": "user", "content": blocks},
            ],
        }
    )
    request, _ = await compat_claude.claude_to_gemini_request(req)
    assert request.systemInstruction is not None
    assert [p.text for p in request.systemInstruction.parts] == ["Instructions"]
    assert [p.text for p in request.contents[0].parts] == ["History"]
    assert [p.model_dump(exclude_none=True) for p in request.contents[1].parts] == [
        {"text": "stub PDF"},
        {"text": "User document"},
        {"inlineData": {"mimeType": "application/pdf", "data": "JVBERi0="}},
    ]
    fetch.assert_awaited_once_with(url)


async def test_user_tool_result_keeps_document_text_inside_its_result():
    req = ClaudeMessagesRequest.model_validate(
        {
            "model": "gemini-3.8-flash",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "read", "id": "one", "input": {}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "one",
                            "content": [_text("A"), _document("B\nC"), _text("D")],
                        },
                        _text("Question"),
                    ],
                },
            ],
        }
    )
    request, _ = await compat_claude.claude_to_gemini_request(req)
    assert [p.model_dump(exclude_none=True) for p in request.contents[-1].parts] == [
        {
            "functionResponse": {
                "name": "read",
                "id": "one",
                "response": {"result": "A\nB\nC\nD"},
            }
        },
        {"text": "Question"},
    ]
