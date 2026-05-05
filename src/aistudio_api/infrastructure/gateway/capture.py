"""Browser-driven request capture workflow."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from aistudio_api.config import DEFAULT_TEXT_MODEL
from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.infrastructure.gateway.session import BrowserSession

logger = logging.getLogger("aistudio")


@dataclass
class CapturedRequest:
    url: str
    headers: dict[str, str]
    body: str
    model: str = ""
    snapshot: str = ""

    def __post_init__(self):
        parsed = json.loads(self.body)
        self.model = parsed[0] if parsed else ""
        self.snapshot = parsed[4] if len(parsed) > 4 and isinstance(parsed[4], str) else ""


class RequestCaptureService:
    def __init__(self, session: BrowserSession, snapshot_cache: SnapshotCache):
        self._session = session
        self._snapshot_cache = snapshot_cache

    async def capture(
        self,
        prompt: str,
        model: str = DEFAULT_TEXT_MODEL,
        images: list[str] | None = None,
    ) -> CapturedRequest | None:
        if not images:
            cached = self._snapshot_cache.get(prompt)
            if cached:
                _snapshot, url, headers, body = cached
                return CapturedRequest(url=url, headers=headers, body=body)

        ctx = await self._session.ensure_context()
        page = await ctx.new_page()
        captured = {}

        async def on_route(route, request):
            if "GenerateContent" in request.url and request.method == "POST" and not captured:
                body = request.post_data
                if body and len(body) > 100:
                    captured["url"] = request.url
                    captured["headers"] = dict(request.headers)
                    captured["body"] = body
            await route.continue_()

        try:
            await page.route("**/*", on_route)
            await page.goto(
                "https://aistudio.google.com/app/prompts/new_chat",
                wait_until="networkidle",
                timeout=30000,
            )

            await self._switch_model(page, model)
            if images:
                await self._upload_images(page, images)

            textarea = page.locator("textarea").first
            await textarea.fill(prompt)
            await asyncio.sleep(0.5)
            run_btn = page.locator("button", has_text="Run").first
            await run_btn.click()

            timeout = 60 if "image" in model.lower() else 15
            for _ in range(timeout):
                await asyncio.sleep(1)
                if captured:
                    break

            if not captured:
                return None

            result = CapturedRequest(**captured)
            logger.info(
                "拦截成功: model=%s, snapshot=%s chars, body=%s chars",
                result.model,
                len(result.snapshot),
                len(captured["body"]),
            )
            if not images:
                self._snapshot_cache.put(prompt, result.snapshot, result.url, result.headers, result.body)
            return result
        finally:
            await page.close()

    async def _switch_model(self, page, model: str):
        target_model_name = model.replace("models/", "")
        model_selector = page.locator("text=gemini-2.5-flash").first
        if not await model_selector.is_visible(timeout=3000):
            return

        await model_selector.click()
        await asyncio.sleep(1)
        target_model = page.locator(f"text={target_model_name}").first
        if not await target_model.is_visible(timeout=2000):
            for cat in ["Gemma", "PRO", "FLASH"]:
                cat_el = page.locator(f"text={cat}").first
                if await cat_el.is_visible(timeout=1000):
                    await cat_el.click()
                    await asyncio.sleep(0.5)
                    if await target_model.is_visible(timeout=1000):
                        break

        if await target_model.is_visible(timeout=2000):
            await target_model.click()
            await asyncio.sleep(1)
            logger.info("已切换模型: %s", model)
            return

        logger.warning("找不到模型 %s，使用默认", target_model_name)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

    async def _upload_images(self, page, images: list[str]):
        for img_path in images:
            upload_btn = page.locator('[aria-label="Insert images, videos, audio, or files"]').first
            if not await upload_btn.is_visible(timeout=3000):
                continue

            await upload_btn.click()
            await asyncio.sleep(1)
            upload_files_btn = page.locator("button", has_text="Upload files").first
            if not await upload_files_btn.is_visible(timeout=3000):
                continue

            try:
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await upload_files_btn.click()
                fc = await fc_info.value
                await fc.set_files(img_path)
            except Exception:
                ack = page.locator("button", has_text="Acknowledge").first
                if await ack.is_visible(timeout=2000):
                    await ack.click()
                    await asyncio.sleep(1)
                    await upload_btn.click()
                    await asyncio.sleep(1)
                    upload_files_btn = page.locator("button", has_text="Upload files").first
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await upload_files_btn.click()
                    fc = await fc_info.value
                    await fc.set_files(img_path)

            logger.info("已上传图片: %s", img_path)
            await asyncio.sleep(2)

