"""Candidate-free meter/ID rows are siblings of content, never content themselves."""

from __future__ import annotations

import json

import httpx
import pytest
import test_anti_truncation_variants as variants
import test_ordered_tool_routes as routes
from google import genai
from google.genai import types

from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.request_history import request_history

wire_source = routes.wire_source
variant_config = variants.variant_config


def _metadata(meter=None, rid=None, *, candidates=None):
    return [candidates, None, meter, None, None, None, None, rid]


def _group(rows, wrapping):
    if wrapping == "ndjson":
        return "\n".join(json.dumps(row) for row in rows)
    return json.dumps(rows if wrapping == "flat" else [rows])


@pytest.mark.parametrize("wrapping", ["flat", "nested", "ndjson"])
@pytest.mark.parametrize("finish", [None, 1, 2])
def test_ordered_metadata_siblings_cannot_create_a_part_or_terminal(wrapping, finish):
    # A finish belongs to the real chunk, even if the following meter-only
    # row arrives after it. Sparse output borrows just this attempt's prompt/cache.
    part = routes.text("kept", signature="opaque")
    rows = [
        _metadata([17, 1, 18, 4], "initial"),
        routes.chunk([part], finish),
        _metadata([None, "7"]),
        _metadata(rid="final"),
    ]
    output = parse_text_output(_group(rows, wrapping))
    (candidate,) = output.candidates
    assert candidate.raw_parts == [part] and candidate.text == "kept"
    assert candidate.ordered_parts and len(candidate.ordered_parts) == 1
    assert candidate.ordered_parts[0].thought_signature == "opaque"
    assert candidate.ordered_parts[0].wire_chunk == 1
    assert candidate.finish_reason == finish and candidate.tool_interrupted == (
        finish == 2
    )
    assert output.response_id == "final"
    assert output.usage["total_tokens"] == 24 and output.usage["cached_tokens"] == 4


@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize(
    ("meter", "rid", "expected"),
    [
        ([0], None, {"prompt_tokens": 0}),
        ([None, 0], None, {"completion_tokens": 0}),
        ([None, None, 0], None, {"total_tokens": 0}),
        ([None, None, None, 0], None, {"cached_tokens": 0}),
        ([None] * 9 + [3], None, {"completion_tokens": 3}),
        (None, "id-only", {}),
        (None, "", {}),
        (None, 1, {}),
        ([False, True, {}, [], "details"], None, {}),
        ([None] * 9, None, {}),
        (_metadata([9, 6, 15], "decoy"), None, {}),
    ],
)
def test_lone_null_candidate_row(layers, meter, rid, expected):
    value = _metadata(meter, rid)
    for _ in range(layers):
        value = [value]
    output = parse_text_output(json.dumps(value))
    assert not output.candidates
    assert output.response_id == (rid if isinstance(rid, str) else "")
    if expected:
        assert output.usage.items() >= expected.items()
    else:
        assert output.usage == {}


@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize("meter", [False, True])
def test_single_candidate_shape_is_not_a_batch_of_fields(layers, meter):
    part = routes.text("real")
    row = routes.chunk([part], 1)
    if meter:
        # A meter field is not a sibling chunk, even if an unused slot looks like an ID.
        row = [*row, None, [None, 2, None, None, None, None, None, "decoy"]]
    for _ in range(layers):
        row = [row]
    output = parse_text_output(json.dumps(row))
    assert output.text == "real" and output.candidates[0].finish_reason == 1
    assert output.candidates[0].raw_parts == [part] and output.response_id == ""
    assert output.usage.get("completion_tokens") == (2 if meter else None)


