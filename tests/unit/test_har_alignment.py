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

import pytest

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
    model_unknown_flag_45,
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
    assert hydrogen["thinkingLevels"] == [1, 2, 3, 6]
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


def test_encode_request_body_gemini_4_hydrogen_xhigh_and_full_output():
    """2026-11-15 真机故障回归：hydrogen Extra High 档此前发 5 → 上游 400
    "Thinking level EXTRA_HIGH is not supported"；抓包双样本证实 6 → 200。
    现 xhigh 直取 6 档，遗留 5 档 wire 吸附为 6；输出默认取目录上限 65536
    （不再被浏览器滑块起始档压半）。"""
    body = json.loads(
        encode_request_body(
            model="models/gemini-4-hydrogen",
            prompt="hi",
            generation_config_overrides={"thinking_config": [1, None, None, 6]},
        )
    )
    gc = body[3]
    assert gc[3] == 65536
    assert gc[13] == 1
    assert gc[16] == [1, None, None, 6]
    # 遗留 5 档 wire（旧目录缓存时代的产物）按最新目录吸附为 6
    body_legacy = json.loads(
        encode_request_body(
            model="models/gemini-4-hydrogen",
            prompt="hi",
            generation_config_overrides={"thinking_config": [1, None, None, 5]},
        )
    )
    assert body_legacy[3][16] == [1, None, None, 6]


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
    """抓包实证：google_search 族工具时 request_context 恒为单元素 [[null,null,时区]]。"""
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


def test_encode_request_body_no_request_context_with_custom_fc_tools():
    """2026-11-15 getWeather 往返抓包（gc12/gc32）实证：纯自定义函数声明
    不携带 request_context(槽13时区)——该槽仅随 google_search 族出现；
    尾部裁剪到 tools 槽(6)，请求体形态 len=7 与真机一致。"""
    tools = [
        [
            None,
            [
                [
                    "getWeather",
                    "获取指定城市的实时天气情况",
                    [
                        6,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        [["city", [1, None, "城市名称，例如北京、上海"]]],
                        ["city"],
                    ],
                ]
            ],
        ]
    ]
    body = json.loads(
        encode_request_body(model="models/gemini-3.8-flash", prompt="hi", tools=tools)
    )
    assert len(body) == 7  # gc32 真机形态：止于 tools 槽
    assert body[6] == tools
    assert not (len(body) > 13 and body[13] is not None)


def test_encode_request_body_no_request_context_with_maps_or_url_context():
    """maps / url_context / code_execution 容器槽位不在 t[3]，同样不带时区槽。"""
    from aistudio_api.infrastructure.gateway.wire_codec import (
        TOOLS_TEMPLATES,
        _has_search_grounding_tools,
    )

    assert _has_search_grounding_tools([TOOLS_TEMPLATES["google_search"]])
    assert _has_search_grounding_tools(
        [[None, None, None, [None, [[], []]]]]
    )  # search+image
    assert not _has_search_grounding_tools([TOOLS_TEMPLATES["google_maps"]])
    assert not _has_search_grounding_tools([TOOLS_TEMPLATES["url_context"]])
    assert not _has_search_grounding_tools([TOOLS_TEMPLATES["code_execution"]])
    assert not _has_search_grounding_tools(None)


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
    # 2026-11-14 hybrid 判别：目录 [85] 非空的 gemini-4 系才携 [45]=1，
    # 3.8-flash（新代但 [85]=null）不带 —— 与 flag_13 正交。
    assert model_unknown_flag_45("gemini-4-hydrogen") == 1
    assert model_unknown_flag_45("gemini-4-carbon") == 1
    assert model_unknown_flag_45("gemini-4-argon") == 1
    assert model_unknown_flag_45("gemini-3.8-flash") is None
    assert model_unknown_flag_45("gemini-3.5-flash-lite") is None
    assert model_output_token_limit("gemini-4-carbon") == 32768
    assert browser_default_max_tokens("gemini-4-carbon") == 32768
    assert browser_default_max_tokens("gemini-3.8-flash") == 65536
    # 2026-11-15 起默认值取目录输出上限与 65536 的较小者，不再复刻浏览器
    # 滑块 32768 起始档（反截断长输出需要目录上限；客户端显式传值仍优先）。
    # carbon/argon 目录上限 32768 → 钳制结果不变；hydrogen 上限 65536 → 65536。
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


