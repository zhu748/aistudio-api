"""Messages must reject unresolvable user Files API attachments, not omit them."""

import asyncio
import base64
import json
from typing import cast
from unittest.mock import AsyncMock

import anthropic
import pytest
from anthropic.types import (
    DocumentBlockParam,
    FileDocumentSourceParam,
    FileImageSourceParam,
    ImageBlockParam,
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
)
from anthropic.types.beta import (
    BetaFileDocumentSourceParam,
    BetaFileImageSourceParam,
    BetaImageBlockParam,
    BetaRequestDocumentBlockParam,
    BetaToolResultBlockParam,
)
from test_compat_media_routes import _PNG, _serialized_contents
from test_ordered_tool_routes import sse_events, wire_source as wire_source
from test_r8_claude_estimator_scope import ascii_count
from test_r16_claude_tool_pdfs import _PDF
from test_r21_chat_file_ref import (
    no_external_client as no_external_client,
    reject_before_io as reject_before_io,
)
from test_r23_claude_opaque_history import _client
from test_responses_failure_sdk import _ASGIObserver
from test_signed_part_roundtrip import MODEL, _claude_sdk_turn, route as route

from aistudio_api.api import routes_compat
from aistudio_api.api.schemas_compat import ClaudeMessagesRequest
from aistudio_api.application import api_service_gemini, compat_claude
from aistudio_api.application.media_session import MediaSession
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_ID = "file_private_fixture"
_TITLE = "Private document title"
_TEXT = TextBlockParam(type="text", text="Compare the exhibits.")
_URL = "https://fixture.invalid/earlier.png"
_MEDIA = [
    {"type": "image", "source": {"type": "url", "url": _URL}},
    {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG["A"]},
    },
]


def _file(kind, beta):
    if kind == "document":
        document = BetaRequestDocumentBlockParam if beta else DocumentBlockParam
        source = BetaFileDocumentSourceParam if beta else FileDocumentSourceParam
        return document(
            type="document", source=source(type="file", file_id=_ID), title=_TITLE
        )
    image = BetaImageBlockParam if beta else ImageBlockParam
    source = BetaFileImageSourceParam if beta else FileImageSourceParam
    return image(type="image", source=source(type="file", file_id=_ID))


def _call(key):
    return {"type": "tool_use", "id": key, "name": f"read_{key}", "input": {}}


def _case(kind, nested, mixed, beta):
    attachment = _file(kind, beta)
    parts = [_TEXT, attachment] if mixed else [attachment]
    first = {"role": "user", "content": _MEDIA if mixed else "Read."}
    if nested:
        result = BetaToolResultBlockParam if beta else ToolResultBlockParam
        calls = [_call("A")]
        results = [result(type="tool_result", tool_use_id="A", content=parts)]
        if mixed:
            calls.append(_call("B"))
            results.insert(
                0, result(type="tool_result", tool_use_id="B", content="Other result.")
            )
        return [
            first,
            {"role": "assistant", "content": calls},
            {"role": "user", "content": results},
        ], f"messages[2].content[{int(mixed)}].content[{int(mixed)}]"
    message = {"role": "user", "content": parts}
    if mixed:
        return [
            first,
            {"role": "assistant", "content": "Noted."},
            message,
        ], "messages[2].content[1]"
    return [message], "messages[0].content[0]"


