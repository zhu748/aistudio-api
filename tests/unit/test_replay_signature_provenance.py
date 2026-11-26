"""Round-trip signature provenance through real HTTP request and response routes."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
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
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.gateway.wire_spec import PartIndex
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

MODEL = "gemini-3.8-flash"
SIG_A = base64.b64encode(b"opaque-fixture-state-a").decode()
SIG_B = base64.b64encode(b"opaque-fixture-state-b").decode()


class FixtureGateway:
    def __init__(self):
        self.history = ModelTurnHistory()
        self.raw = ""
        self.last: list[AistudioContent] = []

    def reply(self, *parts: AistudioPart) -> None:
        candidate = [[[part.to_wire() for part in parts], "model"], 1]
        chunk = [[candidate]]
        self.raw = json.dumps([[chunk]])

    async def generate_content(
        self, *, model: str, contents: list[AistudioContent], **_kwargs: object
    ) -> ModelOutput:
        self.last, _, context = self.history.prepare(model, contents)
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
