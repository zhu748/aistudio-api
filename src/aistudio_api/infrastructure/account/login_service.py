"""Google 账号登录服务，通过有头浏览器完成登录并保存 cookie。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aistudio_api.infrastructure.browser.camoufox_manager import CamoufoxManager

logger = logging.getLogger("aistudio.login")


class LoginStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class LoginSession:
    """登录会话状态。"""
    session_id: str
    status: LoginStatus = LoginStatus.PENDING
    account_id: str | None = None
    email: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LoginService:
    """Google 账号登录服务。"""

    def __init__(self, port: int = 9223) -> None:
        self._port = port
        self._sessions: dict[str, LoginSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _generate_session_id(self) -> str:
        return f"login_{secrets.token_hex(8)}"

    async def start_login(
        self,
        account_store: Any,  # AccountStore
        name: str | None = None,
    ) -> str:
        """启动登录流程，返回 session_id。"""
        session_id = self._generate_session_id()
        session = LoginSession(session_id=session_id)
        self._sessions[session_id] = session
        # 启动后台任务
        task = asyncio.create_task(
            self._login_worker(session_id, account_store, name)
        )
        self._tasks[session_id] = task
        return session_id

    def get_status(self, session_id: str) -> LoginSession | None:
        """获取登录状态。"""
        return self._sessions.get(session_id)

    async def _login_worker(
        self,
        session_id: str,
        account_store: Any,
        name: str | None,
    ) -> None:
        """登录工作协程。"""
        session = self._sessions[session_id]
        manager = CamoufoxManager(
            port=self._port,
            headless=False,  # 有头模式，用户需要看到浏览器
        )
        playwright = None
        browser = None
        try:
            # 启动浏览器
            logger.info("启动登录浏览器，端口 %d", self._port)
            ws_endpoint = await manager.start()
            logger.info("浏览器已启动: %s", ws_endpoint)

            # 连接 Playwright
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
            browser = await playwright.firefox.connect(ws_endpoint)
            context = await browser.new_context()
            page = await context.new_page()

            # 设置登录完成检测
            login_done = asyncio.Event()
            detected_email: str | None = None

            async def on_navigation(frame):
                nonlocal detected_email
                url = frame.url
                logger.debug("导航到: %s", url)
                # 检测登录完成：跳转到非登录页面
                if "accounts.google.com" not in url and "google.com" in url:
                    # 尝试提取邮箱
                    try:
                        detected_email = await page.evaluate("""
                            () => {
                                // 尝试从页面获取邮箱
                                const el = document.querySelector('[data-email]')
                                    || document.querySelector('.gb_nb')
                                    || document.querySelector('[aria-label*="@"]');
                                return el ? (el.getAttribute('data-email') || el.textContent.trim()) : null;
                            }
                        """)
                    except Exception:
                        pass
                    login_done.set()

            page.on("framenavigated", on_navigation)

            # 导航到 Google 登录页面
            logger.info("打开 Google 登录页面")
            await page.goto(
                "https://accounts.google.com/ServiceLogin?continue=https://aistudio.google.com",
                wait_until="networkidle",
            )

            # 等待用户完成登录（最多 5 分钟）
            logger.info("等待用户登录...")
            try:
                await asyncio.wait_for(login_done.wait(), timeout=300)
            except asyncio.TimeoutError:
                session.status = LoginStatus.FAILED
                session.error = "登录超时（5 分钟）"
                logger.warning("登录超时")
                return

            # 登录完成，保存 storage state
            logger.info("登录完成，保存 cookie")
            storage_state = await context.storage_state()

            # 尝试从 Google 账号页面获取邮箱
            if detected_email is None:
                try:
                    # 导航到 Google 账号页面
                    logger.info("尝试从 Google 账号页面获取邮箱")
                    await page.goto("https://myaccount.google.com", wait_until="networkidle")
                    await asyncio.sleep(2)  # 等待页面加载

                    # 从页面提取邮箱（优先匹配 *@gmail.com）
                    detected_email = await page.evaluate("""
                        () => {
                            const text = document.body.innerText;
                            // 直接匹配 *@gmail.com 邮箱
                            const gmailRegex = /[a-zA-Z0-9._%+-]+@gmail\\.com/g;
                            const matches = text.match(gmailRegex);
                            return matches ? matches[0] : null;
                        }
                    """)
                except Exception as e:
                    logger.warning("从 Google 账号页面获取邮箱失败: %s", e)

            # 如果还是没提取到邮箱，尝试从 storage state 的 origins 中提取
            if detected_email is None:
                try:
                    # 检查 localStorage 中是否有用户信息
                    for origin in storage_state.get("origins", []):
                        for item in origin.get("localStorage", []):
                            if "email" in item.get("name", "").lower():
                                detected_email = item.get("value")
                                break
                        if detected_email:
                            break
                except Exception:
                    pass

            # 保存账号
            account_name = name or detected_email or "Google 账号"
            if detected_email and not name:
                account_name = detected_email
            meta = account_store.save_account(
                name=account_name,
                email=detected_email,
                storage_state=storage_state,
            )

            session.status = LoginStatus.COMPLETED
            session.account_id = meta.id
            session.email = detected_email
            logger.info("账号已保存: %s (%s)", meta.id, detected_email)

        except Exception as e:
            session.status = LoginStatus.FAILED
            session.error = str(e)
            logger.exception("登录失败")
        finally:
            # 清理浏览器和 Playwright
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if playwright:
                    await playwright.stop()
            except Exception:
                pass
            try:
                await manager.stop()
            except Exception:
                pass
            # 清理任务引用
            self._tasks.pop(session_id, None)
