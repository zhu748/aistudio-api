"""Stock Responses SDK snapshots describe declarations, not tool calls to execute."""

import asyncio
import gc
import json
import weakref
from collections.abc import AsyncGenerator

import pytest
from openai.types.responses import (
    ResponseCreatedEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseInProgressEvent,
)
from test_r12_chat_allowed_tools import sdk as sdk, sent as sent
from test_responses_failure_sdk import (
    _arm_wire_deadline,
    _ASGIObserver,
    isolated_stream as isolated_stream,
)
from test_signed_part_roundtrip import MODEL, route as route, signature

from aistudio_api.api import routes_compat
from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application.api_service_gemini import GeminiStreamResponse
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioPart,
    wire_function_name,
)

_TINT = "preview_tint"
_SHAPE = "preview_shape"
_SCHEMA = {"type": "object", "properties": {"label": {"type": "string"}}}


def _function(name):
    return {"type": "function", "name": name}


def _allow(*names, mode="auto"):
    return {
        "type": "allowed_tools",
        "mode": mode,
        "tools": [_function(name) for name in names],
    }


def _metadata(response):
    choice = response.tool_choice
    return (
        [tool.model_dump(exclude_unset=True) for tool in response.tools],
        choice if isinstance(choice, str) else choice.model_dump(exclude_unset=True),
        response.parallel_tool_calls,
    )


async def _completed_responses(sdk, **changes):
    request = {"model": MODEL, "input": "Describe the sample.", **changes}
    unary = await sdk.responses.create(**request)
    async with sdk.responses.stream(**request) as stream:
        snapshots = [
            event
            async for event in stream
            if event.type
            in ("response.created", "response.in_progress", "response.completed")
        ]
        final = await stream.get_final_response()
    assert [e.type for e in snapshots] == [
        "response.created",
        "response.in_progress",
        "response.completed",
    ]
    assert not snapshots[0].response.output and not snapshots[1].response.output
    responses = [unary, *(e.response for e in snapshots), final]
    assert all(_metadata(response) == _metadata(unary) for response in responses)
    return unary, snapshots[-1].response, final


@pytest.mark.parametrize("selected", [_SHAPE, "p" * 64])
async def test_declared_catalog_distinct_from_active_wire_selection(
    route, sdk, sent, selected
):
    tools = [
        {**_function(n), "description": n, "parameters": _SCHEMA, "strict": False}
        for n in (_TINT, selected)
    ]
    wire_name = wire_function_name(selected)
    call = AistudioPart(
        function_call=(wire_name, {"label": "blue"}, "fixture-shape"),
        thought_signature=signature("shape owner"),
    )
    route.wire.reply(call)
    for response in await _completed_responses(
        sdk, tools=tools, tool_choice=_allow(selected)
    ):
        assert _metadata(response) == (tools, _allow(selected), True)
        assert len(response.output) == 1
        item = response.output[0].model_dump(exclude_none=True)
        assert (item["name"], item["call_id"], json.loads(item["arguments"])) == (
            selected,
            "fixture-shape",
            {"label": "blue"},
        )
        assert item["extra_content"]["google"]["thought_signature"] == signature(
            "shape owner"
        )
    # The catalog still has both original names. Only the one granted by the
    # allowlist is prepared upstream, shortened when needed.
    assert sent == [[(wire_name, selected)]] * 2


