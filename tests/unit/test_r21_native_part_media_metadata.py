"""Native Part-level media settings must not silently change a request's meaning."""

import base64
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from google.genai import errors, types
from test_compat_media_routes import _PNG
from test_r17_native_tool_media import _sdk
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api.app import http_exception_handler
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import api_service_gemini
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_spec import GenerationConfigIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_METHODS = ("generateContent", "streamGenerateContent", "countTokens")
# Three 16x16 blue MPEG-1 frames (25 fps), no media download or decoder needed.
_MPEG = "AAABswEAEBP//+AYAAABuAAIAEAAAAEAAA//+AAAAQET+UUpS/cLzYAAAAEAAFf/+IAAAAEBEnAAAAEAAJf/+IAAAAEBEnA="
_MARK = b"private inert model marker"


@pytest.fixture
def reject_before_provider(route, monkeypatch):
    route.app.add_exception_handler(HTTPException, http_exception_handler)
    account = AsyncMock(side_effect=AssertionError("unexpected account selection"))
    prepare = AsyncMock(side_effect=AssertionError("unexpected generation preparation"))
    count = AsyncMock(side_effect=AssertionError("unexpected provider count"))
    gateway = route.app.dependency_overrides[get_client]()
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", account)
    monkeypatch.setattr(gateway, "prepare_request", prepare)
    monkeypatch.setattr(gateway, "count_tokens", count)
    yield
    account.assert_not_awaited()
    prepare.assert_not_awaited()
    count.assert_not_awaited()


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("role", ["user", "model"])
@pytest.mark.parametrize(
    ("options", "field"),
    [
        (
            {"video_metadata": types.VideoMetadata(start_offset="0.04s")},
            "videoMetadata.start_offset",
        ),
        (
            {"video_metadata": types.VideoMetadata(end_offset="0.08s")},
            "videoMetadata.end_offset",
        ),
        ({"video_metadata": types.VideoMetadata(fps=12)}, "videoMetadata.fps"),
        (
            {
                "media_resolution": types.PartMediaResolution(
                    level=types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH
                )
            },
            "mediaResolution.level",
        ),
        (
            {"media_resolution": types.PartMediaResolution(num_tokens=256)},
            "mediaResolution.num_tokens",
        ),
    ],
)
async def test_sdk_rejects_only_the_explicit_option(
    route, reject_before_provider, method, role, options, field
):
    data, mime = (
        (_MPEG, "video/mpeg")
        if "video_metadata" in options
        else (_PNG["A"], "image/png")
    )
    part = types.Part(
        inline_data=types.Blob(mime_type=mime, data=base64.b64decode(data)),
        thought_signature=_MARK if role == "model" else None,
        **options,
    )
    turn = types.Content(
        role=role, parts=[types.Part(text="Before."), part, types.Part(text="After.")]
    )
    prompt = types.Content(
        role="user", parts=[types.Part(text="Compare the swatches.")]
    )
    contents = [turn] if role == "user" else [prompt, turn, prompt]
    global_config = types.GenerateContentConfig(
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW
    )
    async with _sdk(route) as sdk:
        with pytest.raises(errors.ClientError) as err:
            if method == "countTokens":
                await sdk.models.count_tokens(model=MODEL, contents=contents)
            elif method == "streamGenerateContent":
                async for _ in await sdk.models.generate_content_stream(
                    model=MODEL, contents=contents, config=global_config
                ):
                    pytest.fail("unsupported Part option reached a success stream")
            else:
                await sdk.models.generate_content(
                    model=MODEL, contents=contents, config=global_config
                )
    assert (err.value.code, err.value.status) == (400, "INVALID_ARGUMENT")
    message = str(err.value.message)
    assert f"contents[{int(role == 'model')}].parts[1].{field}" in message
    for private in (data, base64.b64encode(_MARK).decode(), "Before."):
        assert private not in message


@pytest.mark.parametrize(
    ("method", "wrapper", "turn", "group", "setting", "value"),
    [
        ("generateContent", "", "contents", "videoMetadata", "startOffset", "0s"),
        (
            "streamGenerateContent",
            "",
            "system_instruction",
            "video_metadata",
            "end_offset",
            "0.08s",
        ),
        (
            "countTokens",
            "generateContentRequest",
            "contents",
            "mediaResolution",
            "numTokens",
            128,
        ),
        (
            "countTokens",
            "generate_content_request",
            "systemInstruction",
            "media_resolution",
            "level",
            "MEDIA_RESOLUTION_ULTRA_HIGH",
        ),
    ],
)
async def test_direct_rest_aliases_and_wrapped_count(
    route, reject_before_provider, method, wrapper, turn, group, setting, value
):
    image = {"mime_type": "image/png", "data": _PNG["A"]}
    annotated = {"parts": [{"inline_data": image, group: {setting: value}}]}
    gen = {
        "contents": [{"parts": [{"text": "Plain prompt."}]}],
        turn: [annotated] if turn == "contents" else annotated,
    }
    body = {wrapper: gen} if wrapper else gen
    response = await route.raw.post(f"/v1beta/models/{MODEL}:{method}", json=body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["status"]) == (400, "INVALID_ARGUMENT")
    path = (
        (wrapper + "." if wrapper else "")
        + turn
        + ("[0]" if turn == "contents" else "")
    )
    assert f"{path}.parts[0].{group}.{setting}" in error["message"]


