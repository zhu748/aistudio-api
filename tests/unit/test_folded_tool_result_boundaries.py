"""Empty user placeholders must not detach valid results in a parallel batch."""

from __future__ import annotations

import pytest
import test_signed_part_roundtrip as rig

from aistudio_api.api.dependencies import get_client
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

route = rig.route
_SIG = rig.signature("explicit empty holder")
_IDS = ("B", "A")


@pytest.mark.parametrize(
    "middle", ["joined", "split", "no_parts", "empty_text", "signed", "real_text"]
)
async def test_folded_results_only_cross_a_separately_blank_user_content(route, middle):
    route.wire.reply(AistudioPart(text="Next."))
    gateway = route.app.dependency_overrides[get_client]()
    prepare = gateway.prepare_request
    seen: list[AistudioPart] = []

    async def capture(**kwargs):
        seen[:] = [p for c in kwargs["contents"] if c.role == "user" for p in c.parts]
        return await prepare(**kwargs)

    gateway.prepare_request = capture
    req = [
        {"role": "user", "parts": [{"text": "Check both."}]},
        {
            "role": "model",
            "parts": [
                {"functionCall": {"id": key, "name": "lookup", "args": {"city": key}}}
                for key in reversed(_IDS)
            ],
        },
    ]
    results = [
        {
            "functionResponse": {
                "id": key,
                "name": "lookup",
                "response": {"result": key},
            }
        }
        for key in _IDS
    ]
    intervening = {
        "no_parts": [],
        "empty_text": [{"text": ""}],
        "signed": [{"text": "", "thoughtSignature": _SIG}],
        "real_text": [{"text": "A new user instruction."}],
    }
    if middle == "joined":
        req.append({"role": "user", "parts": results})
    else:
        req.append({"role": "user", "parts": results[:1]})
        if middle in intervening:
            req.append({"role": "user", "parts": intervening[middle]})
        req.append({"role": "user", "parts": results[1:]})

    response = await route.raw.post(
        f"/v1beta/models/{rig.MODEL}:generateContent", json={"contents": req}
    )
    assert response.status_code == 200, response.text
    expected_ids = _IDS[:1] if middle in {"real_text", "signed"} else _IDS
    assert [p.function_response for p in seen if p.function_response] == [
        ("lookup", {"result": key}, key) for key in expected_ids
    ]
    if middle == "signed":
        assert [part.thought_signature for part in seen if part.thought_signature] == [
            _SIG
        ]
    if middle == "real_text":
        assert any(p.text == "A new user instruction." for p in seen)


@pytest.mark.parametrize("split", [False, True])
async def test_native_model_side_result_keeps_its_part_and_pending_calls(route, split):
    model_side_signature = rig.signature("server tool result")
    route.wire.reply(
        AistudioPart(function_call=("lookup", {"city": "Paris"}, "client-call")),
        AistudioPart(
            function_response=("server_lookup", {"status": "ok"}, "server-call"),
            thought_signature=model_side_signature,
        ),
    )
    url = f"/v1beta/models/{rig.MODEL}:generateContent"
    prompt = {"role": "user", "parts": [{"text": "Look up Paris."}]}
    first = await route.raw.post(url, json={"contents": [prompt]})
    assert first.status_code == 200
    model = first.json()["candidates"][0]["content"]
    assert model["parts"][1]["thoughtSignature"] == model_side_signature

    gateway = route.app.dependency_overrides[get_client]()
    prepare = gateway.prepare_request
    echoed_results: list[object] = []

    async def capture(**kwargs):
        echoed_results.extend(
            part.function_response
            for content in kwargs["contents"]
            if content.role == "user"
            for part in content.parts
            if part.function_response is not None
        )
        return await prepare(**kwargs)

    gateway.prepare_request = capture
    route.wire.reply(AistudioPart(text="Next."))
    history = (
        [{"role": "model", "parts": [part]} for part in model["parts"]]
        if split
        else [model]
    )
    second = await route.raw.post(
        url,
        json={
            "contents": [
                prompt,
                *history,
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": "client-call",
                                "name": "lookup",
                                "response": {"city": "Paris"},
                            }
                        }
                    ],
                },
            ]
        },
    )
    assert second.status_code == 200
    _, result = route.wire.replayed()
    assert result.function_response is not None
    assert result.function_response["call_id"] == "server-call"
    assert result.function_response["args"] == {"status": "ok"}
    assert result.thought_signature == model_side_signature
    assert echoed_results == [("lookup", {"city": "Paris"}, "client-call")]
