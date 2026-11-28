"""Anthropic's structured-output helper parses a complete JSON value per text block."""

import json
from dataclasses import dataclass
from typing import Any

import anthropic
import pytest
from pydantic import ValidationError
from test_compat_media_routes import _client_options
from test_signed_part_roundtrip import MODEL, route as route, signature, thought

from aistudio_api.api.dependencies import get_client
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.compat_claude import build_claude_message
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_spec import (
    GenerationConfigIndex,
    TopLevelIndex,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_PROMPT = {"role": "user", "content": "Identify the item."}
_PIECES = ['{"label":', '"Café 東"', ',"count":', "2} \n"]
_JSON = "".join(_PIECES)
_MEDIA = AistudioPart(inline_data=("image/png", "YQ=="))


@dataclass
class Label:
    label: str
    count: int


def _capture(route):
    wire = []
    codec = AistudioWireCodec()

    async def prepare(*, contents, model, generation_config_overrides=None, **kwargs):
        body = codec.encode(
            codec.build_request(
                model=model,
                contents=contents,
                generation_config_overrides=generation_config_overrides,
                tools=kwargs.get("tools"),
            )
        )
        wire.append(json.loads(body))
        return PreparedRequest(url="http://fixture.invalid", headers={}, body=body)

    route.app.dependency_overrides[get_client]().prepare_request = prepare
    sent = []
    options = _client_options(route)

    async def capture_body(request):
        sent.append(json.loads(await request.aread()))

    options["http_client"].event_hooks["request"].append(capture_body)
    return sent, wire, options


@pytest.mark.parametrize("beta", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_parse_fragmented_dataclass_and_replay_exact_text(route, beta, stream):
    sent, wire, options = _capture(route)
    reasoning = thought("Compare counts.", "r")
    call = AistudioPart(
        function_call=("lookup", {"row": 2}, "A"), thought_signature=signature("a")
    )
    route.wire.reply(reasoning, *(AistudioPart(text=x) for x in _PIECES), call)
    params: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": 160,
        "messages": [_PROMPT],
        "output_config": {"effort": "low"},
    }
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **options
    ) as client:
        api = (client.beta if beta else client).messages
        if stream:
            async with api.stream(**params, output_format=Label) as events:
                prior = await events.get_final_message()
        else:
            prior = await api.parse(**params, output_format=Label)
        assert prior.parsed_output == Label("Café 東", 2)
        assert prior.stop_reason == "tool_use"
        assert [block.type for block in prior.content] == [
            "thinking",
            "text",
            "tool_use",
        ]
        visible = prior.content[1]
        assert visible.type == "text" and visible.text == _JSON
        route.wire.reply(AistudioPart(text="Noted."))
        params["messages"] = [
            _PROMPT,
            prior.to_param(),
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "A", "content": "Recorded."}
                ],
            },
        ]
        final = await api.create(**params)
        assert final.stop_reason == "end_turn"
    config = sent[0]["output_config"]
    assert config["effort"] == "low" and config["format"]["type"] == "json_schema"
    schema = config["format"]["schema"]
    assert set(schema["properties"]) == {"label", "count"}
    assert set(schema["required"]) == {"label", "count"}
    assert "parsed_output" not in sent[1]["messages"][1]["content"][1]
    first_cfg, next_cfg = (request[TopLevelIndex.GENERATION_CONFIG] for request in wire)
    # Only the response projection is opt-in: the prepared config matches
    # the same untyped request exactly, including its original effort hint.
    assert first_cfg == next_cfg
    assert first_cfg[GenerationConfigIndex.RESPONSE_SCHEMA] is None
    assert wire[1][TopLevelIndex.CONTENTS][1:] == [
        [
            [reasoning.to_wire(), AistudioPart(text=_JSON).to_wire(), call.to_wire()],
            "model",
        ],
        [
            [
                AistudioPart(
                    function_response=("lookup", {"result": "Recorded."}, "A")
                ).to_wire()
            ],
            "user",
        ],
    ]


