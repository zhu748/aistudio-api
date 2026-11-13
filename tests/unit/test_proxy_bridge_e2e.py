"""跨进程真实验证：httpx → 本地 SOCKS5 → Go 桥接器 → Pion TURN(RFC 6062) → HTTP 目标。

与 test_proxy_bridge.py 的全 mock 测试互补：这里用真实编译的
``aistudio-proxy-bridge`` 二进制 + ``proxybridge/testdata/turnfixture.go``
起一个真实的 Pion TURN 服务器（长期凭据 + RFC 6062 TCP 中继），从
``resolve_proxy_url_async`` 冷启动开始验证完整链路，包括：

- 桥接子进程真实拉起（ready 文件 + SOCKS5 握手双条件就绪）；
- TURN 长期凭据认证、TCP 分配与 ConnectionBind 数据面；
- httpx 经本地 SOCKS5 的 HTTP 请求真实往返；
- 同 URI 重复解析端口稳定（进程复用，无二次拉起）；
- 凭据错误时 fail-closed：SOCKS CONNECT 被拒（连接期报错，绝不降级直连）；
- 关停清扫终止真实子进程。

前置条件（缺失任意即整文件 skip，普通 CI 不阻塞）：
1. Go 工具链（编译 TURN fixture）；
2. 桥接器二进制：现成 ``proxybridge/bin/`` 或用 Go 现场编译（经
   ``AISTUDIO_PROXY_BRIDGE_BIN`` 注入，顺带验证该环境变量分支）。
"""

from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import aistudio_api.infrastructure.proxy_bridge as pb
from aistudio_api.infrastructure.proxy_bridge import (
    resolve_proxy_url_async,
    resolve_proxy_url_cached,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROXYBRIDGE_DIR = _REPO_ROOT / "proxybridge"
_FIXTURE_GO = _PROXYBRIDGE_DIR / "testdata" / "turnfixture.go"

_FIXTURE_USER = "fixture-user"
_FIXTURE_PASSWORD = "fixture-password"
_FIXTURE_REALM = "fixture"


# ---------------------------------------------------------------------------
# Session 级前置：Go 工具链、二进制与 TURN fixture 服务器
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _build_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """session 共享的构建/运行临时目录。"""
    return tmp_path_factory.mktemp("bridge_e2e")


@pytest.fixture(scope="session")
def monkeypatch_session() -> Iterator[pytest.MonkeyPatch]:
    """session 级 monkeypatch（官方 MonkeyPatch fixture 是 function 级）。"""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="session")
def _go(_build_dir: Path) -> str:
    go = shutil.which("go")
    if go is None:
        pytest.skip(
            "未找到 Go 工具链（需要 go >= 1.27 编译 TURN fixture），跳过跨进程 E2E"
        )
    if not _FIXTURE_GO.is_file():
        pytest.skip("TURN fixture 源码缺失：proxybridge/testdata/turnfixture.go")
    # testdata 目录不参与 go build .，须显式编译为独立 fixture 二进制
    out = _build_dir / "turnfixture"
    subprocess.run(
        [go, "build", "-o", str(out), "testdata/turnfixture.go"],
        cwd=_PROXYBRIDGE_DIR,
        check=True,
        capture_output=True,
        timeout=300,
    )
    return go


@pytest.fixture(scope="session")
def _bridge_binary(
    _go: str, _build_dir: Path, monkeypatch_session: pytest.MonkeyPatch
) -> str:
    """提供真实桥接器二进制：优先复用已构建产物，缺失则现场编译。"""
    existing = pb.find_bridge_binary()
    if existing:
        return existing
    out = _build_dir / "aistudio-proxy-bridge"
    subprocess.run(
        [_go, "build", "-o", str(out), "."],
        cwd=_PROXYBRIDGE_DIR,
        check=True,
        capture_output=True,
        timeout=300,
    )
    monkeypatch_session.setenv("AISTUDIO_PROXY_BRIDGE_BIN", str(out))
    return str(out)


def _read_ready_port(proc: subprocess.Popen[bytes]) -> int:
    """限时读取 fixture 的就绪行（JSON）；超时或异常则 skip。"""
    holder: dict[str, bytes] = {}

    def run() -> None:
        stream = proc.stdout
        if stream is not None:
            holder["line"] = stream.readline()

    reader = threading.Thread(target=run, daemon=True)
    reader.start()
    reader.join(15.0)
    if reader.is_alive():
        pytest.skip("TURN fixture 服务器未在限时内输出就绪端口")
    line = holder.get("line", b"").strip()
    if not line:
        pytest.skip(f"TURN fixture 服务器提前退出（code={proc.poll()}）")
    return int(json.loads(line)["turn_port"])


