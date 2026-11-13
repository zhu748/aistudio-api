"""app 探活客户端池的并发安全回归测试。

历史缺陷（B2）：``_get_probe_client`` 的「检查-构造-赋值」写法把
``await _resolve_probe_proxy(...)`` 夹在中间——turn/sstp 桥接冷启动是
真实让出点，并发首调各自构造 AsyncClient、后赋值者胜出，其余实例脱离
pool 无人关闭（连接泄漏给 GC）。修复后的 ``_pooled_probe_client`` 用
setdefault 仲裁入池、竞争中后到者立即自关；本文件锁死该行为。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import aistudio_api.infrastructure.proxy_bridge as pb
from aistudio_api.api.app import _pooled_probe_client


async def test_concurrent_probe_pool_creates_single_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发首调同一代理：全部拿到同一实例，竞争中后到者自关（无泄漏）。"""
    created: list[httpx.AsyncClient] = []
    real_init = httpx.AsyncClient.__init__

    def tracking_init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", tracking_init)

    async def slow_resolve(raw: str | None) -> str | None:
        await asyncio.sleep(0)  # 强制让出：模拟桥接冷启动窗口
        assert raw == "turn://u:p@1.2.3.4:3478"
        return "socks5://127.0.0.1:40000"

    monkeypatch.setattr(pb, "resolve_effective_proxy_async", slow_resolve)

    pool: dict[str, httpx.AsyncClient] = {}
    results = await asyncio.gather(
        *(_pooled_probe_client(pool, "turn://u:p@1.2.3.4:3478") for _ in range(6))
    )

    # 6 路并发全部收敛到同一实例；池内唯一
    assert len({id(client) for client in results}) == 1
    assert len(pool) == 1
    # 竞争窗口内各自构造了 6 个，但除胜者外全部已自关（修复前为泄漏）
    assert len(created) == 6
    assert sum(1 for client in created if not client.is_closed) == 1

    for client in pool.values():
        await client.aclose()
    assert sum(1 for client in created if not client.is_closed) == 0


async def test_pooled_probe_client_reuses_cached_instance() -> None:
    """缓存命中零副作用：同一代理的重复取用不重复解析、不重复建池。"""
    pool: dict[str, httpx.AsyncClient] = {}
    first = await _pooled_probe_client(pool, "http://127.0.0.1:7890")
    second = await _pooled_probe_client(pool, "http://127.0.0.1:7890")
    assert first is second
    assert len(pool) == 1
    await first.aclose()


async def test_pooled_probe_client_separates_proxy_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同代理地址各自分池：绑定独立代理的账号各走各的出口。"""

    async def instant_resolve(raw: str | None) -> str | None:
        return raw

    monkeypatch.setattr(pb, "resolve_effective_proxy_async", instant_resolve)

    pool: dict[str, httpx.AsyncClient] = {}
    a = await _pooled_probe_client(pool, "http://10.0.0.1:8080")
    b = await _pooled_probe_client(pool, "socks5://10.0.0.2:1080")
    assert a is not b
    assert len(pool) == 2
    for client in (a, b):
        await client.aclose()