@pytest.mark.parametrize(
    "kind", ["valid", "empty", "unknown", "missing", "malformed", "effort", "none"]
)
async def test_only_explicit_json_schema_changes_buffered_text_runs(route, kind):
    config = {
        "valid": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
        "empty": {"format": {"type": "json_schema", "schema": {}}},
        "unknown": {"format": {"type": "unknown", "schema": {}}},
        "missing": {"format": {"type": "json_schema"}},
        "malformed": {"format": {"type": "json_schema", "schema": []}},
        "effort": {"effort": "low"},
        "none": None,
    }[kind]
    # No merging across thinking, gateway media notices or real tool blocks.
    reasoning = thought("hidden", "boundary")
    call = AistudioPart(function_call=("lookup", {}, "A"))
    route.wire.reply(
        AistudioPart(text="lead "),
        AistudioPart(text="<EN"),
        reasoning,
        AistudioPart(text="a"),
        AistudioPart(text="b"),
        _MEDIA,
        AistudioPart(text="mid"),
        call,
        AistudioPart(text="last "),
        AistudioPart(text="<EN"),
        AistudioPart(text="D>gone"),
        AistudioPart(text="never"),
    )
    reply = await route.raw.post(
        "/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 160,
            "messages": [_PROMPT],
            "stop_sequences": ["<END>"],
            **({"output_config": config} if config is not None else {}),
        },
    )
    assert reply.status_code == 200
    message = reply.json()
    assert (message["stop_reason"], message["stop_sequence"]) == (
        "stop_sequence",
        "<END>",
    )
    groups = [block.get("text", block["type"]) for block in message["content"]]
    notice = next(text for text in groups if "Image output is unavailable" in text)
    structured = kind in ("valid", "empty")
    assert groups == [
        "lead <EN",
        "thinking",
        *(["ab"] if structured else ["a", "b"]),
        notice,
        "mid",
        "tool_use",
        "last ",
    ]


@pytest.mark.parametrize(
    ("boundary", "extra"),
    [
        ({"executableCode": {"language": "PYTHON", "code": "code"}}, ["code"]),
        ({"codeExecutionResult": {"outcome": "OK", "output": "result"}}, ["result"]),
        ({"functionResponse": {"name": "lookup", "response": {}}}, []),
        ({"thought": True, "text": ""}, []),
        ({"text": "", "thoughtSignature": signature("empty")}, []),
        ({"text": "caption", "functionResponse": {"name": "lookup"}}, ["caption"]),
    ],
)
def test_nontext_source_breaks_runs_even_when_rendered_as_text_or_omitted(
    boundary, extra
):
    # The stop matcher withholds "<E" until it sees subsequent text. Even
    # when a Part has no Claude block, those characters belong on the left.
    parts = [
        {"text": "left"},
        {"text": "<E"},
        boundary,
        {"text": "right"},
        {"text": "!"},
        {"text": "A"},
        {"text": "BC"},
        {"text": "D"},
    ]
    response = GeminiGenerateContentResponse.model_validate(
        {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
    )
    result = build_claude_message(
        response,
        MODEL,
        ["<END>", "ABCD", "BC"],
        output_config={"format": {"type": "json_schema", "schema": {}}},
    )
    # BC completes a frame earlier. Joining before stop selection would match ABCD.
    assert result["stop_reason"] == "stop_sequence" and result["stop_sequence"] == "BC"
    assert result["content"] == [
        {"type": "text", "text": value} for value in ["left<E", *extra, "right!A"]
    ]


async def test_parse_does_not_repair_an_incomplete_value(route):
    route.wire.reply(
        AistudioPart(text='{"label":"x",'),
        AistudioPart(text='"count":'),
        AistudioPart(text="<END>2}"),
    )
    async with anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid", **_client_options(route)
    ) as client:
        with pytest.raises(ValidationError, match="Invalid JSON"):
            await client.messages.parse(
                model=MODEL,
                max_tokens=160,
                messages=[{"role": "user", "content": "Read."}],
                stop_sequences=["<END>"],
                output_format=Label,
            )
