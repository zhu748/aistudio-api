"""动态模型发现与元数据获取服务。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aistudio_api.infrastructure.gateway.session import GatewaySession

from aistudio_api.infrastructure.account.cookie_parser import (
    build_makersuite_headers,
    load_cookies_from_auth_file,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("model_discovery")


# ListModels field 65 (index 64) 主能力码语义映射
MODEL_CAPABILITY_NAMES: dict[int, str] = {
    1: "chat_model",
    9: "code_execution",
    10: "function_declarations",
    12: "google_search",
    13: "url_context",
    20: "video_route",
    21: "image_route",
    25: "thinking",
    26: "live_route",
    35: "thinking_budget",
    37: "speech_route",
    43: "media_resolution",
    46: "speech_translation",
    47: "aspect_ratio",
    48: "code_assistant",
    49: "output_resolution",
    52: "thinking_level",
    53: "music_route",
    54: "image_search",
    58: "google_maps",
    59: "interaction_route",
    85: "speech_metadata",
    91: "output_continuation",
}


def _extract_int_list(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    res: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            res.append(item)
        elif isinstance(item, float) and item.is_integer():
            res.append(int(item))
    return res


def _extract_model_aliases(raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw:
        return []
    if isinstance(raw[0], str):
        return [
            s.removeprefix("models/") for s in raw if isinstance(s, str) and s.strip()
        ]
    aliases: list[str] = []
    for item in raw:
        if (
            isinstance(item, list)
            and item
            and isinstance(item[0], str)
            and item[0].strip()
        ):
            aliases.append(item[0].removeprefix("models/"))
    return aliases


# 本地补充模型：无论上游 ListModels 是否返回，都确保出现在模型列表中
# （README 宣传的模型 / config.yaml 已配置对应付费规则与层级配额）
# 注意：gemini-4-hydrogen 为免费模型（线上目录实测 paid[77]=None /
# accessModes[82]=None），与 argon/carbon 的付费定位不同，不得挂付费规则。
# 付费/等级判定优先用上游目录数据（paid[77]/accessModes[82,87]），
# 随模型列表同一次 ListModels 拉取缓存并 600s 周期刷新；config.yaml
# 规则仅在模型不在目录或上游未标等级时兜底。
_EXTRA_GUARANTEED_MODELS = [
    {
        "id": "gemini-4-hydrogen",
        "displayName": "Gemini 4 Hydrogen",
        "description": "Gemini 4 Hydrogen flagship reasoning model",
    },
    {
        "id": "gemini-4-argon",
        "displayName": "Gemini 4 Argon",
        "description": "Gemini 4 Argon flagship reasoning model",
    },
    {
        "id": "gemini-4-carbon",
        "displayName": "Gemini 4 Carbon",
        "description": "Gemini 4 Carbon flagship reasoning model",
    },
]


def _merge_extra_models(
    models: list[dict[str, object]],
) -> list[dict[str, object]]:
    """将本地补充模型合并进模型列表（按 id 与别名去重，上游已存在则跳过）。"""
    known_ids: set[str] = set()
    for item in models:
        raw_id = str(item.get("id") or item.get("name") or "")
        known_ids.add(raw_id.removeprefix("models/").lower())
        raw_aliases = item.get("aliases")
        if isinstance(raw_aliases, list):
            known_ids.update(
                str(a).removeprefix("models/").lower() for a in raw_aliases
            )
    merged = list(models)
    for extra in _EXTRA_GUARANTEED_MODELS:
        if str(extra.get("id", "")).lower() in known_ids:
            continue
        merged.append(dict(extra))
    return merged


# 本地保底默认模型列表（当网络未就绪或未配置账号时使用）。
# 数据来源: 上游 ListModels 实测快照（含思考等级与别名映射；
# 补录 gemini-4-carbon 与目录 [83] 开关映射；付费条目含实测 accessModes，
# 冷启动时等级判定与线上一致：[1,3]=Pro+，[1,4]=仅 Ultra 系）。
# 覆盖旗舰/Flash/图像(Omni)/音乐(Lyria)/TTS/转写/Live/智能体/机器人/开源 Gemma 全类别。
_FALLBACK_MODELS = [
    {
        "id": "antigravity-preview-09-2026",
        "displayName": "Antigravity Agent Preview",
        "description": "Preview release of Antigravity Agent (09-2026)",
        "category": "agent",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "aliases": ["featured-agent", "antigravity-preview-05-2026"],
        "paid": True,
        "accessModes": [1],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "deep-research-preview-04-2026",
        "displayName": "Deep Research Preview (Apr-21-2026)",
        "description": "Preview release (April 21th, 2026) of Deep Research",
        "category": "agent",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "aliases": ["deep-research-pro-preview-12-2025"],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "deep-research-max-preview-04-2026",
        "displayName": "Deep Research Max Preview (Apr-21-2026)",
        "description": "Preview release (April 21st, 2026) of Deep Research Max",
        "category": "agent",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-4-argon",
        "displayName": "Gemini 4 Argon",
        "description": "Gemini 4 Argon",
        "category": "flagship",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 32768,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 2,
        "thinkingLevels": [1, 2, 3],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-4-carbon",
        "displayName": "Gemini 4 Carbon",
        "description": "Gemini 4 Carbon",
        "category": "flagship",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 32768,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 2,
        "thinkingLevels": [1, 2, 3],
        "paid": True,
        "accessModes": [1, 4],
        "supportedGenerationMethods": [
            "generateContent",
            "countTokens",
            "batchGenerateContent",
        ],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        # 2026-11-12 线上目录实测收录：免费 4 系列模型（paid[77]=None /
        # accessModes[82]=None，Free 号可用）；输出上限 65536 高于 carbon，
        # 思考档 [1,2,3,5] 默认 HIGH(3)，[83]=1。
        "id": "gemini-4-hydrogen",
        "displayName": "Gemini 4 Hydrogen",
        "description": "Gemini 4 Hydrogen",
        "category": "flagship",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [1, 2, 3, 5],
        "aliases": [],
        "paid": False,
        "supportedGenerationMethods": [
            "generateContent",
            "countTokens",
            "batchGenerateContent",
        ],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.8-flash",
        "displayName": "Gemini 3.8 Flash",
        "description": "Gemini 3.8 Flash",
        "category": "flash",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 2,
        "thinkingLevels": [1, 2, 3],
        "aliases": [
            "gemini-3.7-flash",
            "gemini-2.5-flash",
            "gemini-2.5-flash-preview-05-20",
            "gemini-2.5-flash-preview-04-17",
            "gemini-2.0-flash-thinking-exp-1219",
            "gemini-2.0-flash-thinking-exp-01-21",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "learnlm-2.0-flash-experimental",
            "learnlm-1.5-pro-experimental",
        ],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.8-flash-tts",
        "displayName": "Gemini 3.8 Flash TTS",
        "description": "Gemini 3.8 Flash TTS",
        "category": "audio",
        "inputTokenLimit": 8192,
        "outputTokenLimit": 16384,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "gemini-3.5-flash-lite",
        "displayName": "Gemini 3.5 Flash Lite",
        "description": "Gemini 3.5 Flash Lite",
        "category": "flash",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": 4,
        "thinkingLevels": [4, 1, 2, 3],
        "aliases": [
            "thorin",
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash-lite-preview-06-17",
            "gemini-2.0-flash-lite",
            "gemini-2.0-flash-lite-001",
            "gemini-2.0-flash-lite-preview",
            "gemini-2.0-flash-lite-preview-02-05",
        ],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.1-pro-preview",
        "displayName": "Gemini 3.1 Pro Preview",
        "description": "Gemini 3.1 Pro Preview",
        "category": "flagship",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [1, 2, 3],
        "aliases": [
            "gemini-3-pro-preview",
            "gemini-2.5-pro",
            "gemini-2.5-pro-preview-06-05",
            "gemini-exp-1206",
            "gemini-2.0-pro-exp-02-05",
            "gemini-2.5-pro-exp-03-25",
            "gemini-2.5-pro-preview-03-25",
            "gemini-1.5-pro",
            "gemini-2.5-pro-preview-05-06",
        ],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.6-flash",
        "displayName": "Gemini 3.6 Flash",
        "description": "Gemini 3.6 Flash",
        "category": "flash",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 2,
        "thinkingLevels": [4, 1, 2, 3],
        "aliases": ["gemini-3.5-flash"],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.5-live-translate-preview",
        "displayName": "Gemini 3.5 Live Translate Preview",
        "description": "Gemini 3.5 Live Translate Preview",
        "category": "live",
        "inputTokenLimit": 16384,
        "outputTokenLimit": 32768,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.1-flash-lite",
        "displayName": "Gemini 3.1 Flash Lite",
        "description": "Gemini 3.1 Flash Lite",
        "category": "flash",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": 4,
        "thinkingLevels": [4, 1, 2, 3],
        "aliases": ["gemini-3.1-flash-lite-preview"],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3-flash-preview",
        "displayName": "Gemini 3 Flash Preview",
        "description": "Gemini 3 Flash Preview",
        "category": "flash",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [4, 1, 2, 3],
        "aliases": ["gemini-2.0-flash", "gemini-2.0-flash-001"],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-nano-banana-2.1",
        "displayName": "Nano Banana 2.1",
        "description": "Gemini Nano Banana 2.1.",
        "category": "image",
        "inputTokenLimit": 65536,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 2,
        "thinkingLevels": [4, 2, 3],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": True,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.1-flash-lite-image",
        "displayName": "Nano Banana 2 Lite",
        "description": "Gemini 3.1 Flash Lite Image.",
        "category": "image",
        "inputTokenLimit": 65536,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 4,
        "thinkingLevels": [4, 3],
        "aliases": [
            "nano-banana",
            "gemini-2.0-flash-preview-image-generation",
            "gemini-2.0-flash-exp",
            "gemini-2.0-flash-exp-image-generation",
        ],
        "paid": False,
        "is_image_model": True,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.1-flash-image",
        "displayName": "Nano Banana 2",
        "description": "Gemini 3.1 Flash Image.",
        "category": "image",
        "inputTokenLimit": 65536,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 4,
        "thinkingLevels": [4, 3],
        "aliases": ["gemini-3.1-flash-image-preview"],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": True,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3-pro-image",
        "displayName": "Nano Banana Pro",
        "description": "Gemini 3 Pro Image",
        "category": "image",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 32768,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "aliases": ["gemini-3-pro-image-preview", "nano-banana-pro"],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": True,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.5-transcribe",
        "displayName": "Gemini 3.5 Transcribe",
        "description": "Gemini 3.5 Transcribe",
        "category": "audio",
        "inputTokenLimit": 98304,
        "outputTokenLimit": 32768,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "gemini-2.5-flash-image",
        "displayName": "Nano Banana",
        "description": "Gemini 2.5 Flash Preview Image",
        "category": "image",
        "inputTokenLimit": 32768,
        "outputTokenLimit": 32768,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "aliases": ["gemini-2.5-flash-image-preview", "gemini-2.5-flash-preview-image"],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": True,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-pro-latest",
        "displayName": "Gemini Pro Latest",
        "description": "Latest release of Gemini Pro",
        "category": "flagship",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [1, 2, 3],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-flash-latest",
        "displayName": "Gemini Flash Latest",
        "description": "Latest release of Gemini Flash",
        "category": "flash",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 2,
        "thinkingLevels": [1, 2, 3],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-flash-lite-latest",
        "displayName": "Gemini Flash-Lite Latest",
        "description": "Latest release of Gemini Flash-Lite",
        "category": "flash",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": 4,
        "thinkingLevels": [4, 1, 2, 3],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-robotics-er-2-preview",
        "displayName": "Gemini Robotics-ER 2 Preview",
        "description": "Gemini Robotics-ER 2 Preview",
        "category": "robotics",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [4, 1, 2, 3],
        "aliases": ["featured-robotics", "gemini-robotics-er-1.6-preview"],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.8-live",
        "displayName": "Gemini 3.8 Live",
        "description": "Gemini 3.8 Live",
        "category": "live",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "aliases": ["talker-only"],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.8-live-extended-thinking",
        "displayName": "Gemini 3.8 Live Extended Thinking",
        "description": "Gemini 3.8 Live Extended Thinking",
        "category": "live",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": 1,
        "thinkingLevels": [1, 2, 3],
        "aliases": ["thinker-talker"],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.1-flash-live-preview",
        "displayName": "Gemini 3.1 Flash Live Preview",
        "description": "Gemini 3.1 Flash Live Preview",
        "category": "live",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": 4,
        "thinkingLevels": [4, 1, 2, 3],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.5-transcribe-live",
        "displayName": "Gemini 3.5 Transcribe Live",
        "description": "Gemini 3.5 Transcribe Live",
        "category": "audio",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "gemini-robotics-er-2-streaming-preview",
        "displayName": "Gemini Robotics-ER 2 Streaming Preview",
        "description": "Gemini Robotics-ER 2 Streaming Preview",
        "category": "robotics",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [4, 1, 2, 3],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemini-3.8-flash-lite-tts",
        "displayName": "Gemini 3.8 Flash Lite TTS",
        "description": "Gemini 3.8 Flash Lite TTS",
        "category": "audio",
        "inputTokenLimit": 8192,
        "outputTokenLimit": 16384,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "gemini-3.1-flash-tts-preview",
        "displayName": "Gemini 3.1 Flash TTS Preview",
        "description": "Gemini 3.1 Flash TTS Preview",
        "category": "audio",
        "inputTokenLimit": 8192,
        "outputTokenLimit": 16384,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "lyria-3.5",
        "displayName": "Lyria 3.5",
        "description": "Music Generation model",
        "category": "audio",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "lyria-3-pro-preview",
        "displayName": "Lyria 3 Pro Preview",
        "description": "Lyria 3 Pro Preview",
        "category": "audio",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "lyria-3-clip-preview",
        "displayName": "Lyria 3 Clip Preview",
        "description": "Lyria 3 30s model Preview",
        "category": "audio",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "gemini-2.5-pro-preview-tts",
        "displayName": "Gemini 2.5 Pro Preview TTS",
        "description": "Gemini 2.5 Pro Preview TTS",
        "category": "audio",
        "inputTokenLimit": 8192,
        "outputTokenLimit": 16384,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "gemini-2.5-flash-preview-tts",
        "displayName": "Gemini 2.5 Flash Preview TTS",
        "description": "Gemini 2.5 Flash Preview TTS",
        "category": "audio",
        "inputTokenLimit": 8192,
        "outputTokenLimit": 16384,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": True,
    },
    {
        "id": "gemini-omni-1.2-flash",
        "displayName": "Gemini Omni 1.2 Flash",
        "description": "Gemini Omni 1.2 Flash",
        "category": "video",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": None,
        "thinkingLevels": [],
        "aliases": ["omni-featured"],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": True,
        "is_audio_model": False,
    },
    {
        "id": "gemini-omni-1.1-flash",
        "displayName": "Gemini Omni 1.1 Flash",
        "description": "Gemini Omni 1.1 Flash ",
        "category": "video",
        "inputTokenLimit": 131072,
        "outputTokenLimit": 65536,
        "unknownFlag13": 1,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [3, 1],
        "aliases": [
            "veo-3.1-generate-preview",
            "veo-3.1-fast-generate-preview",
            "veo-3.1-lite-generate-preview",
            "veo-2.0-generate-001",
            "gemini-omni-flash-preview",
        ],
        "paid": True,
        "accessModes": [1, 3],
        "is_image_model": False,
        "is_video_model": True,
        "is_audio_model": False,
    },
    {
        "id": "gemma-4-26b-a4b-it",
        "displayName": "Gemma 4 26B A4B IT",
        "description": "Gemma 4 26B A4B IT",
        "category": "gemma",
        "inputTokenLimit": 262144,
        "outputTokenLimit": 32768,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [4, 3],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
    {
        "id": "gemma-4-31b-it",
        "displayName": "Gemma 4 31B IT",
        "description": "Gemma 4 31B IT",
        "category": "gemma",
        "inputTokenLimit": 262144,
        "outputTokenLimit": 32768,
        "defaultThinkingLevel": 3,
        "thinkingLevels": [4, 3],
        "paid": False,
        "is_image_model": False,
        "is_video_model": False,
        "is_audio_model": False,
    },
]


class ModelDiscoveryService:
    """动态获取 Google AI Studio 支持的模型列表（带缓存与保底）。"""

    def __init__(self, cache_ttl_seconds: int = 600) -> None:
        self._cache_ttl = cache_ttl_seconds
        self._cached_models: list[dict[str, object]] = []
        self._last_fetched_at: float = 0.0
        # 锁按事件循环惰性创建：模块级单例在 import 时创建 asyncio.Lock，
        # 若被多个事件循环先后使用（测试、内嵌场景），第二个 loop 上 acquire
        # 会永久挂起或抛 "bound to a different event loop"。与 transport 层
        # 的 _get_client_lock 同方案。
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        """返回与当前事件循环绑定的拉取互斥锁。"""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def get_models(
        self,
        *,
        force_refresh: bool = False,
        cookies: dict[str, str] | None = None,
        auth_user: str = "0",
        session: GatewaySession | None = None,
    ) -> list[dict[str, object]]:
        """获取可用模型列表。"""
        now = time.time()
        if (
            not force_refresh
            and self._cached_models
            and (now - self._last_fetched_at < self._cache_ttl)
        ):
            return self._cached_models

        async with self._get_lock():
            # 双重检查
            if (
                not force_refresh
                and self._cached_models
                and (time.time() - self._last_fetched_at < self._cache_ttl)
            ):
                return self._cached_models

            # 1. 通过直接 HTTP 协议拉取
            effective_cookies = cookies
            effective_auth_user = auth_user
            effective_tier = "Free"
            effective_proxy = None
            if session and session.active_context:
                effective_proxy = session.active_context.proxy_url
            if not effective_cookies:
                auth_file = (
                    getattr(session, "_auth_file", None)
                    if session is not None
                    else None
                )
                if not auth_file:
                    try:
                        from aistudio_api.infrastructure.account.account_store import (
                            AccountStore,
                        )

                        store = AccountStore()
                        active = store.get_active_account()
                        if active:
                            auth_path = store.get_auth_path_optional(
                                active.id, require_exists=True
                            )
                            auth_file = str(auth_path) if auth_path else None
                            effective_auth_user = active.auth_user or "0"
                            effective_proxy = active.proxy_url
                            effective_tier = getattr(active, "tier", "Free") or "Free"
                    except Exception:
                        pass
                if auth_file:
                    effective_cookies = load_cookies_from_auth_file(auth_file) or None
                    if session is not None and hasattr(
                        session, "get_current_auth_user"
                    ):
                        effective_auth_user = session.get_current_auth_user()
                    if session is not None and hasattr(session, "get_current_tier"):
                        effective_tier = session.get_current_tier()

            if effective_cookies:
                try:
                    models = await self._fetch_via_http(
                        effective_cookies,
                        effective_auth_user,
                        tier=effective_tier,
                        proxy_url=effective_proxy,
                    )
                    if models:
                        merged = _merge_extra_models(models)
                        self._cached_models = merged
                        self._last_fetched_at = time.time()
                        logger.info("已更新上游模型列表: count=%d", len(merged))
                        return merged
                except Exception as e:
                    logger.warning(
                        "拉取上游模型列表失败，将使用本地缓存/内置列表: %s", e
                    )
                    # 有旧缓存但拉取失败时也写短 TTL 负缓存：否则 _last_fetched_at
                    # 不更新，后续每次 get_models 都会重新拿锁、对已挂掉的上游
                    # 发 10s 超时请求并在锁上串行堆积，拖慢所有 /models 请求。
                    if self._cached_models:
                        self._last_fetched_at = time.time() - self._cache_ttl + 60.0
            # 3. 保底返回本地默认模型列表（含本地补充模型）
            if not self._cached_models:
                self._cached_models = _merge_extra_models(
                    [dict(m) for m in _FALLBACK_MODELS]
                )
                # 上游拉取失败且无缓存时只给短 TTL，避免启动时一次瞬时网络故障
                # 导致 10 分钟内不再尝试上游（L1）。
                self._last_fetched_at = time.time() - self._cache_ttl + 60.0

            return self._cached_models

    async def get_model_by_id(
        self, model_id: str, session: GatewaySession | None = None
    ) -> Mapping[str, object] | None:
        """根据 model_id 或模型别名获取单个模型元数据（含保底匹配）。"""
        discovered = await self.get_models(session=session)
        # 与 get_cached_model_info 一致地大小写不敏感，避免同一别名仅因大小写
        # 不同在两个入口一个命中一个 404。
        clean_target = model_id.removeprefix("models/").lower()
        for model_info in discovered:
            model_raw_id = str(model_info.get("id") or model_info.get("name") or "")
            model_clean_id = model_raw_id.removeprefix("models/").lower()
            if model_clean_id == clean_target:
                return model_info
            raw_aliases = model_info.get("aliases")
            if isinstance(raw_aliases, list) and clean_target in [
                str(a).lower() for a in raw_aliases
            ]:
                return model_info
        for model_info in (*_FALLBACK_MODELS, *_EXTRA_GUARANTEED_MODELS):
            model_raw_id = str(model_info.get("id") or model_info.get("name") or "")
            model_clean_id = model_raw_id.removeprefix("models/").lower()
            if model_clean_id == clean_target:
                return model_info
        return None

    def get_cached_model_info(self, model_id: str) -> Mapping[str, object] | None:
        """同步查询已缓存的模型元数据（不触发网络请求）。"""
        clean_target = model_id.removeprefix("models/").lower()

        def _matches(model_info: Mapping[str, object]) -> bool:
            model_raw_id = str(model_info.get("id") or model_info.get("name") or "")
            if model_raw_id.removeprefix("models/").lower() == clean_target:
                return True
            raw_aliases = model_info.get("aliases")
            return isinstance(raw_aliases, list) and any(
                str(a).lower() == clean_target for a in raw_aliases
            )

        for model_info in self._cached_models:
            if _matches(model_info):
                return model_info
        # Cold cache: fall back to the guaranteed static catalogue so thinking
        # snapping / paid-tier detection still work before the first ListModels.
        for model_info in (*_FALLBACK_MODELS, *_EXTRA_GUARANTEED_MODELS):
            if _matches(model_info):
                return model_info
        return None

    def is_model_allowed_for_tier(self, model_id: str, tier: str | None) -> bool:
        """根据 config.yaml 付费规则以及缓存的 ListModels accessModes 判断指定订阅等级是否可访问该模型。"""
        req_tiers = self.get_required_tiers(model_id)
        if req_tiers:
            norm_tier = (tier or "Free").strip()
            return norm_tier in req_tiers

        info = self.get_cached_model_info(model_id)
        if not info:
            return True
        raw_modes = info.get("accessModes")
        if not isinstance(raw_modes, list) or not raw_modes:
            return True
        norm_tier = (tier or "Free").strip().lower()
        is_high_tier = (
            norm_tier in ("plus", "pro", "ultra")
            or "ultra" in norm_tier
            or "pro" in norm_tier
        )
        is_ultra_tier = "ultra" in norm_tier
        for mode in raw_modes:
            if mode == 3 and is_high_tier:
                return True
            if mode == 4 and is_ultra_tier:
                return True
        # 如果 accessModes 只包含 1 (Paid API key) 且不含 3/4，对所有网页端等级放行尝试
        return all(m not in (3, 4) for m in raw_modes)

    def is_paid_model(self, model_id: str) -> bool:
        """判定目标模型是否为付费模型（上游 ListModels 目录数据优先，config.yaml 仅兜底）。

        优先级（2026-11-12 上游实测交叉验证，随模型列表同一次拉取 600s 刷新）：
        1. 目录明确结论：paid=True（ListModels 槽位 [77]==2）、accessModes 含
           3/4（订阅门槛）或能力码 20（视频路由）→ 付费；paid=False 且无
           门槛 → 免费。上游结论直接生效，config.yaml 关键词规则不再覆盖。
        2. 上游无该模型（未知 ID / 兼并条目未带 paid）→ 才回落 config.yaml
           paid_model_rules 的 contains/prefixes/exact 匹配。
        """
        clean_m = model_id.removeprefix("models/").lower()
        info = self.get_cached_model_info(clean_m)
        if info:
            if info.get("paid") is True:
                return True
            modes = info.get("accessModes")
            if isinstance(modes, list) and any(m in (3, 4) for m in modes):
                return True
            codes = info.get("capabilityCodes")
            if isinstance(codes, list) and 20 in codes:
                return True
            # 上游明确收录且无任何付费标记 → 免费模型，直接返回，
            # 不再走 config.yaml 模糊匹配（避免关键词误伤免费模型）。
            if info.get("paid") is False:
                return False
            # paid 未知（None，如 _EXTRA_GUARANTEED_MODELS 条目未带 paid
            # 字段）→ 结论不明确，落到 config.yaml 规则。

        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_configured_paid_model_rules,
        )

        rules = get_configured_paid_model_rules()
        for rule_cfg in rules.values():
            if not isinstance(rule_cfg, dict):
                continue
            contains = [
                str(c).lower()
                for c in (rule_cfg.get("contains") or [])
                if isinstance(c, str)
            ]
            prefixes = [
                str(p).lower()
                for p in (rule_cfg.get("prefixes") or [])
                if isinstance(p, str)
            ]
            exact = [
                str(e).removeprefix("models/").lower()
                for e in (rule_cfg.get("exact") or [])
                if isinstance(e, str)
            ]
            if (
                (exact and clean_m in exact)
                or any(clean_m.startswith(p) for p in prefixes)
                or any(c in clean_m for c in contains)
            ):
                return True
        return False

    def get_required_tier(self, model_id: str) -> str:
        """获取目标模型所需的最低账号层级（兼容现有单层级调用）。"""
        tiers = self.get_required_tiers(model_id)
        if not tiers:
            return "Free"
        return tiers[0]

    def get_required_tiers(self, model_id: str) -> list[str]:
        """获取目标模型所允许调用的账号层级列表（上游 accessModes 优先，config.yaml 兜底）。

        语义（2026-11-12 上游 ListModels 实测交叉验证，随模型列表 600s 刷新）：
        - accessModes [1,3] → Pro 及以上（Pro/Ultra/Ultra_20X）
        - accessModes [1,4] → 仅 Ultra 系（Ultra/Ultra_20X）
        - paid=False 且无 modes → 免费模型，任何等级可用（返回空列表）
        - modes 仅 [1]（Paid API key，如 antigravity）或 paid=True 无
          modes → 上游未给网页端等级标记 → 回落 config.yaml 规则
        - 模型不在目录 → config.yaml 规则，仍无匹配 → 空列表
        """
        clean_m = model_id.removeprefix("models/").lower()
        info = self.get_cached_model_info(clean_m)
        modes: list[int] = []
        if info:
            raw_modes = info.get("accessModes")
            if isinstance(raw_modes, list):
                modes = [m for m in raw_modes if isinstance(m, int)]
        if modes:
            if 4 in modes and 3 not in modes:
                return ["Ultra", "Ultra_20X"]
            if 3 in modes:
                return ["Pro", "Ultra", "Ultra_20X"]
        elif info and info.get("paid") is False:
            # 上游明确免费：不进 config 模糊匹配（避免 contains "pro"/
            # "image" 等关键词误伤免费模型）。
            return []

        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_configured_paid_model_rules,
        )

        rules = get_configured_paid_model_rules()
        for rule_cfg in rules.values():
            if not isinstance(rule_cfg, dict):
                continue
            contains = [
                str(c).lower()
                for c in (rule_cfg.get("contains") or [])
                if isinstance(c, str)
            ]
            prefixes = [
                str(p).lower()
                for p in (rule_cfg.get("prefixes") or [])
                if isinstance(p, str)
            ]
            exact = [
                str(e).removeprefix("models/").lower()
                for e in (rule_cfg.get("exact") or [])
                if isinstance(e, str)
            ]
            if (
                (exact and clean_m in exact)
                or any(clean_m.startswith(p) for p in prefixes)
                or any(c in clean_m for c in contains)
            ):
                req = rule_cfg.get("required_tiers")
                if isinstance(req, list) and req:
                    return [str(t) for t in req]

        return []

    async def _fetch_via_http(
        self,
        cookies: dict[str, str],
        auth_user: str,
        *,
        tier: str = "Free",
        proxy_url: str | None = None,
    ) -> list[dict[str, object]]:
        """通过 HTTP 客户端携带 SAPISIDHASH 拉取。"""
        import httpx

        headers = build_makersuite_headers(
            cookies,
            auth_user=auth_user or "0",
            tier=tier,
            include_visit_id=True,
            visit_scope="default",
        )

        async def _resolve_listmodels_proxy(raw: str | None) -> str | None:
            """传入代理或全局默认 → httpx 可用形态（turn/sstp 转本地 SOCKS5）。"""
            from aistudio_api.config import settings

            effective = raw or settings.proxy_url
            if not effective:
                return None
            from aistudio_api.infrastructure.proxy_bridge import (
                resolve_proxy_url_async,
            )

            return await resolve_proxy_url_async(effective)

        url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels"
        # 复用 transport 层的共享 SSLContext 与 h2 探测：裸建 AsyncClient 会
        # 每次重新枚举系统证书库（Windows 上单次 0.7~1.4s）且丢失 HTTP/2。
        from aistudio_api.infrastructure.gateway.transport import (
            _http2_enabled,
            _shared_ssl_context,
        )

        async with httpx.AsyncClient(
            timeout=10.0,
            proxy=await _resolve_listmodels_proxy(proxy_url),
            trust_env=False,
            http2=_http2_enabled(),
            verify=_shared_ssl_context(),
        ) as client:
            resp = await client.post(url, headers=headers, content="[]")
            if resp.status_code == 200:
                # batchexecute 系端点响应携带 XSSI 防护前缀 )]}}'，
                # 必须先剥离再解析（与 GenerateContent/GetAiStudioBenefitTier 同法）。
                text = resp.text.strip()
                if text.startswith(")]}'"):
                    text = text[4:].lstrip()
                try:
                    raw_data = json.loads(text)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "ListModels 响应 JSON 解析失败（已剥离 XSSI 前缀）: %s", e
                    )
                    return []
                return self._parse_raw_models(raw_data)
        return []

    def _parse_raw_models(self, raw_data: object) -> list[dict[str, object]]:
        """解析 Google ListModels 的原始 protobuf 数组结构。"""
        if (
            not isinstance(raw_data, list)
            or not raw_data
            or not isinstance(raw_data[0], list)
        ):
            return []

        models: list[dict[str, object]] = []
        for item in raw_data[0]:
            if not isinstance(item, list) or not item or not isinstance(item[0], str):
                continue
            raw_id = item[0]  # e.g. "models/gemini-3.7-flash"
            model_id = raw_id.replace("models/", "")
            version = (
                str(item[2]).strip()
                if len(item) > 2 and isinstance(item[2], str) and item[2].strip()
                else "001"
            )
            display_name = (
                item[3]
                if len(item) > 3 and isinstance(item[3], str) and item[3].strip()
                else model_id
            )
            description = item[4] if len(item) > 4 and isinstance(item[4], str) else ""
            input_token_limit = (
                item[5] if len(item) > 5 and isinstance(item[5], (int, float)) else 0
            )
            output_token_limit = (
                item[6] if len(item) > 6 and isinstance(item[6], (int, float)) else 0
            )
            methods = item[7] if len(item) > 7 and isinstance(item[7], list) else []
            supported_methods = [str(m) for m in methods if isinstance(m, str)]
            temperature = (
                float(item[8])
                if len(item) > 8
                and isinstance(item[8], (int, float))
                and not isinstance(item[8], bool)
                else 1.0
            )
            top_p = (
                float(item[9])
                if len(item) > 9
                and isinstance(item[9], (int, float))
                and not isinstance(item[9], bool)
                else 0.95
            )
            top_k = (
                int(item[10])
                if len(item) > 10
                and isinstance(item[10], (int, float))
                and not isinstance(item[10], bool)
                else 64
            )
            aliases = _extract_model_aliases(item[56]) if len(item) > 56 else []
            capability_codes = _extract_int_list(item[64]) if len(item) > 64 else []
            capabilities = [
                MODEL_CAPABILITY_NAMES[c]
                for c in capability_codes
                if c in MODEL_CAPABILITY_NAMES
            ]
            default_thinking_level: int | None = None
            thinking_levels: list[int] = []
            if len(item) > 71 and isinstance(item[71], list):
                t_cfg = item[71]
                if (
                    len(t_cfg) > 5
                    and isinstance(t_cfg[5], int)
                    and not isinstance(t_cfg[5], bool)
                ):
                    default_thinking_level = t_cfg[5]
                if len(t_cfg) > 6:
                    thinking_levels = _extract_int_list(t_cfg[6])
            # 2026-11 抓包逆向 + ListModels 交叉验证：目录槽位 [83]=1 与
            # GenerateContent 请求 generation_config[13]=1 严格对应
            # （gemini-3.8-flash / carbon / argon 等新代模型为 1，
            #   gemini-3.5-flash-lite 为 null）。
            unknown_flag_13: int | None = None
            if (
                len(item) > 83
                and isinstance(item[83], int)
                and not isinstance(item[83], bool)
            ):
                unknown_flag_13 = item[83]
            paid = bool(len(item) > 77 and item[77] == 2)
            # accessModes 主槽位 [82]，镜像槽位 [87]（实测两者恒等：carbon
            # [1,4] / argon [1,3]）；主槽缺失时用镜像兜底。模式语义：
            # 1=Paid API key、3=Pro 级可访问、4=仅 Ultra 系可访问。
            access_modes = _extract_int_list(item[82]) if len(item) > 82 else []
            if not access_modes and len(item) > 87:
                access_modes = _extract_int_list(item[87])

            # 过滤支持内容生成的模型
            if not any("generateContent" in str(m) for m in methods) and not any(
                "image" in model_id or "veo" in model_id or "lyria" in model_id
                for _ in [0]
            ):
                continue

            is_image = (
                21 in capability_codes or "image" in model_id or "imagen" in model_id
            )
            is_video = 20 in capability_codes or "veo" in model_id
            is_audio = (
                37 in capability_codes
                or 53 in capability_codes
                or "lyria" in model_id
                or "tts" in model_id
            )

            category = "flagship"
            if is_image:
                category = "image"
            elif is_video:
                category = "video"
            elif is_audio:
                category = "audio"
            elif "lite" in model_id or "8b" in model_id or "flash" in model_id:
                category = "flash"
            elif "gemma" in model_id:
                category = "gemma"

            models.append(
                {
                    "id": model_id,
                    "name": raw_id,
                    "version": version,
                    "displayName": display_name,
                    "description": description,
                    "category": category,
                    "inputTokenLimit": int(input_token_limit),
                    "outputTokenLimit": int(output_token_limit),
                    "supportedGenerationMethods": supported_methods
                    or [
                        "generateContent",
                        "countTokens",
                        "createCachedContent",
                    ],
                    "temperature": temperature,
                    "topP": top_p,
                    "topK": top_k,
                    "aliases": aliases,
                    "capabilities": capabilities,
                    "capabilityCodes": capability_codes,
                    "defaultThinkingLevel": default_thinking_level,
                    "thinkingLevels": thinking_levels,
                    "unknownFlag13": unknown_flag_13,
                    "paid": paid,
                    "accessModes": access_modes,
                    "is_image_model": is_image,
                    "is_video_model": is_video,
                    "is_audio_model": is_audio,
                }
            )

        return models


# 单例实例
model_discovery = ModelDiscoveryService()
