"""Native media must not disappear from a tool result or change owner on SDK replay."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from google import genai
from google.genai import errors, types
from test_compat_media_routes import _serialized_contents
from test_signed_part_roundtrip import HEADERS, MODEL, route as route, signature

from aistudio_api.api.app import http_exception_handler
from aistudio_api.api.dependencies import get_client
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_MIME = "application/octet-stream"
_URI = "https://fixture.invalid/blue.bin"
_BYTES = b"\xff\xfe\x00\xfb"
_STANDARD = "//4A+w=="
_CALLS = [
    AistudioPart(
        function_call=("compare", {"sample": key}, key),
        thought_signature=signature("call " + key),
    )
    for key in "AB"
]


def _sdk(route):
    return genai.Client(
        api_key="fixture",
        http_options=types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            httpx_async_client=route.raw,
            headers=HEADERS,
        ),
    ).aio


def _blob(data, name):
    return types.FunctionResponsePart(
        inline_data=types.FunctionResponseBlob(
            mime_type=_MIME, data=data, display_name=name
        )
    )


def _result(key, payload, children, sig=None):
    return types.Part(
        function_response=types.FunctionResponse(
            name="compare", id=key, response=payload, parts=children
        ),
        thought_signature=sig,
    )


@pytest.mark.parametrize("endpoint", ["generate", "stream", "count"])
@pytest.mark.parametrize("case", ["top", "nested", "mixed", "orphan"])
async def test_file_data_fails_before_any_provider_work(
    route, monkeypatch, endpoint, case
):
    route.app.add_exception_handler(HTTPException, http_exception_handler)
    prepared = _serialized_contents(route)
    count = AsyncMock(return_value=7)
    monkeypatch.setattr(
        route.app.dependency_overrides[get_client](), "count_tokens", count
    )
    route.wire.reply(AistudioPart(text="Not expected."))
    contents = []
    if case != "orphan":
        contents.append(
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="compare", id="B", args={}
                        )
                    )
                ],
            )
        )
    if case == "top":
        part = types.Part.from_uri(file_uri=_URI, mime_type=_MIME)
    else:
        uri = types.FunctionResponsePart(
            file_data=types.FunctionResponseFileData(
                file_uri=_URI, mime_type=_MIME, display_name="blue.bin"
            )
        )
        children = [_blob(_BYTES, "control.bin"), uri] if case == "mixed" else [uri]
        part = _result("B", {"sample": {"$ref": "blue.bin"}}, children)
    contents.append(types.Content(role="user", parts=[part]))
    async with _sdk(route) as sdk:
        with pytest.raises(
            errors.APIError, match="fileData is not supported yet"
        ) as err:
            if endpoint == "generate":
                await sdk.models.generate_content(model=MODEL, contents=contents)
            elif endpoint == "stream":
                async for _ in await sdk.models.generate_content_stream(
                    model=MODEL, contents=contents
                ):
                    pass
            else:
                await sdk.models.count_tokens(model=MODEL, contents=contents)
    # SSE can already have opened its HTTP-200 response. The SDK must still
    # see the same typed failure as for a top-level URI, not a success frame.
    assert (err.value.code, err.value.status) == (400, "INVALID_ARGUMENT")
    assert prepared == []
    count.assert_not_awaited()


@pytest.mark.parametrize("stream", [False, True])
async def test_stock_sdk_replays_signed_binary_and_keeps_named_result_owners(
    route, monkeypatch, stream
):
    prepared = _serialized_contents(route)
    count = AsyncMock(return_value=7)
    monkeypatch.setattr(
        route.app.dependency_overrides[get_client](), "count_tokens", count
    )
    signed = AistudioPart(
        inline_data=(_MIME, _STANDARD), thought_signature=signature("model blob")
    )
    route.wire.reply(signed, *_CALLS)
    b_payload = {"sample": {"$ref": "B.bin"}, "literal": "__4A-w=="}
    a_payload = {
        "files": [{"$ref": "A1.bin"}, {"$ref": "A2.bin"}],
        "fileData": {"fileUri": "just result text"},
    }
    inputs = [
        _result("B", b_payload, [_blob(_BYTES, "B.bin")], b"opaque compound signature"),
        _result(
            "A", a_payload, [_blob(b"\xff", "A1.bin"), _blob(b"\xfb\xff", "A2.bin")]
        ),
        types.Part.from_bytes(data=_BYTES, mime_type=_MIME),
    ]
    async with _sdk(route) as sdk:
        chat = sdk.chats.create(model=MODEL)
        first = await chat.send_message("Compare.")
        assert first.candidates and first.candidates[0].content
        parts = first.candidates[0].content.parts
        assert parts and parts[0].inline_data and parts[0].inline_data.data == _BYTES
        assert parts[0].thought_signature == base64.b64decode(signature("model blob"))
        assert [p.function_call.id for p in parts if p.function_call] == ["A", "B"]
        counted = await sdk.models.count_tokens(
            model=MODEL,
            contents=[
                *chat.get_history(curated=True),
                types.Content(role="user", parts=inputs),
            ],
        )
        assert counted.total_tokens == 7
        route.wire.reply(AistudioPart(text="Done."))
        if stream:
            replies = [frame async for frame in await chat.send_message_stream(inputs)]
        else:
            replies = [await chat.send_message(inputs)]
        assert "".join(frame.text or "" for frame in replies) == "Done."

    assert prepared[-1][-2:] == [
        [[signed.to_wire(), *(call.to_wire() for call in _CALLS)], "model"],
        [
            [
                AistudioPart(function_response=("compare", b_payload, "B")).to_wire(),
                [None, None, [_MIME, _STANDARD, None, None, None, "B.bin"]],
                AistudioPart(function_response=("compare", a_payload, "A")).to_wire(),
                [None, None, [_MIME, "/w==", None, None, None, "A1.bin"]],
                [None, None, [_MIME, "+/8=", None, None, None, "A2.bin"]],
                [None, None, [_MIME, _STANDARD]],
            ],
            "user",
        ],
    ]
    assert count.await_args is not None
    assert [c.to_wire() for c in count.await_args.kwargs["contents"]] == prepared[-1]


@pytest.mark.parametrize("size", range(1, 13))
def test_byte_slot_converts_padded_and_unpadded_sdk_spellings_only(size):
    data = (b"\xfb\xff\xfe" * 4)[:size]
    encoded = base64.urlsafe_b64encode(data).decode("ascii")
    standard = base64.b64encode(data).decode("ascii")
    for spelling in (encoded, encoded.rstrip("=")):
        part = AistudioPart(
            text=spelling, inline_data=(_MIME, spelling), inline_filename=spelling
        )
        assert part.to_wire() == [
            None,
            spelling,
            [_MIME, standard, None, None, None, spelling],
        ]


def test_leave_unrelated_and_malformed_byte_spellings_alone():
    for spelling in (
        "aGk",
        "//4A+w==",
        "QUJDREVGR0hJSktMTU5PUA",  # Standard, including unpadded.
        "not~b64_",
        "AAAAA===",
        "__4A-w=",
        "__4A+w==",
        "_x==",
        " __4A-w== ",
    ):
        assert AistudioPart(inline_data=(_MIME, spelling)).to_wire() == [
            None,
            None,
            [_MIME, spelling],
        ]
