"""media_session.get_client 锁外解析回归测试。

历史缺陷：resolve_proxy_url_async 夹在 asyncio.Lock 临界区内——
turn/sstp 桥接冷启动是秒级真实 await 让出点，并发首图下载会在
锁上串行排队（第二个协程要等第一个的桥接子进程拉起完才开始）。
修复后解析在锁外、双重检查入池，并发首调全部并行解析、单客户端收敛。
"""

import asyncio

import httpx
import pytest

from aistudio_api.application import media_session as ms


async def test_proxy_resolution_runs_outside_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁内不得出现 await 解析：并发首调应并行解析。"""
    resolve_calls: list[float] = []
    resolved_at: dict[asyncio.Task, float] = {}
    started = asyncio.Event()

    async def fake_resolve(raw: str | None) -> str | None:
        # 第一调用先挂起，直到第二个调用也进入解析——锁内串行时第二个
        # 永远到不了这里（会卡在 lock.acquire），测试超时失败。
        resolve_calls.append(1)
        if len(resolve_calls) == 1:
            started.set()
            await asyncio.sleep(0.2)
        resolved_at[asyncio.current_task()] = asyncio.get_event_loop().time()
        return "socks5://127.0.0.1:14000"

    monkeypatch.setattr(ms, "resolve_proxy_url_async", fake_resolve)

    session = ms.MediaSession()

    async def fetch() -> httpx.AsyncClient:
        return await session.get_client()

    c1, c2 = await asyncio.gather(fetch(), fetch())
    # 并发收敛到同一客户端实例
    assert c1 is c2
    assert session.client is c1
    # 两个协程都执行了解析（证明第二个没被锁挡在解析之外）
    assert len(resolve_calls) == 2
    await c1.aclose()


async def test_client_created_once_across_concurrent_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6 路并发首调：仅建一个客户端，全部拿到同一实例。"""
    created: list[httpx.AsyncClient] = []
    real_init = httpx.AsyncClient.__init__

    def tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", tracking_init)

    async def fast_resolve(raw: str | None) -> str | None:
        await asyncio.sleep(0)  # 强制让出，模拟冷启动窗口
        return None

    monkeypatch.setattr(ms, "resolve_proxy_url_async", fast_resolve)
    session = ms.MediaSession()
    clients = await asyncio.gather(*(session.get_client() for _ in range(6)))
    assert len({id(c) for c in clients}) == 1
    assert len(created) == 1
    await clients[0].aclose()
