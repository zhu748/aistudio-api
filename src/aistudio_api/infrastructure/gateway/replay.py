"""Captured request replay workflow."""

from __future__ import annotations

import logging

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.session import BrowserSession

logger = logging.getLogger("aistudio")


class RequestReplayService:
    def __init__(self, session: BrowserSession):
        self._session = session

    async def replay(self, captured: CapturedRequest | None, body: str, timeout: int | None = None) -> tuple[int, bytes]:
        if not captured:
            return 0, b""

        if timeout is None:
            timeout = settings.timeout_replay

        ctx = await self._session.ensure_context()
        headers = {k: v for k, v in captured.headers.items() if k.lower() not in ("host", "content-length")}

        try:
            resp = await ctx.request.post(
                captured.url,
                data=body,
                headers=headers,
                timeout=timeout * 1000,
            )
            raw = await resp.body()
            return resp.status, raw
        except Exception as exc:
            logger.error("Replay error: %s", exc)
            return 0, str(exc).encode()

