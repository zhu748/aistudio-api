"""Shared Camoufox session management for gateway operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any

from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

AI_STUDIO_URL = "https://aistudio.google.com/prompts/new_chat"
AI_STUDIO_URL_FALLBACK = "https://aistudio.google.com/app/prompts/new_chat"
INSTALL_HOOKS_JS = r"""
mw:((() => {
    if (window.__bg_hooked) return 'already_hooked';
    const dms = window.default_MakerSuite;
    if (!dms) return 'no_default_MakerSuite';

    // Auto-detect snapshot function via feature matching
    let snapKey = null;
    for (const k of Object.keys(dms)) {
        try {
            if (typeof dms[k] !== 'function') continue;
            const src = dms[k].toString();
            if (src.includes('.snapshot({') && src.includes('content') && src.includes('yield')) {
                snapKey = k;
                break;
            }
        } catch(e) {}
    }
    if (!snapKey) return 'no_snapshot_fn';

    // Hook snapshot function to capture service
    const origSnap = dms[snapKey];
    dms[snapKey] = function(...args) {
        window.__bg_service = args[0];
        const result = origSnap.apply(this, args);
        if (result instanceof Promise) return result.then(s => { window.__bg_snapshot = s; return s; });
        window.__bg_snapshot = result;
        return result;
    };

    // XHR hook for body replacement
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...args) {
        this.__url = url;
        this.__is_gen = url.includes('GenerateContent') && !url.includes('CountTokens');
        window.__last_hook_url = url;
        return origOpen.call(this, method, url, ...args);
    };
    XMLHttpRequest.prototype.send = function(body) {
        if (this.__is_gen && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = this.__url || '';
            return origSend.call(this, captured);
        }
        return origSend.call(this, body);
    };

    window.__bg_hooked = true;
    window.__snap_key = snapKey;
    return 'hooked:' + snapKey;
})())
"""

DIALOG_CLEANUP_JS = """(() => {
    document.querySelectorAll('button').forEach((button) => {
        const text = (button.textContent || '').trim().toLowerCase();
        if (['dismiss', 'close', 'accept', 'ok', 'agree', 'got it'].includes(text)) {
            button.click();
        }
    });
    document.querySelectorAll('.cdk-overlay-backdrop').forEach((node) => node.remove());
    document.querySelectorAll('.cdk-overlay-container').forEach((node) => node.remove());
})()"""


class BrowserSession:
    def __init__(self, port: int):
        self.port = port
        self._auth_file = settings.auth_file
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._snap_key: str | None = None
        self._templates: dict[str, dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aistudio-camoufox")
        self._lock = asyncio.Lock()

    async def ensure_context(self):
        return await self._run_sync(self._ensure_browser_sync)

    async def switch_auth(self, auth_file: str | None) -> None:
        await self._run_sync(self._switch_auth_sync, auth_file)

    async def ensure_hook_page(self):
        await self._run_sync(self._ensure_hook_page_sync)
        return True

    async def ensure_botguard_service(self):
        await self._run_sync(self._ensure_botguard_service_sync)
        return True

    async def capture_template(self, model: str) -> dict[str, Any]:
        return await self._run_sync(self._capture_template_sync, model)

    async def upload_images(self, image_paths: list[str]) -> list[str]:
        return await self._run_sync(self._upload_images_sync, image_paths)

    async def generate_snapshot(self, contents: list[AistudioContent]) -> str:
        return await self._run_sync(self._generate_snapshot_sync, contents)

    async def send_hooked_request(self, *, body: str, timeout_ms: int) -> tuple[int, bytes]:
        return await self._run_sync(self._send_hooked_request_sync, body, timeout_ms)

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(self._executor, lambda: func(*args))

    def _switch_auth_sync(self, auth_file: str | None) -> None:
        self._auth_file = auth_file
        self._templates.clear()
        self._close_sync()

    def _ensure_browser_sync(self):
        if self._ctx is not None and self._hook_page is not None and not self._hook_page.is_closed():
            return self._ctx

        from camoufox.sync_api import Camoufox

        self._close_sync()
        self._cf = Camoufox(headless=settings.camoufox_headless, main_world_eval=True)
        self._browser = self._cf.__enter__()
        self._ctx = self._new_context_sync()
        self._hook_page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._goto_aistudio_sync(self._hook_page)
        self._install_hooks_sync(self._hook_page)
        return self._ctx

    def _new_context_sync(self):
        if self._auth_file and Path(self._auth_file).exists():
            try:
                return self._browser.new_context(storage_state=self._auth_file)
            except Exception:
                ctx = self._browser.new_context()
                self._apply_storage_state_sync(ctx, self._auth_file)
                return ctx
        return self._browser.new_context()

    def _apply_storage_state_sync(self, ctx, auth_file: str) -> None:
        data = json.loads(Path(auth_file).read_text())
        cookies = data.get("cookies") or []
        if cookies:
            ctx.add_cookies(cookies)

    def _ensure_hook_page_sync(self):
        self._ensure_browser_sync()
        if "aistudio.google.com" not in (self._hook_page.url or ""):
            self._goto_aistudio_sync(self._hook_page)
        self._install_hooks_sync(self._hook_page)
        return self._hook_page

    def _ensure_botguard_service_sync(self):
        page = self._ensure_hook_page_sync()
        if page.evaluate("mw:!!window.__bg_service"):
            self._wait_until_idle_sync(page)
            return page

        page.evaluate(DIALOG_CLEANUP_JS)
        textarea = page.query_selector("textarea")
        if textarea is None:
            raise RuntimeError("textarea not found while capturing BotGuardService")
        textarea.fill("1")
        page.wait_for_timeout(800)
        page.evaluate(DIALOG_CLEANUP_JS)
        if not self._click_run_button_sync(page):
            raise RuntimeError("failed to trigger send while capturing BotGuardService")

        for _ in range(45):
            page.wait_for_timeout(1000)
            if page.evaluate("mw:!!window.__bg_service"):
                self._wait_until_idle_sync(page)
                return page

        raise RuntimeError("BotGuardService capture timeout")

    def _capture_template_sync(self, model: str) -> dict[str, Any]:
        if model in self._templates:
            return self._templates[model]

        page = self._ensure_botguard_service_sync()
        captured: dict[str, Any] = {}

        def on_response(response):
            if "GenerateContent" not in response.url or "Count" in response.url or captured:
                return
            try:
                text = response.text()
            except Exception:
                return
            if len(text) <= 100:
                return
            req = response.request
            body = req.post_data
            if not body or len(body) <= 100:
                return
            captured["url"] = req.url
            captured["headers"] = dict(req.headers)
            captured["body"] = body

        page.on("response", on_response)
        try:
            textarea = page.query_selector("textarea")
            if textarea is None:
                raise RuntimeError("textarea not found during template capture")
            textarea.fill("template")
            page.wait_for_timeout(500)
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send during template capture")

            for _ in range(30):
                page.wait_for_timeout(1000)
                if captured:
                    break
            if not captured:
                raise RuntimeError(f"template capture timeout for model={model}")

            self._wait_until_idle_sync(page)
            self._templates[model] = captured
            return captured
        finally:
            page.remove_listener("response", on_response)

    def _generate_snapshot_sync(self, contents: list[AistudioContent]) -> str:
        page = self._ensure_botguard_service_sync()
        if not self._snap_key:
            raise RuntimeError("Snapshot function not detected")

        # 计算 content hash（包含图片数据，与 camoufox-api 一致）
        hash_parts: list[str] = []
        for content in contents:
            for part in content.parts:
                if part.inline_data:
                    hash_parts.append(part.inline_data[1])  # base64 data
                if part.text:
                    hash_parts.append(str(part.text))
        content_hash = sha256(" ".join(hash_parts).encode("utf-8")).hexdigest()

        page.evaluate(
            """
