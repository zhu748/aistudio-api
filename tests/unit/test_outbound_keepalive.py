"""出站保活服务（infrastructure/outbound_keepalive.py）单元测试。

覆盖：URL 校验（scheme/主机/凭据/元数据与私网裁决）、启停幂等、
未配置零启动、间隔下限拒绝、私网默认拒绝与显式放行、异常轮次
不终止任务、重定向复检、lifespan 接线冒烟。

lifespan 冒烟不走 TestClient（完整 app 会拉起后台任务、关停
bridge_manager 等全局单例污染其他测试），改为直接调用
``app.lifespan`` 上下文管理器并在 finally 中恢复全局状态
（bridge_manager.reopen），与 TestClient 等价但不经过 anyio
portal（残留 _save_task 跨循环 await 会把 CancelledError 抛进
无关测试）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest

from aistudio_api.config import settings
from aistudio_api.infrastructure.outbound_keepalive import (
    OutboundKeepAliveService,
    keepalive_private_targets_allowed,
    validate_keepalive_url,
)


@pytest.fixture
def no_keepalive_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """默认隔离：测试环境里不应启动任何保活任务。"""
    monkeypatch.setattr(settings, "keepalive_url", "")
    monkeypatch.setattr(settings, "keepalive_interval", 60)
    monkeypatch.setattr(settings, "keepalive_allow_private", False)
    yield


class TestValidateUrl:
    @pytest.mark.usefixtures("no_keepalive_url")
    def test_empty_is_valid(self) -> None:
        # 留空 = 关闭保活，视为合法配置。
        assert validate_keepalive_url("") is True

    @pytest.mark.usefixtures("no_keepalive_url")
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/healthz",
            "http://example.com",
            "https://app-1.onrender.com/healthz?probe=1#frag",
            "http://8.8.8.8:8080/healthz",  # 公网单播地址
        ],
    )
    def test_public_targets_valid(self, url: str) -> None:
        assert validate_keepalive_url(url) is True

    @pytest.mark.usefixtures("no_keepalive_url")
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/healthz",
            "file:///etc/passwd",
            "example.com/healthz",
            "https://",
            "https:///healthz",
            "https://user:pass@example.com/healthz",  # 内嵌凭据：日志泄露面
            "https://example.com:0/healthz",
            "https://example.com:70000/healthz",
            "https://exa mple.com/healthz",
            "https://example.com/health\u0000z",
            "https://example.com/health\x7f",
        ],
    )
    def test_invalid_targets_rejected(self, url: str) -> None:
        assert validate_keepalive_url(url) is False

    @pytest.mark.usefixtures("no_keepalive_url")
    @pytest.mark.parametrize(
        "host",
        [
            "169.254.169.254",  # 链路本地（云元数据端点）
            "metadata.google.internal",  # 域名型元数据端点
            "metadata.goog",
            "224.0.0.1",  # 组播
            "240.1.2.3",  # 保留段
            "0.0.0.0",  # 未指定地址
        ],
    )
    def test_metadata_and_special_segments_always_rejected(self, host: str) -> None:
        # 无论 allow_private 与否，元数据/链路本地/组播/保留段都不是合法目标。
        assert validate_keepalive_url(f"https://{host}/healthz") is False
        assert (
            validate_keepalive_url(f"https://{host}/healthz", allow_private=True)
            is False
        )

    @pytest.mark.usefixtures("no_keepalive_url")
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.9"]
    )
    def test_private_and_loopback_require_opt_in(self, host: str) -> None:
        url = f"http://{host}:8080/healthz"
        # 默认拒绝：防被劫持的管理会话借保活任务探测内网。
        assert validate_keepalive_url(url) is False
        # 显式开启 allow_private 后放行（自我保活/内网监控场景）。
        assert validate_keepalive_url(url, allow_private=True) is True

    @pytest.mark.usefixtures("no_keepalive_url")
    def test_allow_private_helper_reads_settings(self) -> None:
        assert keepalive_private_targets_allowed() is False
        settings.keepalive_allow_private = True
        try:
            assert keepalive_private_targets_allowed() is True
        finally:
            settings.keepalive_allow_private = False


class TestServiceLifecycle:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_not_started_without_url(self) -> None:
        svc = OutboundKeepAliveService()
        await svc.start()
        assert svc.is_running is False
        # 停止未启动的服务是幂等空操作。
        await svc.stop()
        assert svc.is_running is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_invalid_url_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "keepalive_url", "not a url")
        svc = OutboundKeepAliveService()
        await svc.start()
        assert svc.is_running is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_private_target_not_started_without_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "keepalive_url", "http://127.0.0.1:9999/healthz")
        svc = OutboundKeepAliveService()
        await svc.start()
        assert svc.is_running is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_private_target_started_with_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "keepalive_url", "http://127.0.0.1:9/healthz")
        monkeypatch.setattr(settings, "keepalive_allow_private", True)
        svc = OutboundKeepAliveService()
        await svc.start()
        try:
            assert svc.is_running is True
        finally:
            await svc.stop()
        assert svc.is_running is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_interval_below_floor_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "keepalive_url", "https://example.com/healthz")
        monkeypatch.setattr(settings, "keepalive_interval", 3)
        svc = OutboundKeepAliveService()
        await svc.start()
        assert svc.is_running is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_start_is_idempotent_and_restart_replaces_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "keepalive_url", "https://example.com/healthz")
        monkeypatch.setattr(settings, "keepalive_interval", 300)
        svc = OutboundKeepAliveService()
        await svc.start()
        assert svc.is_running is True
        first = svc._task
        # 重复 start 不换任务。
        await svc.start()
        assert svc._task is first
        # restart 走 stop + start，任务实例被替换。
        await svc.restart()
        assert svc._task is not None and svc._task is not first
        await svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_failed_round_does_not_kill_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单轮请求失败只记日志，任务在下一轮照常运行（连不上的端口）。"""
        monkeypatch.setattr(settings, "keepalive_url", "https://example.invalid/hz")
        monkeypatch.setattr(settings, "keepalive_interval", 5)
        svc = OutboundKeepAliveService()
        await svc.start()
        try:
            assert svc.is_running is True
            # 给失败的一轮留出时间（远超单请求超时上限的一小段）。
            for _ in range(12):
                await asyncio.sleep(0.25)
                if svc._task is not None and svc._task.done():
                    break
            # 任务必须仍在运行：失败轮不终止服务。
            assert svc.is_running is True
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_redirect_to_private_target_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重定向链复检：公网目标 302 跳私网地址视为无效目标。"""

        calls: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(302, headers={"Location": "http://10.0.0.9/healthz"})

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            kwargs.pop("verify", None)
            return real_client(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "aistudio_api.infrastructure.outbound_keepalive.httpx.AsyncClient",
            patched_client,
        )
        monkeypatch.setattr(settings, "keepalive_url", "https://example.com/healthz")
        monkeypatch.setattr(settings, "keepalive_interval", 5)
        svc = OutboundKeepAliveService()
        await svc.start()
        try:
            assert svc.is_running is True
            for _ in range(12):
                await asyncio.sleep(0.25)
                if len(calls) >= 1:
                    break
            # 任务必须仍在运行（失败只影响该轮，不终止服务）。
            assert svc.is_running is True
        finally:
            await svc.stop()


class TestAppWiring:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_keepalive_url")
    async def test_lifespan_boot_without_keepalive(self) -> None:
        """默认（未配置）时应用正常进入/退出 lifespan 且不产生保活任务。"""
        from aistudio_api.api.app import app, lifespan
        from aistudio_api.infrastructure import proxy_bridge as pb
        from aistudio_api.infrastructure.outbound_keepalive import (
            outbound_keepalive_service,
        )

        try:
            async with lifespan(app):
                assert outbound_keepalive_service.is_running is False
        finally:
            # lifespan 关停会把全局 bridge_manager 置为 closed；恢复供后续
            # 测试（同 TestClient 在多测试进程内的隔离需求，见类 docstring）。
            pb.bridge_manager.reopen()

    @pytest.mark.asyncio
    async def test_lifespan_boot_with_keepalive_round_trip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置合法 URL 时任务随 lifespan 启停，退出后不残留。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            kwargs.pop("verify", None)
            return real_client(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "aistudio_api.infrastructure.outbound_keepalive.httpx.AsyncClient",
            patched_client,
        )
        monkeypatch.setattr(settings, "keepalive_url", "https://keepalive.test/healthz")
        monkeypatch.setattr(settings, "keepalive_interval", 5)

        from aistudio_api.api.app import app, lifespan
        from aistudio_api.infrastructure import proxy_bridge as pb
        from aistudio_api.infrastructure.outbound_keepalive import (
            outbound_keepalive_service,
        )

        try:
            async with lifespan(app):
                assert outbound_keepalive_service.is_running is True
        finally:
            pb.bridge_manager.reopen()
        # lifespan 关停后任务已停止且不残留。
        assert outbound_keepalive_service.is_running is False
        assert outbound_keepalive_service._task is None
