"""Models and metadata routes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.response_models import (
    GeminiModelListResponse,
    GeminiModelResponse,
)
from aistudio_api.application.anti_truncation import (
    BUFFER_VARIANT_SUFFIX,
    STREAM_VARIANT_SUFFIX,
    get_selected_variant_models,
    resolve_model_variant,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.models")

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState
router = APIRouter()


def _safe_int(value: object, default: int) -> int:
    """把上游可能为字符串/脏值的整数配置转为 int，失败回退安全默认值。

    上游若返回非数字字符串（脏数据/协议变更），映射不应 500。
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _to_gemini_model(model_data: Mapping[str, object]) -> GeminiModelResponse:
    raw_id = str(model_data.get("id") or model_data.get("name") or "")
    clean_id = raw_id.removeprefix("models/")
    name = f"models/{clean_id}"
    version = str(model_data.get("version") or "001")
    display_name = str(model_data.get("displayName") or clean_id)
    description = str(model_data.get("description") or f"Google {display_name} model")

    input_tokens = _safe_int(model_data.get("inputTokenLimit"), 1048576)
    output_tokens = _safe_int(model_data.get("outputTokenLimit"), 8192)
    raw_methods = model_data.get("supportedGenerationMethods")
    methods = (
        [str(x) for x in raw_methods]
        if isinstance(raw_methods, list) and raw_methods
        else [
            "generateContent",
            "countTokens",
            "createCachedContent",
        ]
    )
    raw_temp = model_data.get("temperature")
    temperature = (
        float(raw_temp)
        if isinstance(raw_temp, (int, float)) and not isinstance(raw_temp, bool)
        else 1.0
    )
    raw_top_p = model_data.get("topP")
    top_p = (
        float(raw_top_p)
        if isinstance(raw_top_p, (int, float)) and not isinstance(raw_top_p, bool)
        else 0.95
    )
    raw_top_k = model_data.get("topK")
    top_k = (
        int(raw_top_k)
        if isinstance(raw_top_k, (int, float)) and not isinstance(raw_top_k, bool)
        else 64
    )
    return GeminiModelResponse(
        name=name,
        version=version,
        displayName=display_name,
        description=description,
        inputTokenLimit=input_tokens,
        outputTokenLimit=output_tokens,
        supportedGenerationMethods=methods,
        temperature=temperature,
        topP=top_p,
        topK=top_k,
    )


def _to_variant_model(
    model_data: Mapping[str, object], *, buffered: bool
) -> GeminiModelResponse:
    """在基础模型上叠加抗截断变体后缀与展示名。"""
    base = _to_gemini_model(model_data)
    clean_id = str(model_data.get("id") or model_data.get("name") or "").removeprefix(
        "models/"
    )
    if buffered:
        return base.model_copy(
            update={
                "name": f"models/{clean_id}{BUFFER_VARIANT_SUFFIX}",
                "displayName": f"{base.displayName}（非流式抗截断）",
                "description": f"{base.description}（抗截断传输：缓冲上游，整体还原后单条下发）",
            }
        )
    return base.model_copy(
        update={
            "name": f"models/{clean_id}{STREAM_VARIANT_SUFFIX}",
            "displayName": f"{base.displayName}（流式抗截断）",
            "description": f"{base.description}（抗截断传输：流式增量还原）",
        }
    )


def _expand_anti_truncation_variants(
    discovered: Sequence[Mapping[str, object]],
) -> list[GeminiModelResponse]:
    """为面板选中的模型生成流式 / 非流式两个抗截断变体条目。

     变体功能关闭时返回空；生图 / 视频 / 音频模型会被抗截断传输安全
    旁路，不暴露变体以免误导。"""
    selected = get_selected_variant_models()
    if not selected:
        return []
    variants: list[GeminiModelResponse] = []
    for item in discovered:
        clean_id = str(item.get("id") or item.get("name") or "").removeprefix("models/")
        if clean_id.lower() not in selected:
            continue
        if (
            item.get("is_image_model")
            or item.get("is_video_model")
            or item.get("is_audio_model")
        ):
            continue
        variants.append(_to_variant_model(item, buffered=False))
        variants.append(_to_variant_model(item, buffered=True))
    return variants


@router.get("/v1beta/models", response_model=GeminiModelListResponse)
async def list_models(
    runtime_state: RuntimeState = Depends(get_runtime_state),
    force_refresh: bool = False,
) -> GeminiModelListResponse:
    """列出可用模型。

    查询参数 force_refresh=true 时跳过缓存向上游强制拉取（管理面板
    「刷新模型列表」用）；常规调用仍走 10 分钟缓存。
    """
    session = runtime_state.client.session if runtime_state.client else None
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    discovered = await model_discovery.get_models(
        session=session, force_refresh=bool(force_refresh)
    )
    models = [_to_gemini_model(item) for item in discovered]
    models.extend(_expand_anti_truncation_variants(discovered))
    logger.debug("返回模型列表: count=%d, force_refresh=%s", len(models), force_refresh)
    return GeminiModelListResponse(models=models)


@router.get("/v1beta/models/{model_id:path}", response_model=GeminiModelResponse)
async def get_model(
    model_id: str,
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> GeminiModelResponse:
    session = runtime_state.client.session if runtime_state.client else None
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    # 抗截断变体详情：带后缀的模型 ID 直接返回变体元数据。
    variant = resolve_model_variant(model_id)
    if variant is not None:
        base_info = await model_discovery.get_model_by_id(
            variant.model_path, session=session
        )
        if base_info is not None:
            return _to_variant_model(base_info, buffered=variant.buffered)
        raise HTTPException(status_code=404, detail="Model not found")

    model_info = await model_discovery.get_model_by_id(model_id, session=session)
    if model_info is not None:
        logger.debug("返回模型详情: model=%s", model_id)
        response = _to_gemini_model(model_info)
        # 别名请求保持请求名（与官方 API 行为一致），
        # 例: gemini-3.7-flash 已是 gemini-3.8-flash 的上游别名，
        # 请求 3.7 时仍返回 name=models/gemini-3.7-flash。
        clean_requested = model_id.removeprefix("models/")
        clean_returned = (
            str(model_info.get("id") or model_info.get("name") or "")
            .removeprefix("models/")
            .removesuffix(BUFFER_VARIANT_SUFFIX)
            .removesuffix(STREAM_VARIANT_SUFFIX)
        )
        if clean_requested and clean_requested != clean_returned:
            response = response.model_copy(update={"name": f"models/{clean_requested}"})
        return response
    logger.warning("模型不存在: model=%s", model_id)
    raise HTTPException(status_code=404, detail="Model not found")
