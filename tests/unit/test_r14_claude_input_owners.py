"""An omitted/null Claude input is not an echo of the observed empty object."""

import json
from copy import deepcopy

import anthropic
import httpx2
import pytest
import test_signed_part_roundtrip as sdk_routes
from test_replay_signature_provenance import (
    _new_worker,
    _outputs,
    _strip_signatures,
    _wire_tools,
    route as route,
)
from test_signed_part_roundtrip import (
    HEADERS,
    MODEL,
    PROMPT,
    _claude_sdk_turn,
    signature,
    turn_params,
)

from aistudio_api.domain.tool_signatures import SKIP_THOUGHT_SIGNATURE
from aistudio_api.infrastructure.gateway.wire_spec import PartIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_MISSING = object()
stream_route = sdk_routes.route


def _sdk(app, *, on_request=None):
    return anthropic.AsyncAnthropic(
        base_url="http://fixture.invalid",
        api_key="fixture",
        default_headers=HEADERS,
        http_client=httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app),
            event_hooks={"request": [on_request]} if on_request else {},
        ),
        max_retries=0,
        _strict_response_validation=True,
    )


async def _turn(client, history):
    reply = await client.messages.create(model=MODEL, max_tokens=32, messages=history)
    return {
        "role": "assistant",
        "content": [b.model_dump(exclude_none=True) for b in reply.content],
    }


def _call(call_id, args, *, signed=True):
    return AistudioPart(
        function_call=("lookup", args, call_id),
        thought_signature=signature(call_id) if signed else None,
    )


def _results(*ids):
    return _outputs("claude", [(i, "ok") for i in ids])


async def test_only_missing_and_null_inputs_lose_the_direct_marker(route):
    _, gateway = route
    # Do not JSON-decode strings or otherwise reinterpret any non-null input.
    values = [
        _MISSING,
        None,
        {},
        {"x": [False, None]},
        [],
        [1, 2],
        ' {"x":1} ',
        '{"x":',
        "",
        " ",
        False,
        0,
        2.75,
    ]
    blocks, expected = [], []
    for n, raw in enumerate(values):
        call_id = str(n)
        blocks.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": "lookup",
                **({"input": raw} if raw is not _MISSING else {}),
                "extra_content": {"google": {"thought_signature": signature(call_id)}},
            }
        )
        exact = raw is not _MISSING and raw is not None
        expected.append(_call(call_id, raw if exact else {}, signed=exact).to_wire())
    gateway.reply(AistudioPart(text="Done."))
    async with _sdk(gateway.app) as client:
        await _turn(
            client,
            [
                PROMPT,
                {"role": "assistant", "content": blocks},
                *_results(*(str(n) for n in range(len(values)))),
            ],
        )
    # Compare serialized JSON so, in particular, false and 0 are not equal.
    assert json.dumps(gateway.wire[1][0]) == json.dumps(expected)
    assert [p._compat_args_rewritten for p in gateway.last[1].parts] == [
        raw is _MISSING or raw is None for raw in values
    ]


