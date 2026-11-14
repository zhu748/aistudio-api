"""The latest tool argument snapshot wins across object/string representations."""

import json

import pytest
from test_protocol_optimization import calls_from, convert, frame


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
@pytest.mark.parametrize(
    "updates",
    [
        [{"city": "old"}, '{"city":"Paris"}'],
        ['{"city":"old"}', '{"city":"Paris"}'],
        [{"city": "old"}, '{"city":"Par', 'is"}'],
    ],
)
async def test_later_json_snapshot_replaces_earlier_object_arguments(protocol, updates):
    frames = [
        frame([{"functionCall": {"id": "stable", "name": "weather", "args": args}}])
        for args in updates
    ]
    frames.append(frame([], "STOP"))
    calls = calls_from(protocol, await convert(protocol, frames))
    assert len(calls) == 1
    if protocol == "chat":
        arguments = json.loads(calls[0]["function"]["arguments"])
    elif protocol == "responses":
        arguments = json.loads(calls[0]["arguments"])
    else:
        # Claude carries arguments in JSON deltas following the tool_use block.
        events = await convert(protocol, frames)
        arguments = json.loads(
            "".join(e.get("delta", {}).get("partial_json", "") for e in events)
        )
    assert arguments == {"city": "Paris"}


@pytest.mark.parametrize("protocol", ["chat", "claude", "responses"])
async def test_new_incomplete_snapshot_cannot_execute_stale_arguments(protocol):
    events = await convert(
        protocol,
        [
            frame(
                [
                    {
                        "functionCall": {
                            "id": "stable",
                            "name": "weather",
                            "args": {"city": "old"},
                        }
                    }
                ]
            ),
            frame(
                [
                    {
                        "functionCall": {
                            "id": "stable",
                            "name": "weather",
                            "args": '{"city":"partial',
                        }
                    }
                ],
                "STOP",
            ),
        ],
    )
    assert not calls_from(protocol, events)
    assert any(
        "error" in event or event.get("type") == "response.failed" for event in events
    )


@pytest.mark.parametrize("empty", ["", "  "])
async def test_empty_updates_do_not_erase_the_last_complete_argument_object(empty):
    events = await convert(
        "chat",
        [
            frame(
                [
                    {
                        "functionCall": {
                            "id": "c",
                            "name": "weather",
                            "args": {"city": "Paris"},
                        }
                    }
                ]
            ),
            frame(
                [{"functionCall": {"id": "c", "name": "weather", "args": empty}}],
                "STOP",
            ),
        ],
    )
    calls = calls_from("chat", events)
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Paris"}
