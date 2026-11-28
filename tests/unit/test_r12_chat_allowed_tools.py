"""Check allowed tool identity through OpenAI SDK requests and serialized wire bodies."""

from __future__ import annotations

import httpx2
import openai
import pytest
from fastapi import HTTPException
from test_signed_part_roundtrip import HEADERS, MODEL, route as route

from aistudio_api.api.app import http_exception_handler
from aistudio_api.api.dependencies import get_client
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioPart,
    wire_function_name,
)


@pytest.fixture
async def sdk(route):
    async with openai.AsyncOpenAI(
        api_key="fixture",
        base_url="http://fixture.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(route.app)),
        default_headers=HEADERS,
        max_retries=0,
        _strict_response_validation=True,
    ) as client:
        yield client


@pytest.fixture
def sent(route):
    calls = []
    codec = AistudioWireCodec()
    route.app.add_exception_handler(HTTPException, http_exception_handler)

    async def prepare(*, model, contents, tools, tool_config, **kwargs):
        request = codec.build_request(
            model=model, contents=contents, tools=tools, tool_config=tool_config
        )
        body = codec.encode(request)
        calls.append(
            [
                (decl[0], decl[1] if len(decl) > 1 else None)
                for _, declarations in codec.decode(body).tools or []
                for decl in declarations
            ]
        )
        return PreparedRequest(url="http://fixture.invalid", headers={}, body=body)

    route.app.dependency_overrides[get_client]().prepare_request = prepare
    return calls


async def _select(sdk, shape, names, selected, mode="auto"):
    declarations = [{"name": n, "description": n} for n in names]
    if shape == "responses":
        response = await sdk.responses.create(
            model=MODEL,
            input="Hello",
            tools=[{"type": "function", **d} for d in declarations],
            tool_choice={
                "type": "allowed_tools",
                "mode": mode,
                "tools": [{"type": "function", "name": n} for n in selected],
            },
        )
    else:
        allowed = {
            "mode": mode,
            "tools": [{"type": "function", "function": {"name": n}} for n in selected],
        }
        response = await sdk.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            tools=[{"type": "function", "function": d} for d in declarations],
            tool_choice={
                "type": "allowed_tools",
                **({"allowed_tools": allowed} if shape == "chat" else allowed),
            },
        )
    return response.model_dump(exclude_none=True)


@pytest.mark.parametrize("mode", ["auto", "required"])
@pytest.mark.parametrize("shape", ["chat", "flat-chat", "responses"])
async def test_only_selected_function_is_declared(route, sdk, sent, shape, mode):
    # The upstream can still reply with text even for required.
    route.wire.reply(AistudioPart(text="Hello"))
    await _select(sdk, shape, ["calendar", "weather"], ["weather"], mode)
    assert sent == [[("weather", "weather")]]


LONG = "a" * 64
ALIAS = wire_function_name(LONG)


@pytest.mark.parametrize("shape", ["chat", "responses"])
@pytest.mark.parametrize("selected", [LONG, ALIAS])
async def test_original_name_selects_and_restores_only_its_declaration(
    route, sdk, sent, shape, selected
):
    route.wire.reply(AistudioPart(function_call=(ALIAS, {}, "fixture-call")))
    reply = await _select(sdk, shape, [LONG, "calendar", ALIAS], [selected])
    assert sent == [[(ALIAS, selected)]]
    if shape == "responses":
        assert reply["output"][0]["name"] == selected
    else:
        assert (
            reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
            == selected
        )


@pytest.mark.parametrize(("declared", "unknown"), [(ALIAS, LONG), (LONG, ALIAS)])
async def test_undeclared_original_or_alias_cannot_select_a_function(
    route, sdk, sent, declared, unknown
):
    route.wire.reply(AistudioPart(text="Hello"))
    await _select(sdk, "chat", [declared, "calendar"], [unknown])
    assert sent == [[]]


async def test_two_selected_names_with_one_wire_label_are_rejected(route, sdk, sent):
    route.wire.reply(AistudioPart(text="Hello"))
    with pytest.raises(openai.BadRequestError, match="same wire name"):
        await _select(sdk, "chat", [LONG, ALIAS], [LONG, ALIAS])
    assert not sent


@pytest.mark.parametrize(
    ("extra", "allowed", "reason"),
    [
        (
            {"tools": [{"type": "function", "function": {"name": "calendar"}}]},
            {
                "mode": "auto",
                "tools": [{"type": "function", "function": {"name": "weather"}}],
            },
            "mix flat and nested",
        ),
        (
            {},
            {
                "mode": "auto",
                "tools": [{"type": "custom", "custom": {"name": "weather"}}],
            },
            "supported function",
        ),
        ({}, {"mode": "auto", "tools": []}, "supported function"),
        ({}, {"tools": []}, "allowed_tools.mode"),
        ({}, {"mode": "none", "tools": []}, "allowed_tools.mode"),
        ({}, {"mode": "required", "tools": "weather"}, "must be a list"),
    ],
)
async def test_unrepresentable_chat_selection_never_exposes_all_functions(
    route, sent, extra, allowed, reason
):
    route.wire.reply(AistudioPart(text="Hello"))
    reply = await route.raw.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [
                {"type": "function", "function": {"name": n}}
                for n in ("calendar", "weather")
            ],
            "tool_choice": {
                "type": "allowed_tools",
                "allowed_tools": allowed,
                **extra,
            },
        },
    )
    assert reply.status_code == 400
    assert reason in reply.json()["error"]["message"] and not sent