@pytest.mark.parametrize(
    "raw", [_MISSING, None, {}], ids=["missing", "null", "exact-empty"]
)
@pytest.mark.parametrize("direct", [False, True], ids=["stripped", "direct"])
@pytest.mark.parametrize("storage", ["warm", "cold", "disk-restart"])
async def test_input_fallback_stops_recovery_at_that_turn_only(
    route, monkeypatch, tmp_path, raw, direct, storage
):
    _, gateway = route
    exact = raw is not _MISSING and raw is not None
    async with _sdk(gateway.app) as client:
        other = {"role": "user", "content": "An unrelated question."}
        gateway.reply(_call("other", {}))
        unrelated = _strip_signatures(await _turn(client, [other]))
        gateway.reply(_call("prior", {"stage": 1}))
        prior = _strip_signatures(await _turn(client, [PROMPT]))
        prefix = [PROMPT, prior, *_results("prior")]
        gateway.reply(
            _call("A", {}), _call("B", {"n": 7}), _call("U", {"n": 0}, signed=False)
        )
        batch = await _turn(client, prefix)
        batch_results = _results("A", "B", "U")
        gateway.reply(_call("C", {}))
        child = _strip_signatures(await _turn(client, [*prefix, batch, *batch_results]))
        rest = [*batch_results, child, *_results("C")]
        if storage == "disk-restart":
            _new_worker(gateway, monkeypatch, tmp_path / "signatures.json")
        elif storage == "cold":
            gateway.history.clear()

        edited = deepcopy(batch)
        a = edited["content"][0]
        assert a["input"] == {}
        if not direct:
            a.pop("extra_content")
        if raw is _MISSING:
            del a["input"]
        else:
            a["input"] = raw
        gateway.reply(_call("D", {}))
        last = _strip_signatures(await _turn(client, [*prefix, edited, *rest]))
        prior_wire = [("prior", "lookup", {"stage": 1}, signature("prior"))]
        b = ("B", "lookup", {"n": 7}, signature("B"))
        u = ("U", "lookup", {"n": 0}, "")
        assert _wire_tools(gateway, 1, "function_call") == prior_wire
        assert _wire_tools(gateway, 3, "function_call") == [
            ("A", "lookup", {}, signature("A") if exact else ""),
            b,
            u,
        ]
        assert _wire_tools(gateway, 5, "function_call") == [
            ("C", "lookup", {}, signature("C") if exact else "")
        ]

        # The original clean transcript still recovers. A response following
        # a rewritten input cannot become evidence under that clean transcript.
        gateway.reply(AistudioPart(text="Done."))
        await _turn(
            client, [*prefix, _strip_signatures(batch), *rest, last, *_results("D")]
        )
        assert _wire_tools(gateway, 1, "function_call") == prior_wire
        assert _wire_tools(gateway, 3, "function_call") == [
            ("A", "lookup", {}, signature("A")),
            b,
            u,
        ]
        assert _wire_tools(gateway, 5, "function_call") == [
            ("C", "lookup", {}, signature("C"))
        ]
        assert _wire_tools(gateway, 7, "function_call") == [
            ("D", "lookup", {}, signature("D") if exact else SKIP_THOUGHT_SIGNATURE)
        ]
        await _turn(client, [other, unrelated, *_results("other")])
        assert _wire_tools(gateway, 1, "function_call") == [
            ("other", "lookup", {}, signature("other"))
        ]


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize(
    "upstream",
    [
        ["lookup", [[]], "A"],
        ["lookup"],
        {"name": "lookup", "id": "A"},
        ["lookup", None, "A"],
    ],
    ids=["empty", "name-only", "absent", "null"],
)
async def test_sdk_no_argument_blocks_replay_an_explicit_empty_object(
    stream_route, stream, upstream
):
    sent = []

    async def observe(request):
        sent.append(json.loads(await request.aread()))

    async with _sdk(stream_route.app, on_request=observe) as client:
        wire = _call("A", {}).to_wire()
        wire[PartIndex.FUNCTION_CALL_ALT] = upstream
        stream_route.wire.reply(AistudioPart(raw_wire=wire))
        params = turn_params("claude")
        message = await _claude_sdk_turn(client, params, stream)
        (block,) = message.content
        assert (
            block.type == "tool_use"
            and block.input == {}
            and "input" in block.model_fields_set
        )

        for echo in (message.content, [block.model_dump(exclude_none=True)]):
            stream_route.wire.reply(AistudioPart(text="Done."))
            await _turn(
                client,
                [PROMPT, {"role": "assistant", "content": echo}, *_results(block.id)],
            )
            assert sent[-1]["messages"][1]["content"][0]["input"] == {}
            (replayed,) = stream_route.wire.replayed()
            assert replayed.thought_signature == signature("A")