@pytest.mark.parametrize("scenario", ["parallel_false", "required"])
async def test_projection_does_not_enforce_serial_or_required(
    route, sdk, sent, scenario
):
    tools = [_function(_TINT), _function(_SHAPE)]
    public = [
        {**t, "description": None, "parameters": None, "strict": None} for t in tools
    ]
    request = {"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False}
    if scenario == "parallel_false":
        parts = [
            AistudioPart(
                function_call=(n, {"label": i}, f"mock-{i}"),
                thought_signature=signature(n),
            )
            for i, n in enumerate((_TINT, _SHAPE))
        ]
        route.wire.reply(*parts)
    else:
        request["tool_choice"] = "required"
        route.wire.reply(AistudioPart(text="Only text."))
    for response in await _completed_responses(sdk, **request):
        assert _metadata(response) == (public, "auto", True)
        if scenario == "required":
            assert len(response.output) == 1 and response.output_text == "Only text."
        else:
            assert [
                (
                    item.name,
                    item.call_id,
                    json.loads(item.arguments),
                    item.extra_content["google"]["thought_signature"],
                )
                for item in response.output
            ] == [
                (n, f"mock-{i}", {"label": i}, signature(n))
                for i, n in enumerate((_TINT, _SHAPE))
            ]
    assert sent == [[(_TINT, None), (_SHAPE, None)]] * 2


@pytest.mark.parametrize(
    ("request_fields", "public", "choice"),
    [
        ({}, [], "auto"),
        ({"tools": []}, [], "auto"),
        ({"tools": [{"type": "web_search"}]}, [], "auto"),
        ({"tools": [_function(_TINT)], "tool_choice": "none"}, [_TINT], "none"),
        (
            {"tools": [_function(_TINT)], "tool_choice": _allow("unknown")},
            [_TINT],
            "none",
        ),
        (
            {"tools": [_function(_TINT)], "tool_choice": _function(_TINT)},
            [_TINT],
            _allow(_TINT),
        ),
        (
            {
                "tools": [
                    {"type": "custom", "name": "other"},
                    {"type": "function", "name": 17},
                    {**_function(_TINT), "strict": "bogus", "parameters": "bogus"},
                    {**_function(_SHAPE), "strict": True, "defer_loading": True},
                ],
                "tool_choice": _allow("other", _SHAPE, _SHAPE, mode="required"),
            },
            [_TINT, _SHAPE],
            _allow(_SHAPE),
        ),
    ],
)
async def test_sdk_projection_of_defaults_and_normalized_inputs(
    route, sdk, request_fields, public, choice
):
    route.wire.reply(AistudioPart(text="Only text."))
    response = await sdk.responses.create(
        model=MODEL, input="Sample?", **request_fields
    )
    assert _metadata(response) == (
        [
            {
                **_function(name),
                "description": None,
                "parameters": None,
                "strict": False if name == _SHAPE else None,
            }
            for name in public
        ],
        choice,
        True,
    )


@pytest.mark.parametrize("outcome", ["preparation", "deadline", "failed", "incomplete"])
async def test_failed_and_incomplete_streams_keep_only_public_tool_metadata(
    monkeypatch, isolated_stream, outcome
):
    expire = _arm_wire_deadline(monkeypatch)
    states = []
    request_refs = []

    def after_send(message):
        if outcome == "deadline" and message.get("body", b"").startswith(
            b"event: response.in_progress\n"
        ):
            expire()

    class RetainState(_ASGIObserver):
        async def __call__(self, scope, receive, send):
            await super().__call__(scope, receive, send)
            states.append(scope["state"])

    observer = RetainState(after_send)
    convert = routes_compat.responses_to_gemini_request

    async def prepare(request):
        request_refs.append(weakref.ref(request))
        if outcome == "preparation":
            # After a heartbeat, the real conversion error must use Responses
            # lifecycle events even though the native request never existed.
            await observer.heartbeat.wait()
        return await convert(request)

    async def frames() -> AsyncGenerator[dict[str, object], None]:
        if outcome == "deadline":
            await asyncio.Event().wait()
        if outcome == "failed":
            yield {"error": {"code": 429, "message": "mock unavailable"}}
        else:
            yield {"candidates": [{"finishReason": "MAX_TOKENS"}]}

    async def generate(*args, **kwargs):
        assert outcome != "preparation"
        return GeminiStreamResponse(frames())

    monkeypatch.setattr(routes_compat, "responses_to_gemini_request", prepare)
    monkeypatch.setattr(routes_compat, "handle_gemini_generate_content", generate)
    monkeypatch.setitem(app.dependency_overrides, get_client, object)
    options = (
        {"previous_response_id": "resp_fixture"} if outcome == "preparation" else {}
    )
    fields = {
        "model": MODEL,
        "input": "Sample?",
        "tools": [_function(_TINT), {"type": "custom", "name": "other"}],
        "tool_choice": _allow("other", _TINT),
        **options,
    }
    async with (
        asyncio.timeout(3),
        observer.client() as sdk,
        sdk.responses.stream(**fields) as stream,
    ):
        events = [e async for e in stream]
    terminal = "response.incomplete" if outcome == "incomplete" else "response.failed"
    assert [e.type for e in events] == (
        ["response.created"]
        + (["response.in_progress"] if outcome != "preparation" else [])
        + [terminal]
    )
    assert [e.sequence_number for e in events] == list(range(len(events)))
    response_ids = set()
    for event in events:
        assert isinstance(
            event,
            (
                ResponseCreatedEvent,
                ResponseInProgressEvent,
                ResponseFailedEvent,
                ResponseIncompleteEvent,
            ),
        )
        response_ids.add(event.response.id)
        assert _metadata(event.response) == (
            [
                {
                    **_function(_TINT),
                    "description": None,
                    "parameters": None,
                    "strict": None,
                }
            ],
            _allow(_TINT),
            True,
        )
    assert len(response_ids) == 1
    if outcome in ("preparation", "deadline"):
        failed = events[-1]
        assert (
            isinstance(failed, ResponseFailedEvent)
            and failed.response.error is not None
        )
        original_error = (
            "previous_response_id" if outcome == "preparation" else "total timeout"
        )
        assert original_error in failed.response.error.message
    assert len(states) == 1 and not {
        "stream_failure",
        "responses_accept_event",
    }.intersection(states[0])
    gc.collect()
    assert len(request_refs) == 1 and request_refs[0]() is None
