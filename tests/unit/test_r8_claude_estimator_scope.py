"""Only inline media in Claude content-block positions gets a token placeholder."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi import FastAPI

from aistudio_api.api.routes_compat import router

TEXT = ("The ferry leaves the pier at noon. " * 400)[:12_150]
B64 = base64.b64encode(b"Sample media bytes." * 700).decode("ascii")
DATA_URL = "data:image/png;base64," + B64


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def prompt(content, *, role="user", system=None, tools=None):
    return {
        "system": system,
        "tools": tools,
        "messages": [{"role": role, "content": content}],
    }


async def count(client, payload):
    response = await client.post(
        "/v1/messages/count_tokens", json={"model": "test", **payload}
    )
    assert response.status_code == 200, response.text
    return response.json()["input_tokens"]


def ascii_count(payload):
    # These fixtures are ASCII: this is the endpoint's original round-to-nearest formula.
    serialized = json.dumps(payload)
    assert serialized.isascii()
    return int(len(serialized) / 3.5 + 0.5)


def media(kind, source):
    return {"type": kind, "source": source}


def result(block):
    return {"type": "tool_result", "tool_use_id": "t1", "content": [block]}


@pytest.mark.parametrize(
    "key,control,value", [("data", "body", TEXT), ("url", "uri", DATA_URL)]
)
async def test_arbitrary_tool_argument_keys_count_the_same(client, key, control, value):
    def request(name):
        block = {
            "type": "tool_use",
            "id": "t1",
            "name": "inspect",
            "input": {name: value},
        }
        return prompt([block], role="assistant")

    baseline = await count(client, request(control))
    assert baseline == ascii_count(request(control))
    assert await count(client, request(key)) == baseline


@pytest.mark.parametrize("inside_result", [False, True], ids=["message", "tool-result"])
async def test_plain_text_document_counts_its_literal_text(client, inside_result):
    doc = media("document", {"type": "text", "media_type": "text/plain", "data": TEXT})
    payload = prompt([result(doc) if inside_result else doc])
    assert await count(client, payload) == ascii_count(payload)


async def test_media_lookalikes_in_opaque_objects_are_counted_literally(client):
    image = media("image", {"type": "base64", "media_type": "image/png", "data": B64})
    url = media("image", {"type": "url", "url": DATA_URL})
    payload = prompt(
        [
            {"type": "tool_use", "name": "inspect", "id": "t1", "input": result(image)},
            {"type": "text", "text": TEXT, "metadata": url},
            result({"type": "text", "text": TEXT, "metadata": image}),
            result(
                result(url)
            ),  # A tool_result can't itself contain a tool_result block.
        ],
        system=[{"type": "text", "text": TEXT, "metadata": image}],
        tools=[
            {
                "name": "inspect",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "payload": {"type": "object", "default": result(url)}
                    },
                },
            }
        ],
    )
    assert await count(client, payload) == ascii_count(payload)


@pytest.mark.parametrize(
    "kind,source",
    [
        ("image", {"type": "url", "url": "https://example.invalid/" + B64}),
        ("image", {"type": "url", "url": "data:image/png," + B64}),
        ("document", {"type": "url", "url": "data:text/plain;base64," + B64}),
        ("document", {"type": "base64", "media_type": "text/plain", "data": B64}),
        ("image", {"type": "other", "data": B64, "url": DATA_URL}),
    ],
    ids=["remote", "non-base64-url", "text-url", "text-mime", "unknown-source"],
)
async def test_nonmedia_sources_count_literally(client, kind, source):
    payload = prompt([media(kind, source)])
    assert await count(client, payload) == ascii_count(payload)


@pytest.mark.parametrize("inside_result", [False, True], ids=["message", "tool-result"])
@pytest.mark.parametrize(
    "kind,mime", [("image", "image/png"), ("document", "application/pdf")]
)
@pytest.mark.parametrize("key", ["data", "url"], ids=["base64", "data-url"])
async def test_only_the_inline_media_value_is_replaced(
    client, inside_result, kind, mime, key
):
    value = B64 if key == "data" else f"data:{mime};base64,{B64}"

    def request(payload_value):
        # Non-media sibling fields in both the source and block must survive.
        source = (
            {"type": "base64", "media_type": mime, "url": DATA_URL}
            if key == "data"
            else {"type": "url", "data": TEXT}
        )
        block = {
            **media(kind, {**source, key: payload_value}),
            "metadata": {"data": TEXT},
        }
        return prompt([result(block) if inside_result else block])

    expected = ascii_count(request(f"<base64 media: {len(value)} chars>"))
    assert await count(client, request(value)) == expected
