"""OpenAI-compatible API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from aistudio_api.application.api_service import handle_chat, handle_image_generation
from aistudio_api.infrastructure.gateway.client import AIStudioClient

from .dependencies import get_client
from .schemas import ChatRequest, ImageRequest

router = APIRouter()

MODELS = [
    # Gemma 4 系列（开源，支持 thinking）
    {"id": "gemma-4-31b-it", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemma-4-12b-it", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemma-4-4b-it", "object": "model", "created": 1700000000, "owned_by": "google"},
    # Gemini 3 系列
    {"id": "gemini-3-flash-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # Gemini 3.1 系列
    {"id": "gemini-3.1-flash-lite-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-3.1-flash-image-preview", "object": "model", "created": 1700000000, "owned_by": "google"},
    # Gemini 2.5 系列
    {"id": "gemini-2.5-flash-preview-05-20", "object": "model", "created": 1700000000, "owned_by": "google"},
    {"id": "gemini-2.5-pro-preview-05-06", "object": "model", "created": 1700000000, "owned_by": "google"},
    # 图片生成
    {"id": "gemini-2.5-flash-preview-image-generation", "object": "model", "created": 1700000000, "owned_by": "google"},
]

MODEL_IDS = {m["id"] for m in MODELS}


@router.get("/v1/models")
async def list_models():
    return {"object": "list", "data": MODELS}


@router.get("/v1/models/{model_id:path}")
async def get_model(model_id: str):
    for m in MODELS:
        if m["id"] == model_id:
            return m
    raise HTTPException(status_code=404, detail={"message": f"Model '{model_id}' not found", "type": "invalid_request_error"})


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, client: AIStudioClient = Depends(get_client)):
    return await handle_chat(req, client)


@router.post("/v1/images/generations")
async def image_generations(req: ImageRequest, client: AIStudioClient = Depends(get_client)):
    return await handle_image_generation(req, client)