# ───────────────── 思考档位上下兼容（目录最高档 6=EXTRA_HIGH） ─────────────────
# 2026-11-15 Reqable 真机抓包：gemini-4-hydrogen UI「Extra High」档发送
# [1,null,null,6] → 200（双样本），同日 5 档 → 400 "EXTRA_HIGH is not
# supported"——目录最高档已由 5 上移至 6。carbon/argon 仍 [1,2,3]。
# 档位数据随模型列表同一次拉取缓存（600s TTL 自动刷新），fallback 快照
# 含同步实测值。


def test_thinking_level_enum_has_extra_high():
    assert int(ThinkingLevel.VERY_HIGH) == 5
    assert int(ThinkingLevel.EXTRA_HIGH) == 6
    # wire 层数值档位直取：5/6 均可解析
    assert ThinkingLevel(5) is ThinkingLevel.VERY_HIGH
    assert ThinkingLevel(6) is ThinkingLevel.EXTRA_HIGH


def test_snap_very_high_downgrades_to_model_max():
    """传入档位高于模型支持的最高档 → 吸附到最近支持档（同距优先升档）。"""
    # carbon 最高支持 HIGH(3)：请求 VERY_HIGH(5)/EXTRA_HIGH(6) → HIGH
    assert (
        snap_thinking_level_to_supported("gemini-4-carbon", ThinkingLevel.VERY_HIGH)
        == ThinkingLevel.HIGH
    )
    assert (
        snap_thinking_level_to_supported("gemini-4-carbon", ThinkingLevel.EXTRA_HIGH)
        == ThinkingLevel.HIGH
    )
    # argon 同为 [1,2,3]
    assert (
        snap_thinking_level_to_supported("gemini-4-argon", ThinkingLevel.VERY_HIGH)
        == ThinkingLevel.HIGH
    )
    # hydrogen 支持 [1,2,3,6]：EXTRA_HIGH 原样通过；遗留 VERY_HIGH(5) 依
    # 「同距优先升档」吸附到 6（不再落到已被上游移除的 5）
    assert (
        snap_thinking_level_to_supported("gemini-4-hydrogen", ThinkingLevel.EXTRA_HIGH)
        is ThinkingLevel.EXTRA_HIGH
    )
    assert (
        snap_thinking_level_to_supported("gemini-4-hydrogen", ThinkingLevel.VERY_HIGH)
        is ThinkingLevel.EXTRA_HIGH
    )


def test_snap_below_model_min_upgrades_to_min():
    """传入档位低于模型支持的最低档 → 升至模型最低档（向上兼容）。"""
    # carbon 最低支持 LOW(1)：请求 MINIMAL(4，强度最低) → 就近升到 LOW
    # （距离相同优先升档策略保证不意外削弱）
    assert (
        snap_thinking_level_to_supported("gemini-4-carbon", ThinkingLevel.MINIMAL)
        == ThinkingLevel.LOW
    )


def test_snap_wire_with_extra_high_level():
    """wire 数组档位吸附：6 对 hydrogen 原样；遗留 5 对 hydrogen 升到 6、
    对 carbon 降到 3。"""
    # hydrogen：[1,None,None,6] → 原样（6 在支持列表）
    assert snap_thinking_wire_to_model("gemini-4-hydrogen", [1, None, None, 6]) == [
        1,
        None,
        None,
        6,
    ]
    # hydrogen：遗留 [1,None,None,5] → 吸附 [1,None,None,6]
    assert snap_thinking_wire_to_model("gemini-4-hydrogen", [1, None, None, 5]) == [
        1,
        None,
        None,
        6,
    ]
    # carbon：[1,None,None,5] → [1,None,None,3]
    assert snap_thinking_wire_to_model("gemini-4-carbon", [1, None, None, 5]) == [
        1,
        None,
        None,
        3,
    ]


def test_parse_thinking_level_extra_high_variants():
    """reasoningEffort 文本标签统一指向目录最高档 EXTRA_HIGH(6)；数值直传保留。"""
    from aistudio_api.application.chat_service import _parse_thinking_level

    assert _parse_thinking_level("very_high") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("VERY-HIGH") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("VERYHIGH") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("xhigh") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("X-HIGH") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("max") is ThinkingLevel.EXTRA_HIGH
    # 数值直传保留原始档位：5/6 分别对应两个枚举成员
    assert _parse_thinking_level(5) is ThinkingLevel.VERY_HIGH
    assert _parse_thinking_level(6) is ThinkingLevel.EXTRA_HIGH
    # 既有档位不受影响
    assert _parse_thinking_level("high") is ThinkingLevel.HIGH
    assert _parse_thinking_level("none") is None