@pytest.mark.parametrize("method", _METHODS)
async def test_noop_metadata_and_user_json_preserve_media_and_global_settings(
    route, monkeypatch, method
):
    gateway = route.app.dependency_overrides[get_client]()
    codec = AistudioWireCodec()
    generated = []

    async def prepare(*, contents, generation_config_overrides, **kwargs):
        body = codec.encode(
            codec.build_request(
                model=MODEL,
                contents=contents,
                generation_config_overrides=generation_config_overrides,
            )
        )
        generated.append(json.loads(body))
        return PreparedRequest(url="http://fixture.invalid", headers={}, body=body)

    count = AsyncMock(return_value=11)
    monkeypatch.setattr(gateway, "count_tokens", count)
    monkeypatch.setattr(gateway, "prepare_request", prepare)
    route.wire.reply(AistudioPart(text="OK."))
    literal = {
        "videoMetadata": {"fps": 12},
        "mediaResolution": {"level": "MEDIA_RESOLUTION_HIGH"},
    }
    marker = base64.b64encode(_MARK).decode()
    contents = [
        {"role": "user", "parts": [{"text": "Compare."}]},
        {
            "role": "model",
            "parts": [
                {
                    "inlineData": {"mimeType": "video/mpeg", "data": _MPEG, **literal},
                    "thoughtSignature": marker,
                },
                {"functionCall": {"name": "swatch", "args": literal, "id": "A"}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "swatch",
                        "id": "A",
                        "response": literal,
                    }
                },
                {"inlineData": {"mimeType": "image/png", "data": _PNG["A"]}},
            ],
        },
    ]
    expected = [
        [[AistudioPart(text="Compare.").to_wire()], "user"],
        [
            [
                AistudioPart(
                    inline_data=("video/mpeg", _MPEG), thought_signature=marker
                ).to_wire(),
                AistudioPart(function_call=("swatch", literal, "A")).to_wire(),
            ],
            "model",
        ],
        [
            [
                AistudioPart(function_response=("swatch", literal, "A")).to_wire(),
                AistudioPart(inline_data=("image/png", _PNG["A"])).to_wire(),
            ],
            "user",
        ],
    ]
    # No trim, sampling or resolution requested: these inputs retain existing semantics.
    benign = (
        {},
        {"videoMetadata": None, "mediaResolution": {}},
        {
            "video_metadata": {"start_offset": " ", "endOffset": "", "fps": None},
            "mediaResolution": {"numTokens": None},
        },
        {
            "videoMetadata": {"unknown": {"fps": 12}},
            "media_resolution": {"level": "MEDIA_RESOLUTION_UNSPECIFIED", "other": 1},
        },
        {"mediaResolution": {"level": 0}, "videoMetadataExtension": {"fps": 12}},
    )
    url = f"/v1beta/models/{MODEL}:{method}"
    for i, extra in enumerate(benign):
        level, slot = (("LOW", 1), ("MEDIUM", 2), ("HIGH", 3))[i % 3]
        turn = contents[1]
        model = {**turn, "parts": [{**turn["parts"][0], **extra}, turn["parts"][1]]}
        gen = {
            "contents": [contents[0], model, contents[2]],
            "generationConfig": {"mediaResolution": "MEDIA_RESOLUTION_" + level},
        }
        if method == "countTokens":
            # A wrapped generation config isn't a count-options object. When
            # top-level contents win, do not inspect the shadowed contents.
            body = {"generateContentRequest": gen}
            if i % 2:
                body = {
                    "contents": gen["contents"],
                    "generateContentRequest": {
                        **gen,
                        "contents": [{"parts": [literal]}],
                    },
                }
            response = await route.raw.post(url, json=body)
            assert response.json() == {"totalTokens": 11}
            assert count.await_args is not None and not generated
            assert [
                c.to_wire() for c in count.await_args.kwargs["contents"]
            ] == expected
        else:
            response = await route.raw.post(url, json=gen)
            assert response.status_code == 200 and "OK." in response.text
            count.assert_not_awaited()
            assert generated[-1][codec.CONTENTS_INDEX] == expected
            assert (
                generated[-1][codec.GENERATION_CONFIG_INDEX][
                    GenerationConfigIndex.MEDIA_RESOLUTION
                ]
                == slot
            )
