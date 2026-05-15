"""Shared helpers for selecting and launching the browser backend."""

from __future__ import annotations

from typing import Any

from aistudio_api.config import build_camoufox_proxy, settings


def is_camoufox_engine() -> bool:
    return settings.browser_engine == "camoufox"


def describe_browser_backend() -> str:
    if is_camoufox_engine():
        return "camoufox"
    if settings.browser_channel:
        return f"chromium:{settings.browser_channel}"
    if settings.browser_executable_path:
        return f"chromium:{settings.browser_executable_path}"
    return "chromium"


def build_browser_launch_options(headless: bool | None = None) -> dict[str, Any]:
    is_headless = settings.browser_headless if headless is None else headless
    options: dict[str, Any] = {
        "headless": is_headless,
    }
    if not is_headless:
        options["args"] = ["--start-maximized"]
    proxy = build_camoufox_proxy(settings.proxy_url)
    if proxy:
        options["proxy"] = proxy
    if settings.browser_executable_path:
        options["executable_path"] = settings.browser_executable_path
    elif settings.browser_channel:
        options["channel"] = settings.browser_channel
    return options


def build_browser_context_options(headless: bool | None = None) -> dict[str, Any]:
    if is_camoufox_engine():
        return {}

    is_headless = settings.browser_headless if headless is None else headless
    if is_headless:
        return {}

    return {
        "no_viewport": True,
    }


def should_maximize_browser_window(headless: bool | None = None) -> bool:
    if is_camoufox_engine():
        return False
    return not (settings.browser_headless if headless is None else headless)


def sync_maximize_page_window(page: Any, *, headless: bool | None = None) -> None:
    if not should_maximize_browser_window(headless):
        return
    try:
        cdp = page.context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")
        cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {"windowState": "maximized"},
            },
        )
        page.wait_for_timeout(200)
    except Exception:
        pass


async def async_maximize_page_window(page: Any, *, headless: bool | None = None) -> None:
    if not should_maximize_browser_window(headless):
        return
    try:
        cdp = await page.context.new_cdp_session(page)
        window = await cdp.send("Browser.getWindowForTarget")
        await cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {"windowState": "maximized"},
            },
        )
        await page.wait_for_timeout(200)
    except Exception:
        pass


def sync_launch_browser() -> tuple[Any, Any | None, Any | None]:
    """Launch a sync browser session.

    Returns:
        tuple of (browser, camoufox_context_manager, playwright_instance)
    """
    if is_camoufox_engine():
        from camoufox.sync_api import Camoufox

        cf = Camoufox(
            headless=settings.browser_headless,
            main_world_eval=True,
            proxy=build_camoufox_proxy(settings.proxy_url),
        )
        browser = cf.__enter__()
        return browser, cf, None

    from cloakbrowser import launch

    browser = launch(
        headless=settings.browser_headless,
        proxy=build_camoufox_proxy(settings.proxy_url),
    )
    return browser, None, None


async def async_launch_browser(*, headless: bool | None = None) -> Any:
    """Launch an async browser session via cloakbrowser."""
    if is_camoufox_engine():
        raise RuntimeError("async_launch_browser() only supports Chromium backend")
    from cloakbrowser import launch_async

    return await launch_async(
        headless=settings.browser_headless if headless is None else headless,
        proxy=build_camoufox_proxy(settings.proxy_url),
    )