@pytest.mark.parametrize("delivery", ["unary", "early-stream", "committed-stream"])
@pytest.mark.parametrize("beta", [False, True], ids=["standard", "beta"])
@pytest.mark.parametrize("kind", ["document", "image"])
@pytest.mark.parametrize("nested", [False, True], ids=["message", "tool-result"])
@pytest.mark.parametrize("mixed", [False, True], ids=["only", "mixed"])
async def test_typed_file_reference_errors_before_any_media_or_dispatch(
    reject_before_io, monkeypatch, caplog, delivery, beta, kind, nested, mixed
):
    messages, path = _case(kind, nested, mixed, beta)
    chunks = []
    observer = _ASGIObserver(lambda event: chunks.append(event.get("body", b"")))
    account = AsyncMock(side_effect=AssertionError("unexpected account lookup"))
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", account)
    monkeypatch.setattr(compat_claude.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(settings, "nonstream_keepalive", False)
    if delivery == "committed-stream":
        monkeypatch.setattr(settings, "stream_heartbeat_interval", 0.01)
        convert = routes_compat.claude_to_gemini_request

        async def after_commit(req):
            await observer.heartbeat.wait()
            return await convert(req)

        monkeypatch.setattr(routes_compat, "claude_to_gemini_request", after_commit)
    args = {"model": MODEL, "max_tokens": 40, "messages": messages}
    if beta:
        args["betas"] = ["files-api-2025-04-14"]
    async with asyncio.timeout(5), _client(observer) as sdk:
        with pytest.raises(anthropic.APIStatusError) as exc:
            await _claude_sdk_turn(sdk.beta if beta else sdk, args, delivery != "unary")
    body = b"".join(chunks).decode()
    if delivery == "committed-stream":
        assert observer.statuses == [200] and observer.heartbeat.is_set()
        (error,) = sse_events(body)
    else:
        assert observer.statuses == [400] and not observer.heartbeat.is_set()
        assert isinstance(exc.value, anthropic.BadRequestError)
        error = json.loads(body)
    assert (
        error["type"] == "error" and error["error"]["type"] == "invalid_request_error"
    )
    message = error["error"]["message"]
    assert f"{path}.source.file_id" in message and "Files API" in message
    assert "base64" in message and "data" in message and message in str(exc.value)
    if kind == "document":
        assert "application/pdf" in message and "text/plain" in message
    else:
        assert "image" in message
    assert all(
        value not in body for value in (_ID, _TITLE, _TEXT["text"], _PNG["A"], _URL)
    )
    assert _ID not in caplog.text
    account.assert_not_awaited()


@pytest.mark.parametrize("role", ["assistant", "other"])
async def test_non_user_file_references_remain_ignored_without_logging_ids(
    monkeypatch, caplog, role
):
    monkeypatch.setattr(compat_claude.logger, "handlers", [caplog.handler])
    files = [_file(kind, False) for kind in ("document", "image")]
    blocks = [
        *files,
        {"type": "tool_result", "tool_use_id": "A", "content": files},
        _TEXT,
    ]
    req = ClaudeMessagesRequest.model_validate(
        {"model": MODEL, "messages": [{"role": role, "content": blocks}]}
    )
    converted, _ = await compat_claude.claude_to_gemini_request(req)
    result, caption = converted.contents[0].parts
    assert (
        result.functionResponse is not None and result.functionResponse.response == {}
    )
    assert caption.text == _TEXT["text"]
    assert "type=file" in caplog.text and _ID not in caplog.text


async def test_malformed_references_and_opaque_values_do_not_change_inline_wire(
    route, monkeypatch
):
    calls = _serialized_contents(route)
    route.wire.reply(AistudioPart(text="Noted."))
    ref = _file("document", False)
    ignored = [
        {"type": kind, "source": {"type": "file", **fields}}
        for kind in ("image", "document")
        for fields in ({}, {"file_id": None}, {"file_id": " \t"}, {"file_id": 42})
    ]
    ignored += [
        {"type": "other", "source": ref["source"]},
        {"type": "image", "file_id": _ID},
        {"type": "document", "source": {"type": "content", "content": [ref]}},
    ]

    # A stray ID does not supersede an explicit self-contained source.
    def inline(kind, source_type, mime, data):
        source = {"type": source_type, "media_type": mime, "data": data, "file_id": _ID}
        return {"type": kind, "source": source}

    pdf = inline("document", "base64", "application/pdf", _PDF["A"])
    image = inline("image", "base64", "image/png", _PNG["A"])
    note = inline("document", "text", "text/plain", "Literal.")
    nested_lookalike = {"type": "tool_result", "tool_use_id": "B", "content": [ref]}
    content = [
        *ignored,
        nested_lookalike,
        {"type": "document", "source": {"type": "url", "url": _URL}},
        image,
        note,
        pdf,
    ]
    used = {
        "type": "tool_result",
        "tool_use_id": "A",
        "content": content,
        "is_error": True,
    }
    messages = [
        {"role": "user", "content": "Read."},
        {"role": "assistant", "content": [{**_call("A"), "input": {"example": ref}}]},
        {
            "role": "user",
            "content": [used, *ignored, {**_TEXT, "metadata": ref}, pdf, image],
        },
    ]
    charges = []
    account = MediaSession.account_bytes

    def charge(session, size):
        charges.append(size)
        account(session, size)

    img_size, pdf_size = (
        len(base64.b64decode(data)) for data in (_PNG["A"], _PDF["A"])
    )
    monkeypatch.setattr(MediaSession, "account_bytes", charge)
    monkeypatch.setattr(settings, "max_request_bytes", 2 * (img_size + pdf_size))
    reply = await route.raw.post(
        "/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 40,
            "messages": messages,
            "system": [_TEXT, ref],
        },
    )
    assert reply.status_code == 200 and reply.json()["stop_reason"] == "end_turn"
    assert len(calls) == 1 and _ID in json.dumps(calls[0][1])
    assert charges == [img_size, pdf_size, pdf_size, img_size]
    png_part = AistudioPart(inline_data=("image/png", _PNG["A"]))
    pdf_part = AistudioPart(inline_data=("application/pdf", _PDF["A"]))
    expected = [
        AistudioPart(
            function_response=("read_A", {"result": "Literal.", "is_error": True}, "A")
        ),
        png_part,
        pdf_part,
        AistudioPart(text=_TEXT["text"]),
        pdf_part,
        png_part,
    ]
    assert calls[0][-1] == [[part.to_wire() for part in expected], "user"]


@pytest.mark.parametrize("kind", ["document", "image"])
@pytest.mark.parametrize("nested", [False, True])
async def test_count_tokens_still_estimates_user_file_references(
    reject_before_io, monkeypatch, kind, nested
):
    messages, _ = _case(kind, nested, False, False)
    convert = AsyncMock(
        side_effect=AssertionError("count_tokens must not convert media")
    )
    monkeypatch.setattr(routes_compat, "claude_to_gemini_request", convert)
    async with _client(_ASGIObserver()) as sdk:
        count = await sdk.messages.count_tokens(
            model=MODEL, messages=cast("list[MessageParam]", messages)
        )
    assert count.input_tokens == ascii_count(
        {"system": None, "tools": None, "messages": messages}
    )
    convert.assert_not_awaited()
