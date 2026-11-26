"""Round-trip signature provenance through real HTTP request and response routes."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import TypeVar
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from aistudio_api.api.dependencies import get_client
from aistudio_api.api.routes_compat import router as compat_router
from aistudio_api.api.routes_gemini import router as gemini_router
from aistudio_api.application import api_service_gemini
from aistudio_api.domain import tool_signatures
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.gateway.model_turns import ModelTurnHistory
from aistudio_api.infrastructure.gateway.wire_codec import encode_request_body
from aistudio_api.infrastructure.gateway.wire_parser import (
    parse_response_part,
    parse_text_output,
)
from aistudio_api.infrastructure.gateway.wire_spec import PartIndex, TopLevelIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

MODEL = "gemini-3.8-flash"
SIG_A = base64.b64encode(b"opaque-fixture-state-a").decode()
SIG_B = base64.b64encode(b"opaque-fixture-state-b").decode()
SIG_C = base64.b64encode(b"opaque-fixture-state-c").decode()
SURFACES = ["native", "chat", "claude", "responses"]
_JSON = TypeVar("_JSON")


class FixtureGateway:
    def __init__(self):
        self.history = ModelTurnHistory()
        self.raw = ""
        self.last: list[AistudioContent] = []
        self.wire: list = []

    def reply(self, *parts: AistudioPart) -> None:
        candidate = [[[part.to_wire() for part in parts], "model"], 1]
        chunk = [[candidate]]
        self.raw = json.dumps([[chunk]])

    async def generate_content(
        self, *, model: str, contents: list[AistudioContent], **_kwargs: object
    ) -> ModelOutput:
        self.last, _, context = self.history.prepare(model, contents)
        self.wire = json.loads(encode_request_body(model=model, contents=self.last))[
            TopLevelIndex.CONTENTS
        ]
        response = parse_text_output(self.raw)
        if context is not None:
            capture = context.capture()
            candidate = response.candidates[0]
            capture.add(candidate.raw_parts)
            capture.finish(candidate.finish_reason)
        return response

    def replay_parts(self) -> list[AistudioPart]:
        return [
            part for turn in self.last if turn.role == "model" for part in turn.parts
        ]

    def wire_parts(self, index: int):
        return [parse_response_part(part) for part in self.wire[index][0]]


@pytest.fixture
async def route(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AsyncIterator[tuple[httpx.AsyncClient, FixtureGateway]]:
    gateway = FixtureGateway()
    app = FastAPI()
    app.include_router(compat_router)
    app.include_router(gemini_router)
    app.dependency_overrides[get_client] = lambda: gateway
    monkeypatch.setattr(api_service_gemini, "ensure_active_account", AsyncMock())
    cache = tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json")
    monkeypatch.setattr(tool_signatures, "_cache_instance", cache)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"x-anti-truncation": "false"},
    ) as client:
        yield client, gateway


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mode", ["warm", "cold", "explicit"])
async def test_responses_replay_ignores_identical_summary_from_another_prompt(
    route, mode, reverse
):
    client, gateway = route
    cases = [("Prompt one", SIG_A), ("Prompt two", SIG_B)]
    replay = []
    if reverse:
        cases.reverse()
    for prompt, signature in cases:
        gateway.reply(
            AistudioPart(
                text="Let me consider.", thought=True, thought_signature=signature
            ),
            AistudioPart(text="The answer."),
        )
        response = await client.post(
            "/v1/responses", json={"model": MODEL, "input": prompt}
        )
        assert response.status_code == 200
        if signature == SIG_A:
            replay = response.json()["output"]
    if mode != "explicit":
        for item in replay:
            item.pop("extra_content", None)
    if mode != "warm":
        gateway.history.clear()
    gateway.reply(AistudioPart(text="Next answer."))
    response = await client.post(
        "/v1/responses",
        json={
            "model": MODEL,
            "input": [
                {"role": "user", "content": "Prompt one"},
                *replay,
                {"role": "user", "content": "Continue."},
            ],
        },
    )
    assert response.status_code == 200
    assert [
        (part.text, part.thought, part.thought_signature)
        for part in gateway.replay_parts()
    ] == [
        ("Let me consider.", True, None if mode == "cold" else SIG_A),
        ("The answer.", False, None),
    ]


@pytest.mark.parametrize(
    "reroll,keep_summary,keep_signature",
    [
        (None, True, False),
        ("Thought B", True, False),
        ("Thought A", True, False),
        ("Thought B", False, False),
        ("Thought B", True, True),
    ],
)
async def test_chat_replay_keeps_echoed_state_when_rerolls_share_visible_answer(
    route, reroll, keep_summary, keep_signature
):
    client, gateway = route
    endpoint = "/v1/chat/completions"
    prompt = {"role": "user", "content": "Prompt"}
    gateway.reply(
        AistudioPart(text="Thought A", thought=True, thought_signature=SIG_A),
        AistudioPart(text="The same answer"),
    )
    first = await client.post(endpoint, json={"model": MODEL, "messages": [prompt]})
    assert first.status_code == 200
    echo = first.json()["choices"][0]["message"]
    if reroll is not None:
        gateway.reply(
            AistudioPart(text=reroll, thought=True, thought_signature=SIG_B),
            AistudioPart(text="The same answer"),
        )
        second = await client.post(
            endpoint, json={"model": MODEL, "messages": [prompt]}
        )
        assert second.status_code == 200
    if not keep_signature:
        echo.pop("extra_content", None)
    if not keep_summary:
        echo.pop("reasoning_content", None)
    gateway.reply(AistudioPart(text="Continuing"))
    response = await client.post(
        endpoint,
        json={
            "model": MODEL,
            "messages": [prompt, echo, {"role": "user", "content": "Continue"}],
        },
    )
    assert response.status_code == 200
    replay = gateway.replay_parts()
    expected: list[tuple[str, str | None]] = [("The same answer", None)]
    if keep_summary:
        signature = SIG_A if keep_signature or reroll is None else None
        expected.insert(0, ("Thought A", signature))
    assert [(part.text, part.thought_signature) for part in replay] == expected


@pytest.mark.parametrize("mode", ["warm", "cold", "explicit"])
@pytest.mark.parametrize("surface", ["responses", "native"])
async def test_parallel_original_unsigned_sibling_stays_unsigned(route, mode, surface):
    client, gateway = route
    parts = [
        AistudioPart(
            function_call=("lookup", {"item": "a"}, "call-a"),
            thought_signature=SIG_A,
        ),
        AistudioPart(function_call=("lookup", {"item": "b"}, "call-b")),
    ]
    gateway.reply(*parts)
    native_initial = [{"role": "user", "parts": [{"text": "Find both"}]}]
    if surface == "responses":
        endpoint = "/v1/responses"
        first_request = {"model": MODEL, "input": "Find both"}
    else:
        endpoint = f"/v1beta/models/{MODEL}:generateContent"
        first_request = {"contents": native_initial}
    response = await client.post(endpoint, json=first_request)
    assert response.status_code == 200
    if surface == "responses":
        calls = response.json()["output"]
        if mode != "explicit":
            for item in calls:
                item.pop("extra_content", None)
        request = {
            "model": MODEL,
            "input": [
                {"role": "user", "content": "Find both"},
                *calls,
                *[
                    {"type": "function_call_output", "call_id": call_id, "output": "OK"}
                    for call_id in ("call-a", "call-b")
                ],
            ],
        }
    else:
        calls = response.json()["candidates"][0]["content"]["parts"]
        if mode != "explicit":
            for part in calls:
                part.pop("thoughtSignature", None)
        request = {
            "contents": [
                *native_initial,
                {"role": "model", "parts": calls},
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "lookup",
                                "id": call_id,
                                "response": {"result": "OK"},
                            }
                        }
                        for call_id in ("call-a", "call-b")
                    ],
                },
            ]
        }
    if mode != "warm":
        gateway.history.clear()
    # An old per-call record cannot turn a known unsigned sibling into a
    # signed part, nor override a matching scoped raw capture.
    tool_signatures.get_signature_cache().remember(
        "call-b", "lookup", {"item": "b"}, SIG_B
    )
    gateway.reply(AistudioPart(text="Both found."))
    response = await client.post(endpoint, json=request)
    assert response.status_code == 200
    wires = [part.to_wire() for part in gateway.replay_parts()]
    assert wires == [part.to_wire() for part in parts]
    assert wires[0][PartIndex.THOUGHT_SIGNATURE] == SIG_A
    assert len(wires[1]) <= PartIndex.THOUGHT_SIGNATURE


def _prompt(surface: str, text: str) -> list[dict[str, object]]:
    if surface == "native":
        return [{"role": "user", "parts": [{"text": text}]}]
    return [{"role": "user", "content": text}]


async def _request(
    client: httpx.AsyncClient, surface: str, history: list[dict[str, object]]
) -> list[dict[str, object]]:
    body: dict[str, object]
    if surface == "native":
        path = f"/v1beta/models/{MODEL}:generateContent"
        body = {"contents": history}
    elif surface == "chat":
        path = "/v1/chat/completions"
        body = {"model": MODEL, "messages": history}
    elif surface == "claude":
        path = "/v1/messages"
        body = {"model": MODEL, "max_tokens": 128, "messages": history}
    else:
        path = "/v1/responses"
        body = {"model": MODEL, "input": history}
    response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    if surface == "native":
        return [data["candidates"][0]["content"]]
    if surface == "chat":
        return [data["choices"][0]["message"]]
    if surface == "claude":
        return [{"role": "assistant", "content": data["content"]}]
    return data["output"]


def _outputs(
    surface: str, pairs: list[tuple[str | None, str]], name: str = "lookup"
) -> list[dict[str, object]]:
    if surface == "native":
        return [
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": i,
                            "name": name,
                            "response": {"result": v},
                        }
                    }
                    for i, v in pairs
                ],
            }
        ]
    if surface == "chat":
        return [{"role": "tool", "tool_call_id": i, "content": v} for i, v in pairs]
    if surface == "claude":
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": i, "content": v}
                    for i, v in pairs
                ],
            }
        ]
    return [
        {"type": "function_call_output", "call_id": i, "output": v} for i, v in pairs
    ]


def _strip_signatures(tree: _JSON) -> _JSON:
    if isinstance(tree, list):
        for child in tree:
            _strip_signatures(child)
    elif isinstance(tree, dict):
        tree.pop("extra_content", None)
        tree.pop("thoughtSignature", None)
        for child in tree.values():
            _strip_signatures(child)
    return tree


def _wire_tools(gateway, index, attribute):
    return [
        (tool.get("call_id"), tool["name"], tool["args"], part.thought_signature)
        for part in gateway.wire_parts(index)
        if (tool := getattr(part, attribute)) is not None
    ]


@pytest.mark.parametrize("surface", SURFACES)
async def test_result_ownership_survives_reordered_completions_and_sequential_calls(
    route, surface
):
    client, gateway = route
    prompt = _prompt(surface, "Look up both cities, then compare them.")
    gateway.reply(
        AistudioPart(
            function_call=("lookup", {"city": "Paris"}, "A"), thought_signature=SIG_A
        ),
        AistudioPart(function_call=("lookup", {"city": "Oslo"}, "B")),
    )
    batch = _strip_signatures(await _request(client, surface, prompt))
    reverse = _outputs(surface, [("B", "cold"), ("A", "warm")])
    equivalent = _outputs(surface, [("A", "warm"), ("B", "cold")])
    different = _outputs(surface, [("A", "cold"), ("B", "warm")])

    gateway.reply(
        AistudioPart(
            function_call=("compare", {"unit": "C"}, "C"), thought_signature=SIG_B
        )
    )
    compare = _strip_signatures(
        await _request(client, surface, prompt + batch + reverse)
    )
    assert _wire_tools(gateway, 1, "function_call") == [
        ("A", "lookup", {"city": "Paris"}, SIG_A),
        ("B", "lookup", {"city": "Oslo"}, ""),
    ]
    assert _wire_tools(gateway, 2, "function_response") == [
        ("B", "lookup", {"result": "cold"}, ""),
        ("A", "lookup", {"result": "warm"}, ""),
    ]

    final_output = _outputs(surface, [("C", "done")], "compare")
    gateway.reply(AistudioPart(text="Done."))
    base = prompt + batch
    # Even with only one cached capture, the same sequence of payload values
    # is not evidence that they belonged to the same calls.
    await _request(client, surface, base + different + compare + final_output)
    assert _wire_tools(gateway, 3, "function_call")[0][-1] not in {SIG_B, SIG_C}
    # Reordering completion with ownership unchanged should match that capture.
    await _request(client, surface, base + equivalent + compare + final_output)
    assert _wire_tools(gateway, 3, "function_call")[0][-1] == SIG_B
    assert _wire_tools(gateway, 4, "function_response") == [
        ("C", "compare", {"result": "done"}, ""),
    ]

    # Both branches can now retain different signed state for the same visible
    # sequential call (including an identical ID and arguments).
    gateway.reply(
        AistudioPart(
            function_call=("compare", {"unit": "C"}, "C"), thought_signature=SIG_C
        )
    )
    other_compare = _strip_signatures(await _request(client, surface, base + different))
    gateway.reply(AistudioPart(text="Done."))
    for results, echo, expected in [
        (reverse, compare, SIG_B),
        (different, other_compare, SIG_C),
    ]:
        await _request(client, surface, base + results + echo + final_output)
        assert _wire_tools(gateway, 3, "function_call")[0][-1] == expected


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("reverse", [False, True])
async def test_cold_restart_does_not_borrow_a_reused_call_id(
    route, monkeypatch, tmp_path, surface, reverse
):
    client, gateway = route
    cases = [("First prompt", SIG_A), ("Different prompt", SIG_B)]
    if reverse:
        cases.reverse()
    histories = []
    for prompt, signature in cases:
        gateway.reply(
            AistudioPart(
                function_call=("lookup", {"city": "Paris"}, "A"),
                thought_signature=signature,
            )
        )
        initial = _prompt(surface, prompt)
        echoed = _strip_signatures(await _request(client, surface, initial))
        histories.append(
            (initial + echoed + _outputs(surface, [("A", "done")]), signature)
        )

    # A process restart loses the raw turn LRU, not scoped persisted evidence.
    gateway.history.clear()
    tool_signatures.get_signature_cache().flush()
    monkeypatch.setattr(
        tool_signatures,
        "_cache_instance",
        tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json"),
    )
    gateway.reply(AistudioPart(text="Done."))
    for history, expected in histories:
        await _request(client, surface, history)
        assert _wire_tools(gateway, 1, "function_call") == [
            ("A", "lookup", {"city": "Paris"}, expected),
        ]
    unrelated = _prompt(surface, "Unobserved prompt") + histories[0][0][1:]
    await _request(client, surface, unrelated)
    assert _wire_tools(gateway, 1, "function_call")[0][-1] not in {SIG_A, SIG_B}


@pytest.mark.parametrize("second_signature", [SIG_B, None])
async def test_cold_restart_withholds_conflicting_exact_scoped_occurrences(
    route, monkeypatch, tmp_path, second_signature
):
    client, gateway = route
    initial = _prompt("native", "Look up Paris.")
    for signature in (SIG_A, second_signature):
        gateway.reply(
            AistudioPart(
                function_call=("lookup", {"city": "Paris"}, "A"),
                thought_signature=signature,
            )
        )
        echoed = _strip_signatures(await _request(client, "native", initial))
    gateway.history.clear()
    tool_signatures.get_signature_cache().flush()
    monkeypatch.setattr(
        tool_signatures,
        "_cache_instance",
        tool_signatures.ThoughtSignatureCache(tmp_path / "signatures.json"),
    )
    gateway.reply(AistudioPart(text="Done."))
    await _request(
        client, "native", initial + echoed + _outputs("native", [("A", "done")])
    )
    assert _wire_tools(gateway, 1, "function_call") == [
        ("A", "lookup", {"city": "Paris"}, ""),
    ]


@pytest.mark.parametrize("supplied", [None, "A"])
async def test_native_mixed_results_reserve_id_and_leave_idless_order_untouched(
    route, supplied
):
    client, gateway = route
    initial = _prompt("native", "Look up both cities.")
    gateway.reply(
        AistudioPart(
            function_call=("lookup", {"city": "Paris"}, "A"), thought_signature=SIG_A
        ),
        AistudioPart(function_call=("lookup", {"city": "Oslo"}, "B")),
    )
    model = await _request(client, "native", initial)
    outputs = _outputs("native", [(None, "first"), (supplied, "second")])
    result_parts = outputs[0]["parts"]
    assert isinstance(result_parts, list)
    for part, attachment in zip(result_parts, ("YQ==", "Yg=="), strict=True):
        assert isinstance(part, dict)
        response = part["functionResponse"]
        assert isinstance(response, dict)
        response["parts"] = [
            {"inlineData": {"mimeType": "image/png", "data": attachment}}
        ]
    # Also exercise two consecutive user Contents in one return batch.
    split: list[dict[str, object]] = [
        {"role": "user", "parts": [p]} for p in result_parts
    ]
    gateway.reply(AistudioPart(text="Done."))
    await _request(client, "native", initial + model + split)
    assert _wire_tools(gateway, 2, "function_response") == [
        ("B" if supplied else None, "lookup", {"result": "first"}, ""),
        (supplied, "lookup", {"result": "second"}, ""),
    ]
    encoded = gateway.wire_parts(2)
    assert [p.inline_data for p in encoded] == [
        None,
        ("image/png", "YQ=="),
        None,
        ("image/png", "Yg=="),
    ]


@pytest.mark.parametrize("variant", ["swap", "visible_boundary", "thought_boundary"])
async def test_native_warm_capture_never_replaces_explicit_signed_owners(
    route, variant
):
    client, gateway = route
    initial = _prompt("native", "Think and answer.")
    if variant == "swap":
        original = [
            AistudioPart(text="Thought", thought=True, thought_signature=SIG_A),
            AistudioPart(text="Answer", thought_signature=SIG_B),
        ]
        explicit = deepcopy(original)
        explicit[0].thought_signature, explicit[1].thought_signature = SIG_B, SIG_A
    else:
        thought = variant == "thought_boundary"
        original = [
            AistudioPart(text="Early ", thought=thought),
            AistudioPart(text="phrase", thought=thought, thought_signature=SIG_A),
        ]
        explicit = [
            AistudioPart(text="Early phrase", thought=thought, thought_signature=SIG_A)
        ]
    # Get the explicit model history first, from a different worker's capture.
    gateway.reply(*explicit)
    echoed = await _request(client, "native", initial)
    gateway.history.clear()
    gateway.reply(*original)
    await _request(client, "native", initial)
    gateway.reply(AistudioPart(text="Done."))
    await _request(client, "native", initial + echoed + _prompt("native", "Continue."))
    assert gateway.wire[1][0] == [part.to_wire() for part in explicit]
