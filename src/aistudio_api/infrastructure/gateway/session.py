"""Shared browser session management for gateway operations."""

from __future__ import annotations

import json
import os
import urllib.error

from aistudio_api.config import settings
from aistudio_api.infrastructure.browser.camoufox_manager import CamoufoxManager


class BrowserSession:
    def __init__(self, port: int):
        self.port = port
        self._pw = None
        self._browser = None
        self._ctx = None
        self._camoufox_manager = CamoufoxManager(
            port=port,
            auth_profile=settings.auth_file,
            headless=settings.camoufox_headless,
        )

    async def ensure_context(self):
        if self._ctx:
            try:
                await self._ctx.pages
                return self._ctx
            except Exception:
                self._ctx = None

        from playwright.async_api import async_playwright

        if not self._pw:
            self._pw = await async_playwright().start()

        import urllib.request as ur

        try:
            resp = ur.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5)
        except urllib.error.URLError:
            await self._camoufox_manager.start()
            try:
                resp = ur.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5)
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"Camoufox is not available on 127.0.0.1:{self.port}. "
                    "Auto-start was attempted but the debug endpoint is still unreachable."
                ) from exc

        data = json.loads(resp.read())
        ws_url = f"ws://127.0.0.1:{self.port}{data['wsEndpointPath']}"

        self._browser = await self._pw.firefox.connect(ws_url)
        ctx_kwargs = {}
        if settings.auth_file and os.path.exists(settings.auth_file):
            ctx_kwargs["storage_state"] = settings.auth_file
        self._ctx = await self._browser.new_context(**ctx_kwargs)
        return self._ctx
