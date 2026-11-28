"""A stable ID anchors call order, not a license to splice signed snapshots."""

from __future__ import annotations

import json
from typing import cast

import httpx2
import openai
import pytest
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionUserMessageParam,
)
from test_ordered_tool_routes import (
    call,
    chunk,
    input_request,
    native_parts,
    sse_events,
    wire,
)
from test_signed_part_roundtrip import (
    HEADERS,
    MODEL,
    PROMPT,
    AistudioPart,
    route as route,
    signature,
)

from aistudio_api.application import stream_utils

OLD = signature("first name")
NEW = signature("replacement name")
OTHER = signature("unrelated owner")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("first", "last"), [("lookup_city", "lookup"), ("lookup", "lookup_city")]
)
@pytest.mark.parametrize(("first_sig", "last_sig"), [(None, NEW), (OLD, None)])
async def test_stock_chat_replays_only_the_actual_named_snapshot(
    route, stream, first, last, first_sig, last_sig
):
    # Give the sibling the first owner's name and the replacement's args to
    # catch leaks on either name/argument lookup as well as by call index.
    route.wire.raw = wire(
        chunk(
            [
                call(first, {"id": 1}, "A", signature=first_sig),
                call(first, {"id": 2}, "B", signature=OTHER),
            ]
        ),
        chunk([call(last, {"id": 2}, "A", signature=last_sig)], 1),
    )
    prompt: ChatCompletionUserMessageParam = {
        "role": "user",
        "content": PROMPT["content"],
    }
    async with openai.AsyncOpenAI(
        base_url="http://fixture.invalid/v1",
        api_key="fixture",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(route.app)),
        default_headers=HEADERS,
        _strict_response_validation=True,
        max_retries=0,
    ) as sdk:
        if stream:
            async with sdk.chat.completions.stream(
                model=MODEL, messages=[prompt]
            ) as events:
                completion = await events.get_final_completion()
        else:
            completion = await sdk.chat.completions.create(
                model=MODEL, messages=[prompt]
            )
        choice = completion.choices[0]
        assert choice.finish_reason == "tool_calls"
        assistant = choice.message.model_dump(exclude_none=True)
        assert [
            (
                tool["id"],
                tool["function"]["name"],
                json.loads(tool["function"]["arguments"]),
                tool.get("extra_content", {})
                .get("google", {})
                .get("thought_signature"),
            )
            for tool in assistant["tool_calls"]
        ] == [("A", last, {"id": 2}, last_sig), ("B", first, {"id": 2}, OTHER)]
        route.wire.reply(AistudioPart(text="Done."))
        await sdk.chat.completions.create(
            model=MODEL,
            messages=[
                prompt,
                cast(ChatCompletionAssistantMessageParam, assistant),
                {"role": "tool", "tool_call_id": "A", "content": "OK"},
                {"role": "tool", "tool_call_id": "B", "content": "OK"},
            ],
        )
    assert [
        (
            p.function_call["call_id"],
            p.function_call["name"],
            p.function_call["args"],
            p.thought_signature,
        )
        for p in route.wire.replayed()
        if p.function_call
    ] == [("A", last, {"id": 2}, last_sig or ""), ("B", first, {"id": 2}, OTHER)]


async def _native(route, stream, *chunks):
    route.wire.raw = wire(*chunks)
    path, params = input_request("native", stream)
    response = await route.raw.post(path, json=params)
    assert response.status_code == 200, response.text
    events = sse_events(response.text) if stream else [response.json()]
    return native_parts(events), events[-1]["candidates"][0]["finishReason"]


def _part(name, args, call_id="A", sig=None):
    return {
        "functionCall": {"name": name, "args": args, "id": call_id},
        **({"thoughtSignature": sig} if sig else {}),
    }


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("pieces", "expected"),
    [
        (['{"node":', '{"node":1}}'], {"node": {"node": 1}}),
        ([' {"node":', ' {"node":1} } '], {"node": {"node": 1}}),
        (['{"node":', '{"node":1', "}"], {"node": 1}),  # real cumulative partial
        (['{"node":', '{"node":1}'], {"node": 1}),  # real complete snapshot
        (['{"node":', '{"node":1}}garbage}'], None),
    ],
)
async def test_self_similar_delta_and_its_parallel_sibling(
    route, stream, pieces, expected
):
    frames = [
        chunk([call("inspect", item, "A", signature=OLD if i == 0 else None)])
        for i, item in enumerate(pieces)
    ]
    parts, reason = await _native(
        route, stream, *frames, chunk([call("other", {}, "B", signature=OTHER)], 1)
    )
    calls = [p for p in parts if "functionCall" in p]
    if expected is None:
        assert calls == [] and reason == "MALFORMED_FUNCTION_CALL"
        assert OLD not in str(parts) and OTHER not in str(parts)
    else:
        assert reason == "STOP"
        assert calls == [
            _part("inspect", expected, sig=OLD),
            _part("other", {}, "B", OTHER),
        ]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("final_snapshot", [False, True])
