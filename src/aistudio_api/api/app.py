"""FastAPI application entrypoint."""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aistudio_api.infrastructure.gateway.client import AIStudioClient

from .routes_gemini import router as gemini_router
from .routes_openai import router as openai_router
from .routes_system import router as system_router
from .state import runtime_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("aistudio.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    runtime_state.client = AIStudioClient(port=runtime_state.camoufox_port)
    runtime_state.busy_lock = asyncio.Lock()
    logger.info("Client initialized (camoufox port=%s)", runtime_state.camoufox_port)
    yield
    logger.info("Shutting down")
    runtime_state.client = None
    runtime_state.busy_lock = None


app = FastAPI(title="AI Studio API", lifespan=lifespan)
app.include_router(system_router)
app.include_router(gemini_router)
app.include_router(openai_router)


def main():
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio OpenAI-compatible API Server")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--camoufox-port", type=int, default=settings.camoufox_port)
    args = parser.parse_args()

    runtime_state.camoufox_port = args.camoufox_port

    import uvicorn

    logger.info("Starting server on port %s", args.port)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