def test_hydrogen_fallback_snapshot_has_extra_high_level():
    """fallback 快照含 hydrogen 实测 [1,2,3,6]（冷启动吸附与线上一致）。"""
    info = model_discovery.get_cached_model_info("gemini-4-hydrogen")
    assert info is not None
    assert info.get("thinkingLevels") == [1, 2, 3, 6]
    assert info.get("defaultThinkingLevel") == 3
    carbon = model_discovery.get_cached_model_info("gemini-4-carbon")
    assert carbon is not None
    assert carbon.get("thinkingLevels") == [1, 2, 3]


def test_browser_default_thinking_wire_hydrogen():
    """hydrogen 浏览器形态默认档取目录元数据 defaultThinkingLevel=3(HIGH)。"""
    assert browser_default_thinking_wire("gemini-4-hydrogen") == [1, None, None, 3]


def test_parse_thinking_level_xhigh_variants():
    """xhigh（较新 openai 客户端 effort 词汇）及分隔符变体统一指向目录最高档
    EXTRA_HIGH(6)——2026-11-15 抓包：UI Extra High 档发送 6（5 已被上游移除）。"""
    from aistudio_api.application.chat_service import _parse_thinking_level

    assert _parse_thinking_level("xhigh") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("XHIGH") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("X-HIGH") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("x_high") is ThinkingLevel.EXTRA_HIGH
    # 数值直传保留原始档位
    assert _parse_thinking_level(5) is ThinkingLevel.VERY_HIGH
    assert _parse_thinking_level(6) is ThinkingLevel.EXTRA_HIGH
    # 既有档位不受影响（very_high 文本标签同指向目录最高档 6）
    assert _parse_thinking_level("very_high") is ThinkingLevel.EXTRA_HIGH
    assert _parse_thinking_level("high") is ThinkingLevel.HIGH


def test_claude_budget_tier_maps_to_very_high():
    """Claude thinking.budget_tokens 分档：超大预算（>32768）冲 VERY_HIGH(5) 档。

    档位随模型目录自动吸附：模型不支持 5 档（carbon [1,2,3]）时降回 HIGH，
    hydrogen [1,2,3,5] 原样生效——Claude 客户端由此可触达最高思考档。
    """
    from aistudio_api.application.compat_service import _claude_thinking_to_level

    assert (
        _claude_thinking_to_level({"type": "enabled", "budget_tokens": 64000})
        == "VERY_HIGH"
    )
    assert (
        _claude_thinking_to_level({"type": "enabled", "budget_tokens": 40960})
        == "VERY_HIGH"
    )
    # 分档边界：32768 仍是 HIGH（升档阈值 exclusive）
    assert (
        _claude_thinking_to_level({"type": "enabled", "budget_tokens": 32768}) == "HIGH"
    )
    assert (
        _claude_thinking_to_level({"type": "enabled", "budget_tokens": 16000}) == "HIGH"
    )
    assert (
        _claude_thinking_to_level({"type": "enabled", "budget_tokens": 8192})
        == "MEDIUM"
    )
    assert (
        _claude_thinking_to_level({"type": "enabled", "budget_tokens": 2048}) == "LOW"
    )
    # 仅 enabled 未给预算 → 默认 HIGH；显式关闭 → None
    assert _claude_thinking_to_level({"type": "enabled"}) == "HIGH"
    assert _claude_thinking_to_level({"type": "disabled"}) is None
    assert _claude_thinking_to_level(None) is None


def test_xhigh_snap_chain_per_model_catalog():
    """xhigh → EXTRA_HIGH(6) 后按模型目录吸附：hydrogen [1,2,3,6] 原样、
    carbon 降 HIGH；遗留 VERY_HIGH(5) 对 hydrogen 依同距升档吸附到 6。"""
    level = ThinkingLevel.EXTRA_HIGH
    assert snap_thinking_level_to_supported("gemini-4-hydrogen", level) is level
    assert (
        snap_thinking_level_to_supported("gemini-4-carbon", level) is ThinkingLevel.HIGH
    )
    assert (
        snap_thinking_level_to_supported("gemini-4-hydrogen", ThinkingLevel.VERY_HIGH)
        is ThinkingLevel.EXTRA_HIGH
    )


# ─────────────────── 第三轮：工具调用全链路黄金样本 ───────────────────
# 2026-11-15 真机抓包（Reqable HAR，Android Chrome 130，51 条
# MakerSuiteService RPC）：getWeather 函数调用实验，用户声明 → FC(带
# thought_signature) → FR(map 编码) → 二轮续写，四个环节 wire 逐字节采录。


