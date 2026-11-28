"""Imported call names retain their display fallback, not an observed call's state."""

from copy import deepcopy

import pytest
from test_r14_chat_detail_owners import _sdk as openai_sdk
from test_r14_claude_input_owners import _sdk as claude_sdk
from test_replay_signature_provenance import (
    MODEL,
    _new_worker,
    _outputs,
    _request,
    _strip_signatures,
    _wire_tools,
    route as route,
)
from test_signed_part_roundtrip import signature

from aistudio_api.api.schemas import GeminiFunctionCall
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

_SURFACES = ["chat", "responses", "claude"]
_PROMPT = {"role": "user", "content": "Read both labels."}
_MISSING = object()


def _sdk(app, surface):
    return claude_sdk(app) if surface == "claude" else openai_sdk(app)


async def _turn(sdk, surface, history):
    if surface == "chat":
        reply = await sdk.chat.completions.create(model=MODEL, messages=history)
        return [reply.choices[0].message.model_dump(exclude_none=True)]
    if surface == "responses":
        reply = await sdk.responses.create(model=MODEL, input=history)
        return [item.model_dump(exclude_none=True) for item in reply.output]
    reply = await sdk.messages.create(model=MODEL, messages=history, max_tokens=32)
    return [
        {
            "role": "assistant",
            "content": [block.model_dump(exclude_none=True) for block in reply.content],
        }
    ]


def _name_field(history, surface):
    if surface == "chat":
        carrier = history[0]["tool_calls"][0]
        return carrier, carrier["function"]
    carrier = history[0]["content"][0] if surface == "claude" else history[0]
    return carrier, carrier


def _part(name, call_id, page):
    return AistudioPart(
        function_call=(name, {"page": page}, call_id),
        thought_signature=signature(call_id),
    )


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("restart", [False, True], ids=["warm", "disk-restart"])
async def test_synthetic_name_is_not_the_observed_literal_unknown(
    route, monkeypatch, tmp_path, surface, restart
):
    _, gateway = route
    async with _sdk(gateway.app, surface) as sdk:
        other_prompt = {"role": "user", "content": "Read a different label."}
        gateway.reply(_part("lookup", "other", 0))
        unrelated = _strip_signatures(await _turn(sdk, surface, [other_prompt]))

        gateway.reply(_part("unknown", "A", 1), _part("lookup", "B", 2))
        batch = await _turn(sdk, surface, [_PROMPT])
        results = _outputs(surface, [("B", "right"), ("A", "left")])
        gateway.reply(_part("next_page", "C", 3))
        child = _strip_signatures(
            await _turn(sdk, surface, [_PROMPT, *batch, *results])
        )
        rest = [*results, *child, *_outputs(surface, [("C", "done")])]
        if restart:
            _new_worker(gateway, monkeypatch, tmp_path / "signatures.json")

        for raw_name in (_MISSING, None, "", 17):
            for direct in (False, True):
                edited = deepcopy(batch)
                carrier, fields = _name_field(edited, surface)
                assert fields["name"] == "unknown"
                if raw_name is _MISSING:
                    del fields["name"]
                else:
                    fields["name"] = raw_name
                if not direct:
                    carrier.pop("extra_content")
                # Preserve B's explicit state; the stripped child cannot recover
                # through a turn with an invented name, even with the same IDs.
                gateway.reply(_part("last_page", "D", 4))
                after_edit = _strip_signatures(
                    await _turn(sdk, surface, [_PROMPT, *edited, *rest])
                )
                assert _wire_tools(gateway, 1, "function_call") == [
                    ("A", "unknown", {"page": 1}, ""),
                    ("B", "lookup", {"page": 2}, signature("B")),
                ]
                assert _wire_tools(gateway, 2, "function_response") == [
                    ("B", "lookup", {"result": "right"}, ""),
                    ("A", "unknown", {"result": "left"}, ""),
                ]
                assert _wire_tools(gateway, 3, "function_call") == [
                    ("C", "next_page", {"page": 3}, ""),
                ]

        # The exact clean batch and child still recover; a response seen only
        # after an edited turn has not been stored as their continuation.
        gateway.reply(AistudioPart(text="Done."))
        await _turn(
            sdk,
            surface,
            [
                _PROMPT,
                *_strip_signatures(batch),
                *rest,
                *after_edit,
                *_outputs(surface, [("D", "done")]),
            ],
        )
        assert _wire_tools(gateway, 1, "function_call") == [
            ("A", "unknown", {"page": 1}, signature("A")),
            ("B", "lookup", {"page": 2}, signature("B")),
        ]
        assert _wire_tools(gateway, 3, "function_call") == [
            ("C", "next_page", {"page": 3}, signature("C")),
        ]
        assert _wire_tools(gateway, 5, "function_call")[0][-1] != signature("D")
        await _turn(
            sdk,
            surface,
            [other_prompt, *unrelated, *_outputs(surface, [("other", "OK")])],
        )
        assert _wire_tools(gateway, 1, "function_call") == [
            ("other", "lookup", {"page": 0}, signature("other")),
        ]


@pytest.mark.parametrize("surface", _SURFACES)
async def test_nameless_call_cannot_overwrite_the_result_name_index(route, surface):
    _, gateway = route
    async with _sdk(gateway.app, surface) as sdk:
        gateway.reply(_part("lookup", "row", 1))
        named = await _turn(sdk, surface, [_PROMPT])
        missing = deepcopy(named)
        del _name_field(missing, surface)[1]["name"]
        idless = deepcopy(named)
        del _name_field(idless, surface)[0][
            "call_id" if surface == "responses" else "id"
        ]
        results = _outputs(surface, [("row", "OK")])
        gateway.reply(AistudioPart(text="Done."))
        await _turn(
            sdk,
            surface,
            [_PROMPT, *named, *results, *missing, *results, *idless, *results],
        )
        # First two match by exact pending ID, even on the imported call. The
        # third has no call ID: its result uses the separately indexed real name.
        for turn, expected_name in [(2, "lookup"), (4, "unknown"), (6, "lookup")]:
            assert _wire_tools(gateway, turn, "function_response") == [
                ("row", expected_name, {"result": "OK"}, "")
            ]


async def test_native_literal_unknown_cannot_acquire_private_compat_provenance(route):
    client, gateway = route
    prompt = {"role": "user", "parts": [{"text": "Read it."}]}
    part = _part("unknown", "row", 1)
    gateway.reply(part)
    native = _strip_signatures(await _request(client, "native", [prompt]))
    (item,) = native
    calls = item["parts"]
    assert isinstance(calls, list)
    call = calls[0]["functionCall"]
    call["_compat_name_rewritten"] = True
    gateway.reply(AistudioPart(text="Done."))
    await _request(
        client,
        "native",
        [prompt, *native, *_outputs("native", [("row", "OK")], "unknown")],
    )
    assert gateway.wire[1][0] == [part.to_wire()]
    typed = GeminiFunctionCall.model_validate(call)
    assert not typed._compat_name_rewritten
    typed._compat_name_rewritten = True
    assert typed.model_copy(deep=True)._compat_name_rewritten
    assert "_compat_name_rewritten" not in typed.model_dump()
    assert "_compat_name_rewritten" not in typed.model_json_schema()["properties"]
    rewritten = _part("unknown", "row", 1)
    rewritten._compat_name_rewritten = True
    assert rewritten == part and rewritten.to_wire() == part.to_wire()
