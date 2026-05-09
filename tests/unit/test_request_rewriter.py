import json

from aistudio_api.infrastructure.gateway.request_rewriter import AistudioWireCodec, modify_body


def test_modify_body_updates_generation_config_and_prompt():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"!snap",null,null]'
    rewritten = modify_body(
        original,
        model="models/new",
        prompt="new prompt",
        system_instruction="sys",
        max_tokens=256,
        temperature=0.2,
        top_p=0.9,
        top_k=32,
    )

    assert '"models/new"' in rewritten
    assert '"new prompt"' in rewritten
    assert '"sys"' in rewritten
    body = json.loads(rewritten)
    assert body[3][3] == 256
    assert body[3][4] == 0.2
    assert body[3][5] == 0.9
    assert body[3][6] == 32
    assert body[3][16] == [1, None, None, 3]
    assert body[3][17] == 1


def test_wire_codec_decodes_semantic_fields():
    codec = AistudioWireCodec()
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"!snap",[[[null,"sys"]],"user"],[[[]]]]'

    decoded = codec.decode(original)

    assert decoded.model == "models/original"
    assert decoded.snapshot == "!snap"
    assert decoded.contents[0].role == "user"
    assert decoded.contents[0].parts[-1].text == "old"
    assert decoded.system_instruction is not None
    assert decoded.system_instruction.parts[0].text == "sys"
    assert decoded.generation_config.max_tokens == 128


def test_modify_body_sanitizes_plain_text_generation_config():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16,"application/json",[6],null,null,null,null,null,null,null,[1,null,null,3]],"!snap",null,null]'
    rewritten = modify_body(
        original,
        model="models/gemma-4-31b-it",
        prompt="hello",
    )

    assert '"text/plain"' in rewritten
    assert '"application/json"' not in rewritten
    assert '[6]' not in rewritten
    assert json.loads(rewritten)[3][16] == [1, None, None, 3]
    assert json.loads(rewritten)[3][17] == 1


def test_modify_body_enables_thinking_for_any_model():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"!snap",null,null]'
    rewritten = modify_body(
        original,
        model="models/new-text-model",
        prompt="hello",
    )

    body = json.loads(rewritten)
    assert body[3][16] == [1, None, None, 3]
    assert body[3][17] == 1


def test_modify_body_keeps_structured_generation_config_for_gemini_mode():
    original = '["models/original",[[[[null,"old"]],"user"]],null,[null,["6"],null,128,0.5,0.8,16,"application/json",[6],0.1,0.2,true,5,null,[2,1],null,[1,null,null,3],1],"!snap",null,null,null,null,null,1,"cached",null,[[null,null,"Asia/Shanghai"]]]'
    rewritten = modify_body(
        original,
        model="models/gemini-2.5-pro-preview-05-06",
        prompt="hello",
        generation_config_overrides={
            "stop_sequences": ["STOP"],
            "response_mime_type": "application/json",
            "response_schema": [6],
            "presence_penalty": 0.3,
            "frequency_penalty": 0.4,
            "response_logprobs": True,
            "logprobs": 7,
            "media_resolution": [2, 1],
            "thinking_config": [1, None, None, 3],
            "request_flag": 1,
        },
        sanitize_plain_text=False,
    )

    body = json.loads(rewritten)
    assert body[3][1] == ["STOP"]
    assert body[3][7] == "application/json"
    assert body[3][8] == [6]
    assert body[3][9] == 0.3
    assert body[3][10] == 0.4
    assert body[3][11] is True
    assert body[3][12] == 7
    assert body[3][14] == [2, 1]
    assert body[3][16] == [1, None, None, 3]
    assert body[3][17] == 1