async def test_genuine_partial_name_growth_does_not_promote_a_shard_marker(
    route, stream, final_snapshot
):
    frames = [
        chunk([call("look", '{"id":', "A", signature=OLD)]),
        chunk([call("lookup", None, "A", signature=NEW)]),
        chunk([call(None, "2}", "A", signature=NEW)]),
    ]
    if final_snapshot:
        frames.append(chunk([call("lookup", {"id": 2}, "A", signature=NEW)]))
    parts, reason = await _native(route, stream, *frames, chunk([], 1))
    assert (parts, reason) == (
        [_part("lookup", {"id": 2}, sig=NEW if final_snapshot else None)],
        "STOP",
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (call("lookup_city", {"id": 1}, "A"), call("lookup", None, "A", signature=NEW)),
        (call("lookup", {"id": 1}, "A"), call("lookup_city", None, "A", signature=NEW)),
        (call("lookup_city", '{"id":', "A"), call("lookup", "2}", "A", signature=NEW)),
        (call("lookup", '{"id":'), call("lookup_city", {"id": 2}, "A", signature=NEW)),
        (call("lookup", {"id": 1}, "A"), call("other", {"id": 2}, "A", signature=NEW)),
    ],
)
async def test_uncertain_renames_do_not_make_executable_batches(
    route, stream, first, second
):
    parts, reason = await _native(
        route, stream, chunk([first, call("sibling", {}, "B")]), chunk([second], 1)
    )
    assert reason == "MALFORMED_FUNCTION_CALL"
    assert not any(p.get("functionCall") for p in parts)
    assert NEW not in str(parts)


@pytest.mark.parametrize("stream", [False, True])
async def test_name_only_update_cannot_borrow_completed_fragmented_args(route, stream):
    parts, reason = await _native(
        route,
        stream,
        chunk([call("look", '{"id":', "A")]),
        chunk([call("look", "1}", "A")]),
        chunk([call("look", " ", "A")]),
        chunk([call("lookup", None, "A", signature=NEW)], 1),
    )
    assert reason == "MALFORMED_FUNCTION_CALL"
    assert not any(p.get("functionCall") for p in parts)


@pytest.mark.parametrize("stream", [False, True])
async def test_id_promotion_cannot_promote_an_earlier_shard_signature(route, stream):
    parts, reason = await _native(
        route,
        stream,
        chunk([call("lookup", '{"id":', signature=OLD)]),
        chunk([call("lookup", "1}", "A", signature=NEW)], 1),
    )
    assert (parts, reason) == ([_part("lookup", {"id": 1})], "STOP")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("finish", [1, 2])
async def test_changed_caption_cannot_be_spliced_into_named_owner(
    route, stream, finish
):
    first = call("lookup_city", {"id": 1}, "A", signature=OLD)
    first[1] = "First caption."
    second = call("lookup", {"id": 2}, "A", signature=NEW)
    second[1] = "Second caption."
    parts, reason = await _native(
        route, stream, chunk([first]), chunk([second], finish)
    )
    assert (parts, reason) == (
        [{"text": "First caption."}, {"text": "Second caption."}],
        "MALFORMED_FUNCTION_CALL" if finish == 1 else "MAX_TOKENS",
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("second", "expected_args"),
    [
        (call(None, {"id": 2}, "A", signature=NEW), {"id": 2}),
        (call("lookup", None, "A", signature=NEW), {"id": 1}),
    ],
)
async def test_sparse_update_cannot_newly_sign_borrowed_identity(
    route, stream, second, expected_args
):
    parts, reason = await _native(
        route, stream, chunk([call("lookup", {"id": 1}, "A")]), chunk([second], 1)
    )
    assert (parts, reason) == ([_part("lookup", expected_args)], "STOP")


def test_self_similar_delta_counts_both_fragments_in_the_byte_and_count_limits(
    monkeypatch,
):
    prefix, delta = '{"node":', '{"node":1}}'
    monkeypatch.setattr(
        stream_utils, "_MAX_TOOL_ARGUMENT_BYTES", len(prefix + delta) - 1
    )
    assembler = stream_utils.ToolResponseAssembler()
    assembler.feed([_part("inspect", prefix)])
    with pytest.raises(ValueError, match="Tool arguments exceed"):
        assembler.feed([_part("inspect", delta)])

    monkeypatch.setattr(stream_utils, "_MAX_TOOL_ARGUMENT_BYTES", 1_000_000)
    assembler = stream_utils.ToolResponseAssembler()
    # Each space is retained inside the JSON value, not a cumulative snapshot.
    assembler.feed([_part("inspect", '{"s":"')])
    for _ in range(16_383):
        assembler.feed([_part("inspect", " ")])
    with pytest.raises(ValueError, match="fragment count"):
        assembler.feed([_part("inspect", '"}')])