@pytest.fixture(scope="session")
def _turn_server(_go: str, _build_dir: Path, _bridge_binary: str) -> Iterator[int]:
    """起真实 Pion TURN 服务器（长期凭据 + RFC 6062 TCP 中继），yield 端口。"""
    proc = subprocess.Popen(
        [str(_build_dir / "turnfixture")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        yield _read_ready_port(proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        # 显式关闭 stdout 管道：依赖 GC 会在后续测试触发 error 级 ResourceWarning
        if proc.stdout is not None:
            proc.stdout.close()


@pytest.fixture(scope="session")
def _http_target() -> Iterator[int]:
    """本地 HTTP 目标（返回固定 body），yield 端口。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"bridge-relay-ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def _fresh_manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[pb.ProxyBridgeManager]:
    """函数级全新进程池：测试间互不共享实例，结束即终止真实子进程。"""
    manager = pb.ProxyBridgeManager()
    monkeypatch.setattr(pb, "bridge_manager", manager)
    yield manager
    manager.shutdown_all()


# ---------------------------------------------------------------------------
# 真实链路用例
# ---------------------------------------------------------------------------


async def test_turn_relay_full_path_with_realm(
    _turn_server: int, _http_target: int, _fresh_manager: pb.ProxyBridgeManager
) -> None:
    """显式 realm：httpx → SOCKS5 → 桥 → TURN 长期凭据 → HTTP 目标全链路。"""
    uri = (
        f"turn://{_FIXTURE_USER}:{_FIXTURE_PASSWORD}@127.0.0.1:{_turn_server}"
        f"?transport=tcp&realm={_FIXTURE_REALM}"
    )
    resolved = await resolve_proxy_url_async(uri)
    assert resolved is not None and resolved.startswith("socks5://127.0.0.1:")

    import httpx

    async with httpx.AsyncClient(proxy=resolved, trust_env=False, timeout=30) as client:
        resp = await client.get(f"http://127.0.0.1:{_http_target}/e2e")
    assert resp.status_code == 200
    assert resp.content == b"bridge-relay-ok"

    # 同 URI 二次解析：端口稳定（复用同一子进程，不重复拉起）
    resolved_again = await resolve_proxy_url_async(uri)
    assert resolved_again == resolved


async def test_turn_relay_realm_auto_negotiated(
    _turn_server: int, _http_target: int, _fresh_manager: pb.ProxyBridgeManager
) -> None:
    """不带 realm 参数：Pion 客户端从 401 challenge 自动协商 realm。"""
    uri = f"turn://{_FIXTURE_USER}:{_FIXTURE_PASSWORD}@127.0.0.1:{_turn_server}"
    resolved = await resolve_proxy_url_async(uri)
    assert resolved is not None

    import httpx

    async with httpx.AsyncClient(proxy=resolved, trust_env=False, timeout=30) as client:
        resp = await client.get(f"http://127.0.0.1:{_http_target}/auto")
    assert resp.status_code == 200
    assert resp.content == b"bridge-relay-ok"


async def test_cached_resolve_reports_running_instance(
    _turn_server: int, _fresh_manager: pb.ProxyBridgeManager
) -> None:
    """实例运行中：无副作用解析映射为本地 SOCKS5 而非原始 URI。"""
    uri = f"turn://{_FIXTURE_USER}:{_FIXTURE_PASSWORD}@127.0.0.1:{_turn_server}"
    resolved = await resolve_proxy_url_async(uri)
    assert resolved is not None
    assert resolve_proxy_url_cached(uri) == resolved


async def test_wrong_turn_credentials_fail_closed(
    _turn_server: int, _http_target: int, _fresh_manager: pb.ProxyBridgeManager
) -> None:
    """凭据错误：启动探测只区分匿名/认证模式，失败暴露在 CONNECT 期。

    手写 SOCKS5 客户端验证 CONNECT 被 TURN Allocate 拒绝（reply 失败码），
    绝不静默降级直连（直连成功才是事故）。不经 httpx：其 CONNECT 失败
    路径的半开 socket 交给 GC，会触发全局 error 级 ResourceWarning。
    """
    import asyncio

    bad_uri = f"turn://fixture-user:wrong-password@127.0.0.1:{_turn_server}"
    # 启动成功（认证模式探测通过），拿到本地 SOCKS5 入口
    resolved = await resolve_proxy_url_async(bad_uri)
    assert resolved is not None
    port = int(resolved.rsplit(":", 1)[1])
    http_port = _http_target

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        # 方法协商：无认证
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        # CONNECT 127.0.0.1:http_port（ATYP=IPv4）
        writer.write(b"\x05\x01\x00\x01\x7f\x00\x00\x01" + http_port.to_bytes(2, "big"))
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(10), timeout=30)
        assert reply[1] != 0, "TURN 凭据错误却 CONNECT 成功（疑似降级直连）"
    finally:
        writer.close()
        await writer.wait_closed()
    # 桥接进程仍存活（连接期失败不杀实例；诊断页仍可见其运行状态）
    assert _fresh_manager.cached_local_socks5(bad_uri) == resolved


async def test_bridge_shutdown_terminates_real_process(
    _turn_server: int, _fresh_manager: pb.ProxyBridgeManager
) -> None:
    """关停清扫终止真实子进程：本地 SOCKS5 端口随之失效。"""
    uri = f"turn://{_FIXTURE_USER}:{_FIXTURE_PASSWORD}@127.0.0.1:{_turn_server}"
    resolved = await resolve_proxy_url_async(uri)
    assert resolved is not None
    instance = next(iter(_fresh_manager._instances.values()))
    _fresh_manager.shutdown_all()
    assert not instance.alive()
    # 关停后再取用：fail-closed 报错而非复用死实例
    from aistudio_api.infrastructure.proxy_bridge import ProxyBridgeError

    with pytest.raises(ProxyBridgeError):
        _fresh_manager.get_local_socks5(uri)
