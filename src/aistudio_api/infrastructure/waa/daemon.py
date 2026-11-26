"""Automatic local waa-signer daemon manager with strict lifecycle binding.

Prevents orphan processes by:
1. Kernel-level PR_SET_PDEATHSIG (child receives SIGTERM immediately if parent dies).
2. atexit handler ensuring cleanup on any normal or abnormal Python exit.
3. Clean process group termination without zombie residues.

并发防护：模块级互斥锁串行化拉起/停止序列，杜绝两个管理路由并发
调用 ensure_signer_ready 时双拉起子进程导致句柄覆盖与僵尸进程。
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import functools
import io
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from aistudio_api.infrastructure.gateway.model_defaults import get_local_waa_server
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("waa.daemon")

_daemon_proc: subprocess.Popen[bytes] | None = None
_daemon_lock = threading.Lock()
_PR_SET_PDEATHSIG = 1
_PDEATHSIG_SIGNAL = int(signal.SIGTERM)

# preexec_fn 在 fork 出的子进程中执行，CPython 官方文档警告：多线程进程
# 中该回调一旦触发需持锁的分配（如 dlopen）就可能死锁——fork 瞬间其他
# 线程持有的锁不会释放。因此 libc 句柄与 prctl 签名在模块导入期解析
# 完毕，子进程内只剩纯整数的已解析函数调用；旧实现在子进程内
# CDLL(None) 触发 dlopen（需持动态加载器锁），正是文档所指危险形态。
# 全小写命名：该句柄在导入期按平台条件解析（非真常量），大写会被
# pyright reportConstantRedefinition 视为常量重定义。
_libc: ctypes.CDLL | None = None
if sys.platform == "linux":
    try:
        _libc_handle = ctypes.CDLL(None)
        _libc_handle.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        _libc_handle.prctl.restype = ctypes.c_int
        _libc = _libc_handle
    except Exception:
        _libc = None


def _relay_signer_logs(proc: subprocess.Popen[bytes]) -> None:
    stream = getattr(proc, "stdout", None)
    if not isinstance(stream, io.BufferedIOBase):
        return
    try:
        for line in iter(lambda: stream.readline(8192), b""):
            text = line.decode("utf-8", errors="replace").strip()[:8192]
            level = (
                "error"
                if "level=ERROR" in text
                else "warning"
                if "level=WARN" in text
                else "info"
            )
            getattr(get_logger("waa.process"), level)("%s", text)
    finally:
        stream.close()


def _set_parent_death_signal(expected_parent_pid: int) -> None:
    """Linux kernel-level hook: child process is terminated if parent dies.

    在 prctl 之前校验 getppid：若 fork 与 prctl 之间父进程已死亡（经典
    PDEATHSIG 竞态），子进程已被 init 收养，立即退出而不是成为孤儿。

    子进程内只允许纯 syscall 与导入期已解析的整数调用（见模块级 _libc
    注释）：os.getppid 无分配，prctl 走预解析句柄，杜绝 fork-with-threads
    死锁窗口。
    """
    if _libc is None:
        return
    try:
        if os.getppid() != expected_parent_pid:
            # 原父进程已死亡且我们已被收养——直接退出，避免孤儿进程
            os._exit(1)
        # prctl(PR_SET_PDEATHSIG, SIGTERM)
        _libc.prctl(_PR_SET_PDEATHSIG, _PDEATHSIG_SIGNAL, 0, 0, 0)
    except Exception:
        pass


def is_port_open(host: str, port: int) -> bool:
    """Check whether a TCP port is open and accepting connections."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def is_local_signer_url(url: str, *, port: int | None = None) -> bool:
    """判断签名服务器地址是否指向本机（可选限定端口）。

    必须按主机名精确比较：旧实现用 ``"127.0.0.1:8081" in url`` 子串判断，
    ``http://127.0.0.1:8081.evil.com`` 之类的地址会被误判为本机。
    """
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    return port is None or parsed_port == port


