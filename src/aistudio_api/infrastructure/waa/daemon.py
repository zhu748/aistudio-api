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

from aistudio_api.infrastructure.gateway.model_defaults import (
    get_active_waa_server,
    set_runtime_waa_override,
    switch_active_waa_server,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("waa.daemon")

_daemon_proc: subprocess.Popen[bytes] | None = None
_daemon_lock = threading.Lock()
_PR_SET_PDEATHSIG = 1


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
    """
    if sys.platform != "linux":
        return
    try:
        if os.getppid() != expected_parent_pid:
            # 原父进程已死亡且我们已被收养——直接退出，避免孤儿进程
            os._exit(1)
        libc = ctypes.CDLL(None)
        # prctl(PR_SET_PDEATHSIG, SIGTERM)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
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


def _is_local_signer_url(url: str) -> bool:
    """本机 8081 实例判定（本地拉起/停止逻辑专用）。"""
    return is_local_signer_url(url, port=8081)


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


def _fallback_to_remote(reason: str, *, bin_path: Path) -> None:
    """回退远程签名节点。配置写入失败（如只读挂载）时仅告警不中断启动。"""
    build_cmd = (
        ".\\waa-signer\\build.bat (或 .\\waa-signer\\build.ps1)"
        if sys.platform == "win32"
        else "bash waa-signer/build.sh"
    )
    logger.warning(
        "%s: %s\n"
        "  ==> 当前已自动降级回退至远程签名节点 (remote_spinsnow)。\n"
        "  ==> 如需获得毫秒级本地原生性能并彻底脱离外部依赖，请运行构建脚本:\n"
        "      %s",
        reason,
        bin_path,
        build_cmd,
    )
    # 只读配置文件也必须让当前进程真正回退，避免继续请求已失败的本机端口。
    set_runtime_waa_override("remote_spinsnow")
    try:
        switch_active_waa_server("remote_spinsnow")
    except OSError as e:
        # config.yaml 只读挂载等场景：回退失败不应让整个服务起不来
        logger.warning(
            "自动回退远程签名节点时写入 config.yaml 失败（可能为只读挂载）: %s。"
            "本次请求将继续使用远程节点，但重启后仍会尝试本地引擎。",
            e,
        )


def ensure_signer_ready(*, server_url: str | None = None) -> str | None:
    """检查本地 WAA 签名服务状态，未运行时自动拉起或切换至远程节点。

    本函数含阻塞 I/O（探活/子进程等待），禁止直接在事件循环线程内调用，
    请使用 ``await asyncio.to_thread(ensure_signer_ready)``。
    返回本地启动失败原因；指定 server_url 时仅准备该节点，不改变激活配置，
    供面板测试未激活的本地节点使用。
    """
    global _daemon_proc
    with _daemon_lock:
        url = (
            server_url
            if server_url is not None
            else str(get_active_waa_server().get("url") or "")
        )

        root_dir = Path(__file__).resolve().parents[4]
        bin_name = "waa-signer.exe" if sys.platform == "win32" else "waa-signer"
        bin_path = root_dir / "waa-signer" / "bin" / bin_name

        is_local_8081 = _is_local_signer_url(url)
        if not is_local_8081:
            if bin_path.is_file():
                logger.info(
                    "检测到本地 WAA 签名服务已就绪 (%s)。当前配置为远程节点 [%s]，如需启用本地原生引擎可在 Web 控制台或 config.yaml 中切换为 local_spidermonkey",
                    bin_path.name,
                    url,
                )
            return None

        # 1. 检查本地 8081 是否已有健康实例在运行
        if is_service_healthy(url):
            logger.debug("本地 WAA 签名服务运行正常 (127.0.0.1:8081)")
            return None

        # 端口已被其它进程占用但 /health 不通过：不要贸然再拉起一个实例
        # （会立刻因端口冲突退出）。但也绝不能就这么返回——那会让配置继续指向
        # 一个不可用的本地节点，后续所有签名请求都失败。这里直接回退远程节点，
        # 且不去停止/杀死占用端口的进程（它不在我们的管理范围内）。
        if is_port_open("127.0.0.1", 8081):
            reason = (
                "端口 127.0.0.1:8081 已被占用，但 /health 探活失败；"
                "请确认占用该端口的进程，或释放端口后重试"
            )
            if server_url is None:
                _fallback_to_remote(reason, bin_path=bin_path)
            return reason

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
            proxy = os.getenv("AISTUDIO_PROXY") or os.getenv("HTTPS_PROXY") or ""
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

        # 3. 若无本地二进制或拉起失败，先回收子进程，再回退到远程签名节点
        #    （不回收会留下僵尸进程并长期占用 8081，导致后续重启无法绑定端口）。
        if _daemon_proc is not None:
            _stop_signer_daemon_locked()

        if server_url is None:
            _fallback_to_remote(reason, bin_path=bin_path)
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
                proc.wait(timeout=1.0)
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
