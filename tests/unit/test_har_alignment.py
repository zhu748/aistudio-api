"""2026-11 真机抓包对齐性回归测试。

基线来源（Reqable HAR，235 条 entry / 46 条 MakerSuiteService RPC，Android
Chrome 130 真机）与 SOCKS5 出口代理线上实测；详见
docs/CAPTURE_ALIGNMENT_2026-11.md 与 docs/WIRE_SPECIFICATION.md。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid

from aistudio_api.config import settings
from aistudio_api.infrastructure.account.cookie_parser import (
    MINIMAL_COOKIE_WHITELIST,
    build_makersuite_headers,
    calculate_sapisid_hash,
    filter_minimal_cookies,
    get_visit_id,
)
from aistudio_api.infrastructure.gateway.model_defaults import (
    browser_default_max_tokens,
    browser_default_thinking_wire,
    effective_generation_config_overrides,
    model_output_token_limit,
    model_unknown_flag_13,
    resolve_model_defaults,
    snap_thinking_level_to_supported,
    snap_thinking_wire_to_model,
)
from aistudio_api.infrastructure.gateway.model_discovery import (
    _EXTRA_GUARANTEED_MODELS,
    _FALLBACK_MODELS,
    ModelDiscoveryService,
    model_discovery,
)
from aistudio_api.infrastructure.gateway.wire_codec import encode_request_body
from aistudio_api.infrastructure.gateway.wire_types import ThinkingLevel

# ─────────────────────── 第一轮：请求头 / Cookie / visit-id ───────────────────────


def test_rpc_headers_have_no_x_browser_signature():
    """抓包实证：46 条 clients6 RPC 请求从不携带 X-Browser-* 头。"""
    headers = build_makersuite_headers(tier="Pro")
    xbrowser = [k for k in headers if k.lower().startswith("x-browser")]
    assert xbrowser == []


def test_rpc_headers_include_fetch_metadata():
    """真实浏览器跨站 XHR 必带的 Fetch Metadata 头（2026-11 HAR 35/35 条）。"""
    headers = build_makersuite_headers(tier="Pro")
    assert headers["Accept"] == "*/*"
    assert headers["Sec-Fetch-Site"] == "same-site"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert headers["Accept-Language"]


def test_visit_id_is_session_stable_and_v1_base64_uuid_form():
    """visit-id 会话级复用 + v1_+base64(UUID 字符串) 形态（抓包 7/7 条恒定）。"""
    a1 = get_visit_id("align-acct")
    a2 = get_visit_id("align-acct")
    assert a1 == a2
    assert a1.startswith("v1_")
    decoded = base64.b64decode(a1[3:])
    uuid.UUID(decoded.decode("ascii"))  # 合法 UUID 字符串
    # 36 字符 UUID 字符串 → 48 字符 base64（与抓包形态一致）
    assert len(a1) == 3 + 48
    # 账号作用域隔离：不同账号不共享 visit-id
    b = get_visit_id("align-acct-2")
    assert b != a1


def test_build_makersuite_headers_visit_scope_reuse():
    headers = build_makersuite_headers(
        tier="Pro", include_visit_id=True, visit_scope="align-scope"
    )
    again = build_makersuite_headers(
        tier="Pro", include_visit_id=True, visit_scope="align-scope"
    )
    assert headers["X-AIStudio-Visit-Id"] == again["X-AIStudio-Visit-Id"]


def test_cookie_whitelist_includes_strp_and_search_samesite():
    """2026-11 抓包 16 项 Cookie 全集：__Secure-STRP / SEARCH_SAMESITE 恒上行。"""
    assert "__Secure-STRP" in MINIMAL_COOKIE_WHITELIST
    assert "SEARCH_SAMESITE" in MINIMAL_COOKIE_WHITELIST
    cookies = {
        "SAPISID": "abc",
        "__Secure-STRP": "strp-token",
        "SEARCH_SAMESITE": "CgQIoaIB",
        "AEC": "telemetry-only",
        "NID": "telemetry-only",
    }
    filtered = filter_minimal_cookies(cookies)
    assert filtered["__Secure-STRP"] == "strp-token"
    assert filtered["SEARCH_SAMESITE"] == "CgQIoaIB"
    # 纯遥测项维持导入即丢弃策略
    assert "AEC" not in filtered
    assert "NID" not in filtered


def test_sapisid_hash_format_matches_capture():
    """三段式 SAPISIDHASH，hash = sha1(f"{ts} {sid} {origin}")（抓包反推验证）。"""
    cookies = {"SAPISID": "hqGZyb33DgPFpTbO/AQydfyRktj_L-scWp"}
    header = calculate_sapisid_hash(cookies)
    parts = header.split(" ")
    assert parts[0] == "SAPISIDHASH"
    assert parts[2] == "SAPISID1PHASH"
    assert parts[4] == "SAPISID3PHASH"
    ts, digest = parts[1].split("_", 1)
    expected = hashlib.sha1(
        f"{ts} {cookies['SAPISID']} https://aistudio.google.com".encode()
    ).hexdigest()
    assert digest == expected
    assert abs(int(ts) - int(time.time())) < 5


# ─────────────────────── 第二轮：请求体浏览器形态 ───────────────────────


def test_fallback_catalog_contains_gemini_4_carbon():
    """2026-11-12 线上目录收录 gemini-4-carbon（旗舰，输出 32768）。"""
    carbon = next(m for m in _FALLBACK_MODELS if m["id"] == "gemini-4-carbon")
    assert carbon["outputTokenLimit"] == 32768
    assert carbon["inputTokenLimit"] == 1048576
    assert carbon["defaultThinkingLevel"] == 2
    assert carbon["thinkingLevels"] == [1, 2, 3]
    assert carbon["unknownFlag13"] == 1
    assert carbon["paid"] is True
    assert "generateContent" in carbon["supportedGenerationMethods"]
    extras = {m["id"] for m in _EXTRA_GUARANTEED_MODELS}
    assert "gemini-4-carbon" in extras


def test_fallback_catalog_contains_gemini_4_hydrogen_free_model():
    """2026-11-12 线上目录收录 gemini-4-hydrogen：免费 4 系列模型。

    上游实测槽位：paid[77]=None / accessModes[82]=None（Free 号可用，
    与 3.8-flash 同形态），输出上限 65536（高于 carbon 的 32768），
    思考档 [1,2,3,5] 默认 HIGH(3)，[83]=1。
    """
    hydrogen = next(m for m in _FALLBACK_MODELS if m["id"] == "gemini-4-hydrogen")
    assert hydrogen["outputTokenLimit"] == 65536
    assert hydrogen["inputTokenLimit"] == 1048576
    assert hydrogen["defaultThinkingLevel"] == 3
    assert hydrogen["thinkingLevels"] == [1, 2, 3, 5]
    assert hydrogen["unknownFlag13"] == 1
    assert hydrogen["paid"] is False
    assert "generateContent" in hydrogen["supportedGenerationMethods"]
    extras = {m["id"] for m in _EXTRA_GUARANTEED_MODELS}
    assert "gemini-4-hydrogen" in extras


def test_fallback_catalog_flag13_matches_listmodels_mapping():
    """目录 [83]=1 ↔ gen_config[13]=1 的映射（3.8-flash/argon=1，3.5-lite 无）。"""
    by_id = {m["id"]: m for m in _FALLBACK_MODELS}
    assert by_id["gemini-3.8-flash"]["unknownFlag13"] == 1
    assert by_id["gemini-4-argon"]["unknownFlag13"] == 1
    assert "unknownFlag13" not in by_id["gemini-3.5-flash-lite"]


def test_discovery_parses_unknown_flag_13_from_slot_83():
    service = ModelDiscoveryService()
    item: list[object] = [None] * 84
    item[0] = "models/flagged-model"
    item[2] = "1.0"
    item[3] = "Flagged"
    item[4] = "Flagged"
    item[5] = 1000
    item[6] = 1000
    item[7] = ["generateContent"]
    item[71] = [None, None, None, 0, None, 2, [1, 2, 3]]
    item[77] = 1
    item[82] = [1, 3]
    item[83] = 1  # ← 目录槽位 [83]
    parsed = service._parse_raw_models([[item]])
    assert parsed[0]["unknownFlag13"] == 1
    assert parsed[0]["defaultThinkingLevel"] == 2
    assert parsed[0]["thinkingLevels"] == [1, 2, 3]


def test_encode_request_body_gemini_38_flash_browser_defaults():
    """抓包 [185] 精确形态：恒定默认参数 + flag13=1 + 思考 MEDIUM。"""
    body = json.loads(encode_request_body(model="models/gemini-3.8-flash", prompt="hi"))
    assert body[0] == "models/gemini-3.8-flash"
    # 无工具请求：尾部截断至 snapshot（len=5，与抓包一致）
    assert len(body) == 5
    safety = body[2]
    assert safety == [
        [None, None, 7, 5],
        [None, None, 8, 5],
        [None, None, 9, 5],
        [None, None, 10, 5],
    ]
    gc = body[3]
    assert gc[3] == 65536
    assert gc[4] == 1
    assert gc[5] == 0.95
    assert gc[6] == 64
    assert gc[13] == 1
    assert gc[16] == [1, None, None, 2]
    assert len(gc) == 17


def test_encode_request_body_gemini_35_flash_lite_browser_defaults():
    """抓包 [10]/[150] 形态：flag13 缺席 + 思考默认档跟随元数据（MINIMAL=4）。"""
    body = json.loads(
        encode_request_body(model="models/gemini-3.5-flash-lite", prompt="hi")
    )
    gc = body[3]
    assert gc[3] == 65536
    assert gc[4] == 1
    assert gc[5] == 0.95
    assert gc[6] == 64
    # 3.5-flash-lite 目录默认档位为 4（抓包实测浏览器即用模型默认档）
    assert gc[16] == [1, None, None, 4]
    if len(gc) > 13:
        assert gc[13] is None


def test_encode_request_body_gemini_4_carbon_caps_max_tokens():
    """carbon 输出上限 32768 → 浏览器默认 65536 被钳制。"""
    body = json.loads(encode_request_body(model="models/gemini-4-carbon", prompt="hi"))
    gc = body[3]
    assert gc[3] == 32768
    assert gc[13] == 1
    assert gc[16] == [1, None, None, 2]


def test_encode_request_body_user_params_take_priority():
    """用户显式参数覆盖浏览器默认值（temperature/max_tokens/thinking）。"""
    body = json.loads(
        encode_request_body(
            model="models/gemini-3.8-flash",
            prompt="hi",
            temperature=0.3,
            max_tokens=128,
            generation_config_overrides={"thinking_config": [1, None, None, 1]},
        )
    )
    gc = body[3]
    assert gc[3] == 128
    assert gc[4] == 0.3
    assert gc[5] == 0.95  # 未显式指定 → 浏览器默认
    assert gc[16] == [1, None, None, 1]


def test_encode_request_body_image_model_exempts_browser_defaults():
    """图像模型豁免：文本默认值被清除，generation_config 回退空数组。"""
    body = json.loads(
        encode_request_body(model="models/gemini-3.1-flash-image-preview", prompt="hi")
    )
    assert body[2] is None  # image profile 关闭安全设置
    assert body[3] == []


def test_encode_request_body_tts_model_exempts_browser_defaults():
    """TTS 模型豁免（输出上限远低于 65536，避免未知行为）。"""
    body = json.loads(
        encode_request_body(model="models/gemini-3.8-flash-tts", prompt="hi")
    )
    assert body[3] == []


def test_encode_request_body_gemma_keeps_conservative_defaults():
    """gemma 保守策略：不注入浏览器文本默认值。"""
    body = json.loads(encode_request_body(model="models/gemma-4-31b-it", prompt="hi"))
    assert body[3] == []


def test_encode_request_body_drops_thinking_for_models_without_capability():
    """无思考能力模型：默认档位丢弃（避免上游 400）。"""
    body = json.loads(
        encode_request_body(model="models/gemini-3.8-flash-tts", prompt="hi")
    )
    assert body[3] == []  # TTS 无思考能力 → 清除后全空


def test_encode_request_body_request_context_single_element_with_tools():
    """抓包实证：带工具时 request_context 恒为单元素 [[null,null,时区]]。"""
    tools = [[None, None, None, [None, [[]]]]]  # google_search 模板
    body = json.loads(
        encode_request_body(model="models/gemini-3.8-flash", prompt="hi", tools=tools)
    )
    assert len(body) == 14
    assert body[6] == tools  # tools 透传不重新包裹
    rc = body[13]
    assert isinstance(rc, list)
    assert len(rc) == 1
    assert rc[0][0] is None
    assert rc[0][1] is None
    assert rc[0][2] == settings.timezone


def test_encode_request_body_trims_tail_without_tools():
    """无工具请求：尾部裁剪，请求体止于 snapshot（len=5）。"""
    body = json.loads(encode_request_body(model="models/gemini-3.8-flash", prompt="hi"))
    assert len(body) == 5


def test_encode_request_body_unknown_flag_13_slot_passthrough():
    """显式 overrides 指定 flag13 时透传保真。"""
    body = json.loads(
        encode_request_body(
            model="models/gemini-3.5-flash-lite",
            prompt="hi",
            generation_config_overrides={"unknown_flag_13": 1},
        )
    )
    assert body[3][13] == 1


# ─────────────────────── 元数据 helper 单元 ───────────────────────


def test_browser_default_thinking_wire_follows_metadata():
    wire = browser_default_thinking_wire("gemini-3.5-flash-lite")
    assert wire == [1, None, None, 4]
    wire38 = browser_default_thinking_wire("gemini-3.8-flash")
    assert wire38 == [1, None, None, 2]
    unknown = browser_default_thinking_wire("totally-unknown-model")
    assert unknown == [1, None, None, 2]


def test_snap_thinking_level_snaps_to_supported():
    # 3.5-lite 支持 [4,1,2,3]，传入任意档位原样或就近吸附
    assert (
        snap_thinking_level_to_supported("gemini-3.5-flash-lite", ThinkingLevel.MEDIUM)
        == ThinkingLevel.MEDIUM
    )
    # 无思考能力 → None
    assert (
        snap_thinking_level_to_supported("gemini-3.8-flash-tts", ThinkingLevel.MEDIUM)
        is None
    )


def test_snap_thinking_wire_drops_budget_only_for_non_thinking_models():
    # 无思考能力模型（TTS）→ 仅预算配置被丢弃
    assert snap_thinking_wire_to_model("gemini-3.8-flash-tts", [1, 512]) is None
    # 未知模型 → 信任调用方
    assert snap_thinking_wire_to_model("totally-unknown-model", [1, 512]) == [1, 512]


def test_model_metadata_helpers():
    assert model_unknown_flag_13("gemini-3.8-flash") == 1
    assert model_unknown_flag_13("gemini-3.5-flash-lite") is None
    assert model_unknown_flag_13("gemini-4-hydrogen") == 1
    assert model_output_token_limit("gemini-4-carbon") == 32768
    assert browser_default_max_tokens("gemini-4-carbon") == 32768
    assert browser_default_max_tokens("gemini-3.8-flash") == 65536
    # hydrogen 输出上限 65536：默认值不被钳制（高于碳的 32768）
    assert model_output_token_limit("gemini-4-hydrogen") == 65536
    assert browser_default_max_tokens("gemini-4-hydrogen") == 65536


def test_effective_generation_config_overrides_applies_clear_semantics():
    defaults = resolve_model_defaults("models/gemini-3.1-flash-image-preview")
    assert defaults.clear_generation_config_indexes
    overrides = effective_generation_config_overrides(defaults)
    # 清除槽位对应的键被置 None
    for key in ("max_tokens", "temperature", "top_p", "top_k", "thinking_config"):
        if key in overrides:
            assert overrides[key] is None


def test_model_discovery_singleton_fallback_lookup_matches_catalog():
    info = model_discovery.get_cached_model_info("gemini-4-carbon")
    assert info is not None
    assert info.get("unknownFlag13") == 1


# ─────────────────────── 归一化路径（normalize）───────────────────────


def test_normalize_passes_user_values_only():
    """归一化只透传用户显式参数；profile 默认由 wire 构造层统一注入。"""
    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.application.chat_service import normalize_gemini_request

    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            "generationConfig": {"temperature": 0.7},
        }
    )
    norm = normalize_gemini_request(req, "models/gemini-3.8-flash")
    assert norm.generation_config_overrides == {"temperature": 0.7}


def test_normalize_explicit_thinking_none_suppresses_default():
    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.application.chat_service import normalize_gemini_request

    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "none"}},
        }
    )
    norm = normalize_gemini_request(req, "models/gemini-3.8-flash")
    assert norm.generation_config_overrides == {"thinking_config": None}


def test_normalize_empty_thinking_config_leaves_default_active():
    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.application.chat_service import normalize_gemini_request

    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            "generationConfig": {"thinkingConfig": {}},
        }
    )
    norm = normalize_gemini_request(req, "models/gemini-3.8-flash")
    assert norm.generation_config_overrides is None


# ─────────────────────── visit-id 传输层注入 ───────────────────────


def test_visit_id_regex_shape():
    vid = get_visit_id("regex-check")
    assert re.fullmatch(r"v1_[A-Za-z0-9+/=]+", vid)


# ───────────────── 思考档位上下兼容（VERY_HIGH 新档） ─────────────────
# 2026-11 上游 ListModels 目录实测：gemini-4-hydrogen thinkingLevels=[1,2,3,5]
# （新档 5=VERY_HIGH），carbon/argon 仍 [1,2,3]。档位数据随模型列表同一次
# 拉取缓存（600s TTL 自动刷新），fallback 快照含同步实测值。


def test_thinking_level_enum_has_very_high():
    assert int(ThinkingLevel.VERY_HIGH) == 5
    # wire 层数值档位直取：VERY_HIGH(5) 可被解析（此前 ValueError 保持原样）
    assert ThinkingLevel(5) is ThinkingLevel.VERY_HIGH


def test_snap_very_high_downgrades_to_model_max():
    """传入档位高于模型支持的最高档 → 降至模型最高档（向下兼容）。"""
    # carbon 最高支持 HIGH(3)：请求 VERY_HIGH(5) → 吸附降档到 HIGH
    assert (
        snap_thinking_level_to_supported("gemini-4-carbon", ThinkingLevel.VERY_HIGH)
        == ThinkingLevel.HIGH
    )
    # argon 同为 [1,2,3]
    assert (
        snap_thinking_level_to_supported("gemini-4-argon", ThinkingLevel.VERY_HIGH)
        == ThinkingLevel.HIGH
    )
    # hydrogen 支持 [1,2,3,5]：VERY_HIGH 原样通过
    assert (
        snap_thinking_level_to_supported("gemini-4-hydrogen", ThinkingLevel.VERY_HIGH)
        == ThinkingLevel.VERY_HIGH
    )


def test_snap_below_model_min_upgrades_to_min():
    """传入档位低于模型支持的最低档 → 升至模型最低档（向上兼容）。"""
    # carbon 最低支持 LOW(1)：请求 MINIMAL(4，强度最低) → 就近升到 LOW
    # （距离相同优先升档策略保证不意外削弱）
    assert (
        snap_thinking_level_to_supported("gemini-4-carbon", ThinkingLevel.MINIMAL)
        == ThinkingLevel.LOW
    )


def test_snap_wire_with_very_high_level():
    """wire 数组档位吸附：level=5 对 hydrogen 原样、对 carbon 降到 3。"""
    # hydrogen：[1,None,None,5] → 原样（5 在支持列表）
    assert snap_thinking_wire_to_model("gemini-4-hydrogen", [1, None, None, 5]) == [
        1,
        None,
        None,
        5,
    ]
    # carbon：[1,None,None,5] → [1,None,None,3]
    assert snap_thinking_wire_to_model("gemini-4-carbon", [1, None, None, 5]) == [
        1,
        None,
        None,
        3,
    ]


def test_parse_thinking_level_very_high_variants():
    """reasoningEffort 文本 / 数值 / 分隔符变体统一解析为 VERY_HIGH。"""
    from aistudio_api.application.chat_service import _parse_thinking_level

    assert _parse_thinking_level("very_high") is ThinkingLevel.VERY_HIGH
    assert _parse_thinking_level("VERY-HIGH") is ThinkingLevel.VERY_HIGH
    assert _parse_thinking_level("VERYHIGH") is ThinkingLevel.VERY_HIGH
    assert _parse_thinking_level(5) is ThinkingLevel.VERY_HIGH
    # 既有档位不受影响
    assert _parse_thinking_level("high") is ThinkingLevel.HIGH
    assert _parse_thinking_level("none") is None


def test_hydrogen_fallback_snapshot_has_very_high_level():
    """fallback 快照含 hydrogen 实测 [1,2,3,5]（冷启动吸附与线上一致）。"""
    info = model_discovery.get_cached_model_info("gemini-4-hydrogen")
    assert info is not None
    assert info.get("thinkingLevels") == [1, 2, 3, 5]
    assert info.get("defaultThinkingLevel") == 3
    carbon = model_discovery.get_cached_model_info("gemini-4-carbon")
    assert carbon is not None
    assert carbon.get("thinkingLevels") == [1, 2, 3]


def test_browser_default_thinking_wire_hydrogen():
    """hydrogen 浏览器形态默认档取目录元数据 defaultThinkingLevel=3(HIGH)。"""
    assert browser_default_thinking_wire("gemini-4-hydrogen") == [1, None, None, 3]