@pytest.mark.parametrize("suffix", ["", "-at", "-at-stream"])
async def test_sdk_final_usage_and_id_match_actual_transport(
    wire_source, variant_config, suffix
):
    rid = "v1_" + "f" * 24
    # A canned presentation call is restored to text by both -at transports.
    # No function is registered or executed.
    early = [] if suffix else [routes.text("one ")]
    final = (
        [routes.call("router_emit", {"content": "one two"}, "fixture")]
        if suffix
        else [routes.text("two")]
    )
    rows = [
        [*routes.chunk(early), None, [17, 1, 18, 4]],
        routes.chunk(final, 1),
        _metadata([None, 7], rid),
    ]
    wire_source.raw = routes.wire(*rows)
    ids = []

    async def remember(response):
        ids.append(response.headers["x-request-id"])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(routes.app),
        event_hooks={"response": [remember]},
    ) as http:
        options = types.HttpOptions(
            base_url="http://fixture.invalid",
            api_version="v1beta",
            headers={"X-Anti-Truncation": "1" if suffix else "0"},
            httpx_async_client=http,
        )
        model = "gemini-3.7-flash" + suffix
        config = types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            )
        )
        async with genai.Client(api_key="fixture", http_options=options).aio as client:
            message = await client.models.generate_content(
                model=model, contents="Hi", config=config
            )
            stream = await client.models.generate_content_stream(
                model=model, contents="Hi", config=config
            )
            frames = [frame async for frame in stream]
    assert message.text == "".join(frame.text or "" for frame in frames) == "one two"
    for result in (message, frames[-1]):
        assert result.response_id == rid
        assert (
            result.candidates
            and result.candidates[0].finish_reason == types.FinishReason.STOP
        )
        usage = result.usage_metadata
        assert usage and (
            usage.prompt_token_count,
            usage.candidates_token_count,
            usage.total_token_count,
            usage.cached_content_token_count,
        ) == (17, 7, 24, 4)
    assert len(set(ids)) == len(ids) == 2
    for request_id in ids:
        row = request_history().get(request_id)
        assert row and row["result"] == "success"
        recorded = row["usage"]
        assert isinstance(recorded, dict) and recorded["total_tokens"] == 24


def test_numeric_error_array_is_not_an_envelope_of_metadata_rows():
    # Scalar discriminator and unrelated payload fields are neither candidate
    # rows nor a batch. An inner array in slot 2 must not donate a response ID.
    output = parse_text_output(
        json.dumps([403, "error", _metadata([9, 6, 15], "decoy")])
    )
    assert output.candidates == [] and output.usage == {} and output.response_id == ""


def test_a_metadata_row_flag_does_not_prove_a_finish():
    row = _metadata([4, 1, 5])
    row[1] = 1
    for value in (row, [row], [[row]]):
        output = parse_text_output(json.dumps(value))
        assert not output.candidates and output.usage["total_tokens"] == 5


def test_empty_candidate_container_and_finishes_still_parse():
    for row, finish in (([[]], None), ([[[None, 1]]], 1)):
        output = parse_text_output(json.dumps(row))
        (candidate,) = output.candidates
        assert candidate.finish_reason == finish and candidate.ordered_parts == []


@pytest.mark.parametrize("container", [[], [[None]]])
def test_unusable_chunk_does_not_treat_its_array_fields_as_siblings(container):
    raw = [container, [], _metadata([9, 6, 15], "decoy")]
    for value in (raw, [raw], [[raw]]):
        output = parse_text_output(json.dumps(value))
        assert (
            not output.has_content and output.usage == {} and output.response_id == ""
        )


@pytest.mark.parametrize("wrapping", ["flat", "nested", "ndjson"])
def test_roleless_candidate_with_metadata_tail(wrapping):
    part = routes.text("retained")
    row = routes.chunk([part], 1)
    row[0][0][0].pop()  # role is optional
    output = parse_text_output(_group([row, _metadata([6, 4, 10], "tail")], wrapping))
    assert output.text == "retained" and output.candidates[0].finish_reason == 1
    assert output.response_id == "tail" and output.usage["total_tokens"] == 10


@pytest.mark.parametrize("wrapping", ["flat", "nested", "ndjson"])
def test_finish_only_candidate_keeps_its_own_fields_and_late_metadata(wrapping):
    stop = [[[None, 2, None, "quota", []]]]
    output = parse_text_output(_group([stop, _metadata([6, 4, 10], "tail")], wrapping))
    (candidate,) = output.candidates
    assert (
        not candidate.raw_parts
        and not candidate.ordered_parts
        and not candidate.has_content
    )
    assert candidate.finish_reason == 2 and candidate.finish_message == "quota"
    assert candidate.tool_interrupted
    assert output.response_id == "tail" and output.usage["total_tokens"] == 10
