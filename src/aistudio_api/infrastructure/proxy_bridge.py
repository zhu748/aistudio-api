"""TURN / TURNS / SSTP 桥接代理的进程池管理与统一解析入口。

接法学习自 vertex2openai 的 proxy_resolver：这三类协议无法被 httpx 直接
消费，由独立的 Go 桥接器（``proxybridge/``，产物 ``aistudio-proxy-bridge``）
把单个节点转换为**仅监听 127.0.0.1 的本地 SOCKS5**；本模块负责子进程的
生命周期（惰性启动、按 URI 复用、崩溃重启、空闲回收、关停清扫）并把
任意代理 URI 解析为 httpx 可直接使用的形态：

- 空 / ``direct`` 哨兵 → ``None``（直连）；
- ``http`` / ``https`` / ``socks5`` / ``socks5h`` → 原样返回；
- ``turn`` / ``turns`` / ``sstp`` → ``socks5://127.0.0.1:{port}``。

失败一律 fail-closed（抛 :class:`ProxyBridgeError`，绝不静默降级直连，
防止真实出口 IP 意外泄漏）。URI 的严格解析与规范化在
``infrastructure/account/cookie_parser.py``（单一权威实现）。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from aistudio_api.infrastructure.account.cookie_parser import (
    ProxyFormatError,
    normalize_proxy_url,
    parse_bridge_proxy_uri,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("proxy_bridge")

__all__ = [
    "ProxyBridgeError",
    "bridge_manager",
    "bridge_status",
    "resolve_proxy_url",
    "resolve_proxy_url_async",
    "resolve_proxy_url_cached",
    "shutdown_bridge_processes",
]


class ProxyBridgeError(RuntimeError):
    """桥接代理解析 / 启动失败（文案可展示，不含凭据）。"""

    def __init__(self, message: str, *, retry_new_port: bool = False) -> None:
        super().__init__(message)
        self.retry_new_port = retry_new_port


# ---------------------------------------------------------------------------
# 二进制发现与状态目录
# ---------------------------------------------------------------------------

_BRIDGE_BIN_NAME = "aistudio-proxy-bridge"
_BRIDGE_EXE_NAME = f"{_BRIDGE_BIN_NAME}.exe"


def _project_root() -> Path:
    """仓库根目录（本模块位于 src/aistudio_api/infrastructure/ 下）。"""
    return Path(__file__).resolve().parents[3]


def find_bridge_binary() -> str | None:
    """按固定顺序发现桥接器二进制；找不到返回 None（fail-closed 报错用）。

    顺序：``AISTUDIO_PROXY_BRIDGE_BIN`` 环境变量 → 仓库 ``proxybridge/bin/``
    → Docker 镜像安装路径 → ``PATH``。Termux 本地开发用 build.sh 产出
    仓库内路径；Docker / Render 部署由镜像内置。
    """
    repo_bin = (
        _project_root()
        / "proxybridge"
        / "bin"
        / (_BRIDGE_EXE_NAME if os.name == "nt" else _BRIDGE_BIN_NAME)
    )
    candidates = [
        os.getenv("AISTUDIO_PROXY_BRIDGE_BIN", ""),
        str(repo_bin),
        f"/usr/local/bin/{_BRIDGE_BIN_NAME}",
        shutil.which(_BRIDGE_BIN_NAME) or "",
    ]
    for path in candidates:
        if not path:
            continue
        absolute = Path(path).resolve()
        if absolute.is_file() and (
            os.access(str(absolute), os.X_OK) or os.name == "nt"
        ):
            return str(absolute)
    return None


def _bridge_state_dir() -> Path:
    """桥接器临时文件目录（私有配置 / 日志 / ready 标记）。

    跟随 ``AISTUDIO_DATA_DIR``（默认仓库 ``data/``），绝不硬编码 /tmp
    （Termux 无系统 /tmp）。目录权限 0700：配置文件含节点凭据。
    """
    from aistudio_api.config import settings

    return Path(settings.data_dir) / "proxy-bridge"


def _idle_minutes() -> float:
    """空闲回收阈值（分钟）；非法 / 未设置默认 30，下限 0 表示不回收。"""
    raw = os.getenv("AISTUDIO_PROXY_BRIDGE_IDLE_MINUTES", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else 30.0
    except ValueError:
        return 30.0


# ---------------------------------------------------------------------------
# 桥接器实例与就绪探测
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _BridgeInstance:
    """单个桥接器子进程的运行时描述。"""

    uri: str
    port: int
    process: subprocess.Popen[bytes]
    log_path: Path
    started_at: float
    last_used: float

    def alive(self) -> bool:
        return self.process.poll() is None

    def log_tail(self, limit: int = 400) -> str:
        """读取日志尾部（有界），供启动失败时的受控错误码提取。"""
        try:
            with self.log_path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - limit * 4))
                return f.read(limit * 4).decode("utf-8", errors="replace")[-limit:]
        except OSError:
            return ""


def _find_free_port() -> int:
    """让 OS 分配一个空闲回环端口（bind(0) 后立即关闭，竞态可接受）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_socks5_ready(
    port: int,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
) -> bool:
    """轮询本地端口直到 SOCKS5 方法协商应答。

    仅 TCP connect 成功不算就绪：端口可能被其他进程占着（崩溃复活场景），
    必须收到 SOCKS5 应答（发 ``\\x05\\x01\\x00``、回 ``\\x05\\x00``）才认；
    process 传入时子进程一退出立即返回，不等满超时。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4) as s:
                s.settimeout(0.5)
                s.sendall(b"\x05\x01\x00")
                reply = bytearray()
                while len(reply) < 2:
                    chunk = s.recv(2 - len(reply))
                    if not chunk:
                        break
                    reply.extend(chunk)
                if reply == b"\x05\x00":
                    return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def _wait_bridge_ready(inst: _BridgeInstance, ready_file: Path, timeout: float) -> bool:
    """要求子进程自身的绑定确认（ready 文件）后再做 SOCKS5 握手校验。

    外来 SOCKS5 进程可能抢占已释放的动态端口（SSTP 握手长达 30s 的窗口
    期内），其握手应答绝不能算作我们子进程的就绪。
    """
    deadline = time.monotonic() + timeout
    while inst.alive() and time.monotonic() < deadline:
        try:
            ready = ready_file.read_text(encoding="ascii") == "ready\n"
        except OSError:
            ready = False
        if ready:
            return _wait_socks5_ready(
                inst.port,
                timeout=max(0.1, deadline - time.monotonic()),
                process=inst.process,
            )
        time.sleep(0.05)
    return False


def _terminate(inst: _BridgeInstance) -> None:
    """终止子进程（terminate → 3s 后 kill），异常静默（关停路径兜底）。"""
    process = inst.process
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    except Exception:  # 关停兜底：任何异常都不能阻断其余实例回收
        pass


# Go 侧启动期（ready 文件写出前）可达的全部受控错误码 → 中文文案。
# ⚠️ 跨进程契约（与 proxybridge/main.go 同步维护）：新增 / 改名启动期
# 错误码时必须同步更新本字典，遗漏不会报错，只会退化为笼统文案。
_STARTUP_ERROR_HINTS: dict[str, str] = {
    "ca_read_failed": "无法读取 CA 证书文件",
    "ca_invalid_pem": "CA 文件不是有效的 PEM 证书",
    "ca_setenv_failed": "无法设置 CA 环境变量（路径含非法字符）",
    "config_read_failed": "无法读取转换器配置文件",
    "server_connect_failed": "无法连接代理服务器（TCP 层）",
    "server_tls_failed_check_ca_sni": "代理服务器 TLS 握手失败（检查 CA 证书与 SNI）",
    "server_transport_unsupported": "代理服务器连接返回非 TCP 传输",
    "sstp_custom_ca_requires_linux": "SSTP 自定义 CA 当前需要 Linux",
    "sstp_auth_failed_requires_mschapv2": "SSTP 认证失败，需正确的 MS-CHAPv2 账号密码",
    "sstp_handshake_failed_check_tls_credentials_and_mschapv2": "SSTP TLS / PPP 握手失败",
    "sstp_handshake_timeout": "SSTP 握手超时（TCP / TLS / PPP 共 30 秒预算）",
    "sstp_ipv4_address_missing": "SSTP 服务器未分配可用的 IPv4 地址",
    "sstp_network_stack_failed": "SSTP 用户态网络栈初始化失败",
    "sstp_packet_bridge_failed": "SSTP 虚拟网卡桥接初始化失败（内核不支持或资源不足）",
    "turn_probe_failed_check_server": "TURN 服务器探测失败：连接或分配请求无响应",
    "unsupported_protocol": "不支持的代理协议（仅 TURN / TURNS / SSTP）",
    "listen_failed": "本地端口绑定失败",
    "ready_write_failed": "无法写入转换器就绪标记",
    "invalid_config": "转换器配置无效",
}


# ---------------------------------------------------------------------------
# 进程池管理器
# ---------------------------------------------------------------------------


class ProxyBridgeManager:
    """TURN/SSTP 桥接子进程池：代理 URI → 本地 SOCKS5，惰性启动、稳定复用。

    线程安全；同 URI 只起一个进程（按 URI 加锁，不同代理的启动互不阻塞）。
    崩溃的实例下次取用时自动重启：优先复用原端口（客户端池签名不变）；
    原端口被抢走时换新端口重试一次。凭据只经 0600 临时配置文件传递，
    绝不进入命令行（ps 泄漏面）。
    """

    engine_name = "TURN/SSTP 转换器"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._instances: dict[str, _BridgeInstance] = {}
        self._uri_locks: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._closed = False
        self._file_namespace = os.urandom(6).hex()
        self._swept = False

    # ---------- 对外 ----------

    def get_local_socks5(self, uri: str) -> str:
        """URI → ``socks5://127.0.0.1:{port}``；首次调用启动桥接子进程。

        阻塞提示：首次启动含子进程拉起与就绪等待（TURN 典型 ~100ms-10s、
        SSTP 含 TLS/PPP 握手最多 35s）。事件循环线程请先经
        ``resolve_proxy_url_async`` 预热，避免卡住全部并发请求。
        """
        cached = self._cached_local_socks5(uri)
        if cached is not None:
            return cached
        with self._uri_lock(uri):
            cached = self._cached_local_socks5(uri)
            if cached is not None:
                return cached
            with self._lock:
                inst = self._instances.get(uri)
            stale_port = inst.port if inst is not None else None
            try:
                inst = self._launch(uri, stale_port or _find_free_port())
            except ProxyBridgeError as exc:
                if stale_port is None or not exc.retry_new_port:
                    raise
                # 崩溃重启偏好复用原端口（保池签名），但端口可能已被其他
                # 进程抢走——换新端口重试一次；仍失败则清掉死条目再抛。
                try:
                    inst = self._launch(uri, _find_free_port())
                except ProxyBridgeError:
                    with self._lock:
                        self._instances.pop(uri, None)
                    raise
            with self._lock:
                if self._closed:
                    _terminate(inst)
                    raise ProxyBridgeError(
                        f"进程正在退出，{self.engine_name} 已停。",
                        retry_new_port=False,
                    )
                self._instances[uri] = inst
            return f"socks5://127.0.0.1:{inst.port}"

    def cached_local_socks5(self, uri: str) -> str | None:
        """已在运行的 URI → 本地 SOCKS5；未启动返回 None（无副作用）。"""
        return self._cached_local_socks5(uri)

    def shutdown_all(self) -> None:
        """停机：终止全部桥接进程（FastAPI shutdown / atexit 双保险）。"""
        with self._lock:
            self._closed = True
            instances = list(self._instances.values())
            self._instances.clear()
        for inst in instances:
            _terminate(inst)  # 锁外终止：terminate+kill 最多 6s，不阻塞取用

    def reopen(self) -> None:
        """停机后重新启用管理器（测试隔离 / 运行时重启场景）。"""
        with self._lock:
            self._closed = False

    def reap_idle(self) -> int:
        """回收空闲实例（已崩溃的死实例不受空闲阈值保护，总是清理）。"""
        idle_seconds = _idle_minutes() * 60.0
        now = time.time()
        with self._lock:
            keys = list(self._instances)
        removed = 0
        for uri in keys:
            lock = self._uri_lock(uri)
            if not lock.acquire(blocking=False):
                continue  # 有并发重启持有该 URI 锁：不动它的端口
            try:
                with self._lock:
                    inst = self._instances.get(uri)
                    if inst is None:
                        continue
                    if inst.alive() and (
                        idle_seconds <= 0 or now - inst.last_used <= idle_seconds
                    ):
                        continue
                    self._instances.pop(uri)
                _terminate(inst)
                removed += 1
            finally:
                lock.release()
        return removed

    def stats(self) -> dict[str, Any]:
        """运行概览（诊断接口展示，URI 已脱敏）。"""
        with self._lock:
            items = list(self._instances.values())
        binary = find_bridge_binary()
        return {
            "available": bool(binary),
            "binary": binary or "",
            "idle_minutes": _idle_minutes(),
            "instances": [
                {
                    "port": i.port,
                    "uri": _mask_bridge_uri(i.uri),
                    "alive": i.alive(),
                    "started_at": int(i.started_at),
                }
                for i in items
            ],
        }

    # ---------- 内部 ----------

    def _cached_local_socks5(self, uri: str) -> str | None:
        with self._lock:
            if self._closed:
                raise ProxyBridgeError(
                    f"进程正在退出，{self.engine_name} 已停。", retry_new_port=False
                )
            inst = self._instances.get(uri)
            if inst is not None and inst.alive():
                inst.last_used = time.time()
                return f"socks5://127.0.0.1:{inst.port}"
        return None

    def _uri_lock(self, uri: str) -> threading.Lock:
        """共享启动锁：当前等待者复用同一把，无人等待时自动丢弃。"""
        with self._lock:
            lock = self._uri_locks.get(uri)
            if lock is None:
                lock = threading.Lock()
                self._uri_locks[uri] = lock
            return lock

    def _launch(self, uri: str, port: int) -> _BridgeInstance:
        """启动单个桥接子进程并等待就绪；失败按错误契约抛中文指引。"""
        try:
            config = parse_bridge_proxy_uri(uri)
        except ProxyFormatError as exc:
            raise ProxyBridgeError(str(exc), retry_new_port=False) from None
        binary = find_bridge_binary()
        if not binary:
            raise ProxyBridgeError(
                "未找到 TURN/SSTP 转换器 aistudio-proxy-bridge。解决方法任选其一："
                "① 使用包含该组件的新版本 Docker 镜像；"
                "② 在 proxybridge/ 目录执行 bash build.sh（需要 Go >= 1.27）；"
                "③ 通过环境变量 AISTUDIO_PROXY_BRIDGE_BIN 指定可执行文件。"
                "在装好之前，turn/turns/sstp 协议的代理无法使用"
                "（为防真实 IP 泄露，不会自动降级直连）。",
                retry_new_port=False,
            )
        launch_config: dict[str, Any] = {
            "scheme": config["scheme"],
            "server": config["server"],
            "server_port": config["server_port"],
            "username": config["username"],
            "password": config["password"],
            "tls_server_name": config["tls_server_name"],
            "ca_file": config["ca_file"],
            "insecure": config["insecure"],
            "realm": config["realm"],
            "mtu": config["mtu"],
        }
        launch_config["listen_port"] = port
        directory = _bridge_state_dir()
        digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]
        prefix = f"bridge-{os.getpid()}-{self._file_namespace}-{digest}-"
        config_path = directory / f"{prefix}{port}.json"
        log_path = directory / f"{prefix}{port}.log"
        ready_path = directory / f"{prefix}{port}.ready"
        launch_config["ready_file"] = str(ready_path)
        inst: _BridgeInstance | None = None
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            self._sweep_orphan_files(directory)
            for stale in directory.glob(f"{prefix}*"):
                stale.unlink(missing_ok=True)
            # 0600：配置含节点凭据，umask 默认 0644 会让同机其他本地账号
            # 读到完整代理凭证；os.open 的 mode 仅创建时生效，残余文件靠
            # 前面的 stale 清扫兜底。
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(launch_config, file)
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(log_fd, "ab", buffering=0) as log_file:
                if os.name == "nt":
                    # Windows：独立进程组 + 无控制台窗口（不弹 cmd 框）
                    popen_kwargs: dict[str, Any] = {
                        "stdout": log_file,
                        "stderr": subprocess.STDOUT,
                        "stdin": subprocess.DEVNULL,
                        "creationflags": (
                            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        ),
                    }
                else:
                    popen_kwargs = {
                        "stdout": log_file,
                        "stderr": subprocess.STDOUT,
                        "stdin": subprocess.DEVNULL,
                        "start_new_session": True,  # 进程组独立，Ctrl+C 不误杀
                    }
                # stdout 为二进制句柄且未开文本模式，运行时为 Popen[bytes]；
                # **kwargs 透传会让 pyright 选中 str 重载，这里显式钉住泛型。
                process = cast(
                    "subprocess.Popen[bytes]",
                    subprocess.Popen(
                        [binary, "-config", str(config_path)], **popen_kwargs
                    ),
                )
            inst = _BridgeInstance(
                uri=uri,
                port=port,
                process=process,
                log_path=log_path,
                started_at=time.time(),
                last_used=time.time(),
            )
            # SSTP 握手预算 30s + 余量；TURN 探测/分配 10s
            timeout = 35.0 if str(config["scheme"]) == "sstp" else 10.0
            if not _wait_bridge_ready(inst, ready_path, timeout) or not inst.alive():
                self._terminate_instance(inst)
                if self._closed:
                    raise ProxyBridgeError(
                        "服务正在退出，已取消代理启动。", retry_new_port=False
                    )
                # 只转发受控错误码：任意子进程输出都可能回显凭据
                code = next(
                    (
                        line.strip()
                        for line in reversed(inst.log_tail().splitlines())
                        if line.strip() in _STARTUP_ERROR_HINTS
                    ),
                    "",
                )
                detail = _STARTUP_ERROR_HINTS.get(code, "启动超时或子进程退出")
                raise ProxyBridgeError(
                    f"{str(config['scheme']).upper()} 转换失败：{detail}。"
                    "请检查服务器连通性、账号密码、TLS 证书，以及 TURN RFC6062 / "
                    "SSTP MS-CHAPv2 支持。",
                    retry_new_port=code == "listen_failed",
                )
        except ProxyBridgeError:
            raise
        except OSError as exc:
            if inst is not None:
                self._terminate_instance(inst)
            raise ProxyBridgeError(
                f"无法创建或启动 TURN/SSTP 转换器，请检查状态目录权限与可执行文件：{exc}",
            ) from None
        finally:
            # 配置含凭据：启动后（成功 / 失败路径都）立即删除临时文件
            for path in (config_path, ready_path):
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        logger.info(
            "TURN/SSTP 桥接已启动: %s -> 127.0.0.1:%d",
            _mask_bridge_uri(uri),
            port,
        )
        return inst

    def _terminate_instance(self, inst: _BridgeInstance) -> None:
        _terminate(inst)

    def _sweep_orphan_files(self, directory: Path) -> None:
        """删除属主进程已消失的桥接状态文件（SIGKILL / OOM 残留）。

        文件名内嵌 pid：pid 已死且超过 1 小时宽限期才删；pid 复用只会导致
        少删（安全方向）。本进程实例的重启残留由前缀清扫覆盖。
        """
        if self._swept:
            return
        self._swept = True
        now = time.time()
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for path in entries:
            parts = path.name.split("-")
            # bridge-{pid}-{ns}-{digest}-{port}.{json|log|ready}
            if len(parts) < 5 or parts[0] != "bridge" or not parts[1].isdigit():
                continue
            pid = int(parts[1])
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
                continue  # 属主仍存活（含僵尸）：保留
            except ProcessLookupError:
                pass  # 已死：孤儿
            except OSError:
                continue  # 无法判定（权限 / 平台差异）：保守保留
            try:
                if now - path.stat().st_mtime > 3600:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def _mask_bridge_uri(uri: str) -> str:
    """脱敏桥接代理 URI 的凭据（日志 / 诊断展示用）。"""
    return re.sub(r"//[^@/]+@", "//***@", uri)


# ---------------------------------------------------------------------------
# 模块级单例与后台回收
# ---------------------------------------------------------------------------

bridge_manager = ProxyBridgeManager()

_reaper_started = False
_reaper_lock = threading.Lock()


def _reaper_loop() -> None:
    """后台空闲回收线程（每 5 分钟扫一次，daemon）。"""
    while True:
        time.sleep(300)
        try:
            removed = bridge_manager.reap_idle()
            if removed:
                logger.info("已回收 %d 个空闲/已退出的 TURN/SSTP 桥接实例。", removed)
        except Exception as e:  # 回收线程兜底：异常不能杀死线程
            logger.debug("TURN/SSTP 桥接空闲回收异常（已忽略）: %s", e)


def _ensure_reaper() -> None:
    """惰性启动空闲回收线程（首次成功创建桥接实例时调用）。"""
    global _reaper_started
    with _reaper_lock:
        if _reaper_started:
            return
        _reaper_started = True
        threading.Thread(
            target=_reaper_loop, daemon=True, name="proxy-bridge-reaper"
        ).start()


def shutdown_bridge_processes() -> None:
    """终止全部桥接子进程（FastAPI lifespan 关停钩子调用）。"""
    bridge_manager.shutdown_all()


atexit.register(shutdown_bridge_processes)


# ---------------------------------------------------------------------------
# 统一解析入口
# ---------------------------------------------------------------------------


def _warn_sync_resolve_blocked_loop(kind: str, started: float) -> None:
    """同步 resolve 阻塞了事件循环时告警（>50ms 才报，缓存命中零误报）。"""
    elapsed = time.monotonic() - started
    if elapsed <= 0.05:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # 工作线程 / 无循环：同步调用合法
    logger.warning(
        "resolve_proxy_url(%s) 在事件循环线程上同步冷启动桥接子进程，"
        "本次阻塞 %.1f 秒（全部并发请求停摆）；异步上下文请改用 "
        "resolve_proxy_url_async，或先经它预热使缓存命中。",
        kind,
        elapsed,
    )


def resolve_proxy_url(raw: str | None) -> str | None:
    """任意代理 URI → httpx 可直接使用的代理 URL（同步版）。

    - 空 / ``direct`` 哨兵 → ``None``（直连）；
    - ``http`` / ``https`` / ``socks5`` / ``socks5h`` → 原样返回；
    - ``turn`` / ``turns`` / ``sstp`` → ``socks5://127.0.0.1:{port}``
      （首次使用时惰性启动桥接子进程；之后纯缓存查找）；
    - 无法识别 → 抛 :class:`ProxyBridgeError`（绝不静默降级直连）。

    正常复用期间端口稳定，客户端池签名不会因重复 resolve 而抖动。
    """
    text = (raw or "").strip()
    if not text or text.lower() in {"direct", "none", "off", "直连", "无代理"}:
        return None
    normalized = normalize_proxy_url(text)
    if normalized is None:
        raise ProxyBridgeError(f"代理地址无法识别：{_mask_bridge_uri(text)}")
    scheme = normalized.split("://", 1)[0].lower()
    if scheme in ("http", "https", "socks5", "socks5h"):
        return normalized
    started = time.monotonic()
    url = bridge_manager.get_local_socks5(normalized)
    _warn_sync_resolve_blocked_loop(scheme, started)
    _ensure_reaper()
    return url


async def resolve_proxy_url_async(raw: str | None) -> str | None:
    """:func:`resolve_proxy_url` 的异步版：冷启动放线程池，不阻塞事件循环。

    FastAPI 处理器等异步上下文应优先用这个入口；结果与同步版完全一致
    （同 URI 端口映射在进程内稳定）。
    """
    text = (raw or "").strip()
    if not text or text.lower() in {"direct", "none", "off", "直连", "无代理"}:
        return None
    normalized = normalize_proxy_url(text)
    if normalized is None:
        raise ProxyBridgeError(f"代理地址无法识别：{_mask_bridge_uri(text)}")
    scheme = normalized.split("://", 1)[0].lower()
    if scheme in ("http", "https", "socks5", "socks5h"):
        return normalized
    cached = bridge_manager.cached_local_socks5(normalized)
    if cached is not None:
        return cached
    return await asyncio.to_thread(resolve_proxy_url, normalized)


def resolve_proxy_url_cached(raw: str | None) -> str | None:
    """无副作用的解析：桥接实例已在运行时返回本地 SOCKS5，否则返回原始 URI。

    供「以新代理为保留键退役其它客户端池」这类不能触发子进程冷启动的
    场景使用（transport.retire_stale_clients）：native 协议原样返回，
    bridge 协议仅在实例存活时映射为本地地址。
    """
    text = (raw or "").strip()
    if not text:
        return None
    normalized = normalize_proxy_url(text)
    if normalized is None:
        return None
    scheme = normalized.split("://", 1)[0].lower()
    if scheme in ("http", "https", "socks5", "socks5h"):
        return normalized
    cached = bridge_manager.cached_local_socks5(normalized)
    return cached if cached is not None else normalized


def bridge_status() -> dict[str, Any]:
    """桥接器运行概览（诊断接口用）。"""
    return bridge_manager.stats()