def is_service_healthy(url: str) -> bool:
    """探活签名服务 /health。

    本地探活必须绕过系统代理：urllib 默认启用环境代理（HTTPS_PROXY 等），
    一旦用户配置了出站代理，探活请求会被发往代理侧的 localhost 并必然失败，
    导致刚拉起的本地引擎被误判死亡而触发不必要的远程回退。
    （signer.py 对本地 URL 已显式绕过代理，此处保持一致。）
    """
    import urllib.request

    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=0.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_signer_ready(*, server_url: str | None = None) -> str | None:
    """检查本地 WAA 签名服务状态，未运行时自动拉起。

    本地引擎是唯一签名方式（远程签名与多节点切换支持已移除）：拉起
    失败只报告原因（构建指引见日志），不再回退任何外部节点。
    本函数含阻塞 I/O（探活/子进程等待），禁止直接在事件循环线程内调用，
    请使用 ``await asyncio.to_thread(ensure_signer_ready)``。
    返回本地启动失败原因；指定 server_url 时仅准备该节点（测试注入用）。
    """
    global _daemon_proc
    with _daemon_lock:
        url = (
            server_url
            if server_url is not None
            else str(get_local_waa_server().get("url") or "")
        )

        root_dir = Path(__file__).resolve().parents[4]
        bin_name = "waa-signer.exe" if sys.platform == "win32" else "waa-signer"
        bin_path = root_dir / "waa-signer" / "bin" / bin_name

        # 1. 检查本地 8081 是否已有健康实例在运行
        if is_service_healthy(url):
            logger.debug("本地 WAA 签名服务运行正常 (127.0.0.1:8081)")
            return None

        # 端口已被其它进程占用但 /health 不通过：不要贸然再拉起一个实例
        # （会立刻因端口冲突退出）。返回原因让调用方明确知晓本地引擎
        # 不可用；不去停止/杀死占用端口的进程（它不在我们的管理范围内）。
        if is_port_open("127.0.0.1", 8081):
            return (
                "端口 127.0.0.1:8081 已被占用，但 /health 探活失败；"
                "请确认占用该端口的进程，或释放端口后重试"
            )

        # 2. 检查本地编译产物是否存在
        is_executable = bin_path.is_file() and (
            sys.platform == "win32" or os.access(bin_path, os.X_OK)
        )
        reason = (
            f"本地签名器没有执行权限: {bin_path}，请为该文件添加执行权限"
            if bin_path.is_file()
            else f"未检测到本地 WAA 签名服务二进制产物: {bin_path}，"
            "请运行 waa-signer 构建脚本，或重新构建包含本地签名器的 Docker 镜像"
        )
        if is_executable:
            # 全局代理传给 Go 签名器（仅 http/https/socks5；turn/sstp 先解析
            # 为本地回环 SOCKS5，解析失败按无代理启动并在日志里给出指引——
            # 签名器拉起失败会整体回退远程节点，不应因代理单点问题阻断启动）。
            raw_proxy = (
                os.getenv("AISTUDIO_PROXY") or os.getenv("HTTPS_PROXY") or ""
            ).strip()
            proxy = ""
            if raw_proxy:
                from aistudio_api.infrastructure.proxy_bridge import (
                    normalize_proxy_for_go_signer,
                    resolve_proxy_url,
                )

                try:
                    resolved_proxy = resolve_proxy_url(raw_proxy)
                except Exception as e:
                    logger.warning(
                        "全局代理 %s 解析失败，本地签名器本次直连启动: %s", raw_proxy, e
                    )
                    resolved_proxy = None
                if resolved_proxy:
                    proxy = normalize_proxy_for_go_signer(resolved_proxy)
            cmd = [str(bin_path), "-listen", "127.0.0.1:8081"]
            if proxy:
                cmd.extend(["-proxy", proxy])

            logger.info("启动本地 WAA 签名服务: %s", bin_path)
            try:
                popen_kwargs: dict[str, object] = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "cwd": str(bin_path.parent.parent),
                    # PORT 属于 PaaS 主服务；旧版签名器在解析 -listen 前就
                    # 检查它，显式覆盖默认监听地址也能兼容这些已有二进制。
                    "env": {**os.environ, "LISTEN_ADDR": "127.0.0.1:8081"},
                }
                from aistudio_api.config import settings

                if settings.signer_secret:
                    popen_kwargs["env"] = {
                        **os.environ,
                        "LISTEN_ADDR": "127.0.0.1:8081",
                        "SIGNER_SHARED_SECRET": settings.signer_secret,
                    }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["preexec_fn"] = functools.partial(
                        _set_parent_death_signal, os.getpid()
                    )

                proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[call-overload]
                threading.Thread(
                    target=_relay_signer_logs,
                    args=(proc,),
                    daemon=True,
                    name="signer-logs",
                ).start()
                _daemon_proc = proc
                # 冷启动给 10 秒宽限；提前退出时立即报告退出码。
                deadline = time.monotonic() + 10.0
                reason = "本地 WAA 签名服务启动后 10 秒内未通过 /health 探活，请查看 waa.process 日志"
                while time.monotonic() < deadline:
                    returncode = proc.poll()
                    if returncode is not None:
                        reason = f"本地 WAA 签名服务提前退出（退出码 {returncode}），请查看 waa.process 日志"
                        break
                    if is_service_healthy(url):
                        logger.info("本地 WAA 签名服务已就绪 (PID %d)", proc.pid)
                        return None
                    time.sleep(0.1)
                logger.warning("%s", reason)
            except Exception as e:
                reason = f"启动本地 WAA 签名服务失败: {e}"
                logger.warning("%s", reason)

        # 3. 若无本地二进制或拉起失败：回收子进程（不回收会留下僵尸进程
        #    并长期占用 8081，导致后续重启无法绑定端口），返回失败原因。
        if _daemon_proc is not None:
            _stop_signer_daemon_locked()

        return reason


def _stop_signer_daemon_locked() -> None:
    """终止自动拉起的本地签名服务子进程（调用方必须已持有 _daemon_lock）。"""
    global _daemon_proc
    if _daemon_proc is not None:
        proc = _daemon_proc
        _daemon_proc = None
        try:
            logger.info("停止本地 WAA 签名服务 (PID %d)", proc.pid)
            proc.terminate()
            try:
                # Go 侧 SIGTERM 处理是 server.Shutdown(5s) + 池排空
                # （main.go），原先 1s 即 SIGKILL 会硬杀在途 mint 的 VM
                # 拆除与状态目录写入；对齐 5s 优雅预算，超时才 kill。
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=0.5)
        except Exception as e:
            logger.debug("停止签名服务子进程异常: %s", e)
            with contextlib.suppress(Exception):
                proc.kill()


def stop_signer_daemon() -> None:
    """终止自动拉起的本地签名服务子进程，杜绝孤儿进程与僵尸进程。"""
    with _daemon_lock:
        _stop_signer_daemon_locked()


# 注册 Python 解释器退出钩子，双重保险杜绝孤儿进程
atexit.register(stop_signer_daemon)