def test_har_golden_function_declaration_wire():
    """抓包 req_12 槽 6 工具声明：我们的编码与其逐字节一致。"""
    from aistudio_api.application.chat_service import (
        encode_function_declaration_to_wire,
    )

    golden = [
        "getWeather",
        "获取指定城市的实时天气情况",
        [
            6,
            None,
            None,
            None,
            None,
            None,
            [["city", [1, None, "城市名称，例如北京、上海"]]],
            ["city"],
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            ["city"],
        ],
    ]
    wire = encode_function_declaration_to_wire(
        {
            "name": "getWeather",
            "description": "获取指定城市的实时天气情况",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，例如北京、上海",
                    }
                },
                "required": ["city"],
            },
        }
    )
    assert wire == golden
    # propertyOrdering 未显式声明时按键序推导（抓包：浏览器恒携带）


def test_har_golden_function_call_part_with_signature():
    """抓包 FC part：槽 10 三元组 + 槽 14 签名，11-13 恒空。"""
    from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

    sig = "Eo0CCooCAWkUfRO0lhH0949y"
    part = AistudioPart(
        function_call=("getWeather", {"city": "上海"}, "call_12531810613605977258"),
        thought_signature=sig,
    )
    wire = part.to_wire()
    assert wire[10] == [
        "getWeather",
        [[["city", [None, None, "上海"]]]],
        "call_12531810613605977258",
    ]
    assert wire[14] == sig
    assert wire[11] is None and wire[12] is None and wire[13] is None


def test_har_golden_function_response_part_map_encoding():
    """抓包 FR part：槽 11 三元组，response 为 [[key,[null,null,val]]] map 编码。"""
    from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

    part = AistudioPart(
        function_response=(
            "getWeather",
            {"weather": "中雨", "temperature": "18°C", "humidity": "85%"},
            "call_12531810613605977258",
        )
    )
    wire = part.to_wire()
    assert wire[11] == [
        "getWeather",
        [
            [
                ["weather", [None, None, "中雨"]],
                ["temperature", [None, None, "18°C"]],
                ["humidity", [None, None, "85%"]],
            ]
        ],
        "call_12531810613605977258",
    ]


def test_har_golden_usage_reasoning_tokens_slot9():
    """抓包 usage：[prompt, visible, total, cached, promptDetails, ..., reasoning@9]。

    实测样本 [106,24,179,null,[[1,106]],null,null,null,null,49]：
    total(179) = prompt(106) + visible(24) + reasoning(49)。
    """
    from aistudio_api.infrastructure.gateway.wire_parser import parse_usage_metadata

    usage = parse_usage_metadata(
        [106, 24, 179, None, [[1, 106]], None, None, None, None, 49]
    )
    assert usage["prompt_tokens"] == 106
    assert usage["completion_tokens"] == 24 + 49
    assert usage["total_tokens"] == 179
    details = usage["completion_tokens_details"]
    assert isinstance(details, dict)
    assert details["reasoning_tokens"] == 49


def test_har_session_token_v1_shape():
    """抓包：响应槽 7 的 v1_ 会话 token 下轮请求写回槽 11（SESSION_TOKEN）。"""
    token = "v1_ChcyaXo1YXF1R0s2Zl8wX3dQbWVLbDhRWRIXMml6NWF1V0tNNjJob19VUHphcXdtQUk"
    assert token.startswith("v1_")
    from aistudio_api.infrastructure.gateway.model_turns import _TOKEN_RE

    assert _TOKEN_RE.fullmatch(token)


@pytest.mark.asyncio
async def test_har_openai_file_block_carries_filename_to_inline_slot5():
    """抓包 idx 88：上传截图 inlineData = [mime, b64, null, null, null, 文件名]。

    OpenAI file.filename 透传链路：compat 入口 → GeminiInlineData →
    AistudioPart.inline_filename → wire 槽 5。
    """
    from aistudio_api.application.compat_service import _openai_parts_from_content

    parts = await _openai_parts_from_content(
        [
            {
                "type": "file",
                "file": {
                    "file_data": "data:image/jpeg;base64,/9j/4QFc",
                    "filename": "Screenshot_20261115_115206.jpg",
                },
            }
        ]
    )
    assert parts[0].inlineData is not None
    assert parts[0].inlineData.filename == "Screenshot_20261115_115206.jpg"

    from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

    wire = AistudioPart(
        inline_data=("image/jpeg", "/9j/4QFc"),
        inline_filename=parts[0].inlineData.filename,
    ).to_wire()
    assert wire[2] == [
        "image/jpeg",
        "/9j/4QFc",
        None,
        None,
        None,
        "Screenshot_20261115_115206.jpg",
    ]
