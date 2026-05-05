"""System and metadata routes."""

from __future__ import annotations

from fastapi import APIRouter

from aistudio_api.application.api_service import health_response, stats_response

router = APIRouter()


@router.get("/health")
async def health():
    return health_response()


@router.get("/stats")
async def stats():
    return stats_response()

