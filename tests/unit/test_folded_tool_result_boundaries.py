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