mw:((hash) => {
    const dms = window.default_MakerSuite;
    const service = window.__bg_service;
    const snapKey = window.__snap_key;
    if (!dms || !service || !snapKey || typeof dms[snapKey] !== 'function') {
        window.__sr = '';
        window.__sl = 0;
        window.__snap_error = 'service_unavailable';
        return;
    }
    window.__sr = '';
    window.__sl = 0;
    window.__snap_error = '';
    const result = dms[snapKey](service, hash);
    if (result instanceof Promise) {
        result.then((snapshot) => {
            window.__sr = snapshot || '';
            window.__sl = snapshot ? snapshot.length : 0;
        }).catch((error) => {
            window.__snap_error = String(error);
        });
        return;
    }
    window.__sr = result || '';
    window.__sl = result ? result.length : 0;
})(%s)
"""
            % json.dumps(content_hash)
        )
        for _ in range(20):
            if page.evaluate("mw:(window.__sl || 0)") > 0:
                break
            page.wait_for_timeout(500)

        snapshot = page.evaluate("mw:window.__sr")
        if snapshot:
            return snapshot
        error = page.evaluate("mw:window.__snap_error || ''")
        raise RuntimeError(f"Snapshot generation failed: {error or 'unknown'}")

    def _upload_images_sync(self, image_paths: list[str]) -> list[str]:
        if not image_paths:
            return []

        # 尝试非 UI 方式上传（更快、更可靠）
        # 需要在主线程中获取 cookies，因为 Playwright 的同步 API 有 greenlet 限制
        try:
            if self._ctx is not None:
                cookies = self._ctx.cookies()
                return self._upload_images_via_api_sync(image_paths, cookies)
        except Exception as e:
            # 如果非 UI 方式失败，回退到 UI 方式
            import logging
            logging.getLogger("aistudio").debug("Non-UI upload failed, falling back to UI: %s", e)
            pass

        # UI 方式上传（原有逻辑）
        page = self._ensure_botguard_service_sync()
        self._wait_until_idle_sync(page)
        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            for image_path in image_paths:
                target_count = len(uploaded_ids) + 1
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_btn = page.locator('[aria-label="Insert images, videos, audio, or files"]').first
                if not upload_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload button not visible")
                upload_btn.click()
                page.wait_for_timeout(1500)
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    upload_btn.click()
                    page.wait_for_timeout(1000)
                    upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload files button not visible")
                with page.expect_file_chooser(timeout=10000) as chooser_info:
                    upload_files_btn.click()
                chooser_info.value.set_files(image_path)

                deadline = time.time() + 30
                while time.time() < deadline:
                    if len(uploaded_ids) >= target_count:
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(1500)
        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _upload_images_via_api_sync(self, image_paths: list[str], cookies: list[dict]) -> list[str]:
        """通过 Playwright 的 setInputFiles 方法上传图片（非 UI 点击方式）"""
        page = self._hook_page
        if page is None:
            raise RuntimeError("Hook page not initialized")

        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            # 找到文件输入元素（如果有的话）
            file_input = page.query_selector('input[type="file"]')

            if file_input:
                # 直接使用 setInputFiles 方法上传
                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)
            else:
                # 如果没有 file input，尝试创建一个
                page.evaluate("""
                    () => {
                        const input = document.createElement('input');
                        input.type = 'file';
                        input.id = '__api_file_input__';
                        input.style.display = 'none';
                        input.accept = 'image/*';
                        document.body.appendChild(input);

                        // 监听文件选择事件
                        input.addEventListener('change', (e) => {
                            const file = e.target.files[0];
                            if (file) {
                                // 触发上传逻辑
                                window.__api_upload_file = file;
                            }
                        });
                    }
                """)

                file_input = page.query_selector('#__api_file_input__')
                if not file_input:
                    raise RuntimeError("Failed to create file input")

                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)
                    page.wait_for_timeout(1000)

                    # 触发上传
                    page.evaluate("""
                        () => {
                            if (window.__api_upload_file) {
                                // 模拟拖放或触发上传按钮
                                const event = new Event('change', { bubbles: true });
                                const input = document.querySelector('#__api_file_input__');
                                if (input) input.dispatchEvent(event);
                            }
                        }
                    """)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)

        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _send_hooked_request_sync(self, body: str, timeout_ms: int) -> tuple[int, bytes]:
        page = self._ensure_botguard_service_sync()
        self._wait_until_idle_sync(page)
        route_hits: list[str] = []

        def route_handler(route):
            request = route.request
            if "GenerateContent" in request.url and "CountTokens" not in request.url:
                route_hits.append(request.url)
                route.continue_(post_data=body)
                return
            route.continue_()

        page.route("**/*GenerateContent*", route_handler)
        page.evaluate(
            """(payload) => {
                window.__pending_body = payload;
                window.__hooked = false;
                window.__last_hook_url = '';
            }""",
            body,
        )
        page.evaluate(DIALOG_CLEANUP_JS)
        textarea = page.query_selector("textarea")
        if textarea is None:
            raise RuntimeError("textarea not found before hooked send")
        textarea.fill("__api_trigger__")
        page.wait_for_timeout(500)
        page.evaluate(DIALOG_CLEANUP_JS)
        responses: list[tuple[int, str]] = []
        failures: list[dict[str, str]] = []

        def on_response(response):
            if "GenerateContent" in response.url and "Count" not in response.url:
                try:
                    text = response.text()
                except Exception:
                    return
                responses.append((response.status, text))

        def on_failed(request):
            if "GenerateContent" in request.url and "Count" not in request.url:
                failures.append(
                    {
                        "url": request.url,
                        "failure": str(request.failure),
                    }
                )

        page.on("response", on_response)
        page.on("requestfailed", on_failed)
        try:
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send before hooked request")
            deadline = timeout_ms / 1000
            elapsed = 0.0
            while elapsed < deadline:
                state = page.evaluate(
                    """() => ({
                        hooked: !!window.__hooked,
                        ready: document.body.innerText.includes('Response ready.'),
                    })"""
                )
                if state["ready"] and responses:
                    break
                if failures:
                    break
                page.wait_for_timeout(500)
                elapsed += 0.5
        finally:
            page.remove_listener("response", on_response)
            page.remove_listener("requestfailed", on_failed)
            page.unroute("**/*GenerateContent*", route_handler)

        hook_debug = page.evaluate(
            """() => ({
                hooked: !!window.__hooked,
                pending_exists: !!window.__pending_body,
                pending_len: window.__pending_body ? window.__pending_body.length : 0,
                textarea: document.querySelector('textarea')?.value || '',
                last_url: window.__last_hook_url || '',
            })"""
        )
        hook_debug["failures"] = failures
        if not responses:
            raise RuntimeError(f"No response captured; debug={hook_debug}")
        status, raw_text = responses[-1]
        if not hook_debug.get("hooked") and not route_hits:
            raise RuntimeError(f"Hook did not intercept request; debug={hook_debug}")
        return status, raw_text.encode("utf-8")

    def _goto_aistudio_sync(self, page) -> None:
        last_exc = None
        for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK):
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(10000)
                return
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc

    def _install_hooks_sync(self, page) -> None:
        result = page.evaluate(INSTALL_HOOKS_JS)
        if result == "already_hooked":
            return
        if isinstance(result, str) and result.startswith("hooked:"):
            self._snap_key = result.split(":", 1)[1]
            return
        for _ in range(3):
            page.wait_for_timeout(2000)
            result = page.evaluate(INSTALL_HOOKS_JS)
            if result == "already_hooked":
                return
            if isinstance(result, str) and result.startswith("hooked:"):
                self._snap_key = result.split(":", 1)[1]
                return
        raise RuntimeError(f"Hook install failed: {result}")

    def _click_run_button_sync(self, page) -> bool:
        try:
            button = page.query_selector("button:has-text('Run')")
        except Exception:
            return False
        if button is None:
            return False
        try:
            button.click()
            return True
        except Exception:
            return False

    def _has_run_button_sync(self, page) -> bool:
        try:
            return page.query_selector("button:has-text('Run')") is not None
        except Exception:
            return False

    def _wait_until_idle_sync(self, page) -> None:
        for _ in range(60):
            if self._has_run_button_sync(page):
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("page never became idle")

    def _close_sync(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._cf is not None:
            try:
                self._cf.__exit__(None, None, None)
            except Exception:
                pass
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._snap_key = None
