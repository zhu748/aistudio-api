"""Native function-result blob names belong to their own input attachments."""

from __future__ import annotations

import base64
import json
from copy import deepcopy

import pytest
from google import genai
from google.genai import types
from test_compat_media_routes import _PNG, _serialized_contents
from test_signed_part_roundtrip import HEADERS, MODEL, route as route, signature

from aistudio_api.api.schemas import GeminiFunctionResponse, GeminiPart
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_REFS = {"before": {"$ref": "before.png"}, "after": {"$ref": "after.png"}}
_CALL = AistudioPart(
    function_call=("compare", {}, "C"), thought_signature=signature("compare")
)
_RESULT = AistudioPart(function_response=("compare", _REFS, "C"))


def _image_wire(sample: str, name: str | None = None):
    blob = ["image/png", _PNG[sample]]
    return [None, None, blob if name is None else [*blob, None, None, None, name]]


@pytest.mark.parametrize("stream", [False, True])
async def test_stock_chat_keeps_swapped_names_on_their_exact_png(route, stream):
    wire = _serialized_contents(route)
    client = genai.Client(
        api_key="fixture",
        http_options=types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            httpx_async_client=route.raw,
            headers=HEADERS,
        ),
    )
    async with client.aio as sdk:
        for names in (("before.png", "after.png"), ("after.png", "before.png")):
            route.wire.reply(_CALL)
            chat = sdk.chats.create(model=MODEL)
            first = await chat.send_message("Compare.")
            assert first.function_calls and first.function_calls[0].id == "C"
            result = types.Part(
                function_response=types.FunctionResponse(
                    name="compare",
                    id="C",
                    response=_REFS,
                    parts=[
                        types.FunctionResponsePart(
                            inline_data=types.FunctionResponseBlob(
                                mime_type="image/png",
                                data=base64.b64decode(_PNG[sample]),
                                display_name=name,
                            )
                        )
                        for sample, name in zip(("A", "B"), names, strict=True)
                    ],
                )
            )
            route.wire.reply(AistudioPart(text="Done."))
            if stream:
                replies = [r async for r in await chat.send_message_stream(result)]
            else:
                replies = [await chat.send_message(result)]
            assert "".join(reply.text or "" for reply in replies) == "Done."
            # Same bytes, order, result ID, references, and signed model history.
            # Only the two upload-name slots change. No live resolution is assumed.
            assert wire[-1] == [
                [[AistudioPart(text="Compare.").to_wire()], "user"],
                [[_CALL.to_wire()], "model"],
                [
                    [
                        _RESULT.to_wire(),
                        _image_wire("A", names[0]),
                        _image_wire("B", names[1]),
                    ],
                    "user",
                ],
            ]


@pytest.mark.parametrize("stream", [False, True])
async def test_raw_camel_child_uses_upload_rules_but_outer_blob_names_stay_separate(
    route, stream
):
    wire = _serialized_contents(route)
    generated = AistudioPart(
        inline_data=("image/png", _PNG["B"]),
        inline_filename="not-a-public-output-field.png",
        thought_signature=signature("image"),
    )
    route.wire.reply(generated)
    blob = {"mimeType": "image/png", "data": _PNG["A"]}
    request = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {**blob, "displayName": "ignore.png"}},
                    {"inline_data": {**blob, "display_name": "also-ignored.png"}},
                    {"inlineData": {**blob, "filename": "ordinary-upload.png"}},
                ],
            },
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "compare", "args": {}, "id": "C"},
                        "thoughtSignature": _CALL.thought_signature,
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "compare",
                            "id": "C",
                            "response": _REFS,
                            "parts": [
                                {
                                    "inlineData": {
                                        **blob,
                                        "displayName": "  be\x00fore.png\t",
                                    }
                                }
                            ],
                        }
                    }
                ],
            },
        ],
    }
    method = "streamGenerateContent" if stream else "generateContent"
    reply = await route.raw.post(f"/v1beta/models/{MODEL}:{method}", json=request)
    assert reply.status_code == 200, reply.text
    assert wire[-1] == [
        [
            [
                _image_wire("A"),
                _image_wire("A"),
                _image_wire("A", "ordinary-upload.png"),
            ],
            "user",
        ],
        [[_CALL.to_wire()], "model"],
        [[_RESULT.to_wire(), _image_wire("A", "before.png")], "user"],
    ]
    rows = (
        [
            json.loads(line[6:])
            for line in reply.text.splitlines()
            if line.startswith("data: ")
        ]
        if stream
        else [reply.json()]
    )
    assert [
        part
        for row in rows
        for candidate in row["candidates"]
        for part in candidate["content"]["parts"]
    ] == [
        {
            "inlineData": {"mimeType": "image/png", "data": _PNG["B"]},
            "thoughtSignature": generated.thought_signature,
        }
    ]


@pytest.mark.parametrize(
    ("name_fields", "expected"),
    [
        (
            {
                "filename": "f",
                "file_name": "u",
                "displayName": "c",
                "display_name": "s",
            },
            "f",
        ),
        ({"filename": None, "file_name": "u", "displayName": "c"}, None),
        ({"file_name": "", "display_name": "s"}, ""),
        ({"file_name": "u", "displayName": "c"}, "u"),
        ({"displayName": "c", "display_name": "s"}, "c"),
        ({"displayName": None, "display_name": "s"}, None),
        ({"display_name": "s"}, "s"),
        ({}, None),
    ],
)
def test_child_name_uses_key_presence_and_does_not_mutate_input(name_fields, expected):
    blob = {"mimeType": "image/png", "data": _PNG["A"]}
    child = {
        "inlineData": {**blob, **name_fields},
        "inline_data": {**blob, "filename": "shadowed"},
    }
    original = deepcopy(child)
    typed = GeminiPart(text="Already normalized.")
    result = GeminiFunctionResponse.model_validate({"parts": [child, typed]})
    assert result.parts and result.parts[0].inlineData
    assert result.parts[0].inlineData.filename == expected
    assert result.parts[1] is typed and child == original
