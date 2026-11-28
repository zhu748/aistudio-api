"""JSON Schema and Gemini Schema are distinct native generation controls."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.exceptions import RequestValidationError
from google.genai import errors, types
from test_r17_native_tool_media import _sdk
from test_r21_native_part_media_metadata import (
    reject_before_provider as reject_before_provider,
)
from test_signed_part_roundtrip import MODEL, route as route

from aistudio_api.api import routes_gemini
from aistudio_api.api.app import validation_exception_handler
from aistudio_api.api.dependencies import get_client
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_spec import GenerationConfigIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_MODELS = f"/v1beta/models/{MODEL}:"
_CONTENTS = [{"parts": [{"text": "Private prompt."}]}]
_SCHEMA = {"type": "object", "properties": {"private_key": {"type": "integer"}}}


@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_json_schema_is_rejected_before_keepalive_or_provider(
    route, reject_before_provider, monkeypatch, stream
):
    keepalive = AsyncMock(side_effect=AssertionError("generation must not start"))
    monkeypatch.setattr(routes_gemini, "keepalive_response", keepalive)
    async with _sdk(route) as sdk:
        for schema in (_SCHEMA, {}, False, True):
            config = types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=schema
            )
            with pytest.raises(errors.ClientError) as exc:
                if stream:
                    async for _ in await sdk.models.generate_content_stream(
                        model=MODEL, contents="Private prompt.", config=config
                    ):
                        pytest.fail("unsupported schema produced a generation")
                else:
                    await sdk.models.generate_content(
                        model=MODEL, contents="Private prompt.", config=config
                    )
            assert (exc.value.code, exc.value.status) == (400, "INVALID_ARGUMENT")
            message = str(exc.value.message)
            assert "generationConfig.responseJsonSchema" in message
            assert "private_key" not in message and "Private prompt." not in message
    keepalive.assert_not_awaited()


@pytest.mark.parametrize("method", ["generateContent", "streamGenerateContent"])
async def test_rest_aliases_and_existing_validation_priority(
    route, reject_before_provider, method
):
    route.app.add_exception_handler(
        RequestValidationError, validation_exception_handler
    )
    body = {
        "contents": _CONTENTS,
        "generation_config": {"response_schema": None, "response_json_schema": False},
    }
    path = _MODELS + method
    response = await route.raw.post(path, json=body)
    assert response.status_code == 400
    assert "generationConfig.responseJsonSchema" in response.json()["error"]["message"]
    for invalid, expected in (
        (
            {"contents": [{"parts": [{"videoMetadata": {"fps": 12}}]}]},
            "videoMetadata.fps",
        ),
        ({"contents": "invalid"}, "contents"),
        (
            {"generationConfig": {"responseSchema": 7, "responseJsonSchema": {}}},
            "responseSchema",
        ),
    ):
        response = await route.raw.post(path, json={**body, **invalid})
        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert (
            expected in message and "generationConfig.responseJsonSchema" not in message
        )


@pytest.mark.parametrize("method", ["generateContent", "streamGenerateContent"])
async def test_primary_schema_and_non_schema_placeholders_preserve_wire(route, method):
    codec = AistudioWireCodec()
    slots = []

    async def prepare(*, model, contents, generation_config_overrides, **kwargs):
        body = codec.encode(
            codec.build_request(
                model=model,
                contents=contents,
                generation_config_overrides=generation_config_overrides,
            )
        )
        config = json.loads(body)[codec.GENERATION_CONFIG_INDEX]
        slots.append(config[GenerationConfigIndex.RESPONSE_SCHEMA])
        return PreparedRequest(url="http://fixture.invalid", headers={}, body=body)

    route.app.dependency_overrides[get_client]().prepare_request = prepare
    route.wire.reply(AistudioPart(text="OK"))
    for config, expected in (
        ({"responseSchema": {"type": "string"}, "responseJsonSchema": _SCHEMA}, [1]),
        ({"response_schema": {}, "response_json_schema": False}, [6]),
        ({"responseSchema": [3], "response_json_schema": True}, [3]),
        ({"response_schema": [], "responseJsonSchema": {}}, []),
        ({"responseJsonSchema": None, "response_json_schema": _SCHEMA}, None),
        *(
            ({"response_json_schema": value}, None)
            for value in (None, 0, 1, "true", [], [_SCHEMA])
        ),
    ):
        response = await route.raw.post(
            _MODELS + method, json={"contents": _CONTENTS, "generation_config": config}
        )
        assert response.status_code == 200 and "OK" in response.text
        assert slots[-1] == expected


async def test_count_ignores_wrapped_generation_schema(route, monkeypatch):
    count = AsyncMock(return_value=13)
    monkeypatch.setattr(
        route.app.dependency_overrides[get_client](), "count_tokens", count
    )
    # The Developer API SDK doesn't expose the wrapper as a CountTokensConfig
    # field. extra_body sends the REST form and still uses the typed count parser.
    async with _sdk(route) as sdk:
        for wrapper in ("generateContentRequest", "generate_content_request"):
            body = {
                "contents": [],
                wrapper: {
                    "contents": _CONTENTS,
                    "systemInstruction": {"parts": [{"text": "System."}]},
                    "generationConfig": {"responseJsonSchema": _SCHEMA},
                },
            }
            counted = await sdk.models.count_tokens(
                model=MODEL,
                contents="Replaced by extra_body.",
                config=types.CountTokensConfig(
                    http_options=types.HttpOptions(extra_body=body)
                ),
            )
            assert counted.total_tokens == 13
            assert count.await_args is not None
            contents = count.await_args.kwargs["contents"]
            assert [c.parts[0].text for c in contents] == ["System.", "Private prompt."]
