"""上游交换快照（URL / 有序线缆头 / 线缆体 / 响应样本与响应头）。

用途：把「实际发往 AI Studio 的最终请求」落盘为
``<dump_dir>/<request_id>.upstream*.json``，供请求详情页查看与下载
（排除「客户端侧原始请求 vs 网关转换后 wire 请求」的歧义——
2026-11-14 排查 403/400 时两次都需要人工 dump 才能定位线缆形态）。

采集策略（2026-11-14 起扩展，原「仅失败采集」→「成功也采集」）：

- **GenerateContent（模型主调用）**：成功与失败均采集。
  - 成功 → ``<request_id>.upstream.ok.json``，按保留条数环形淘汰
    （``logging.upstream_snapshot_keep``，默认 10，0 = 关闭成功采集）；
  - 失败 → ``<request_id>.upstream.json``，**不受条数限制**（错误现场
    优先保留），仅可手动清理；重试多次时最后一次尝试覆盖前次。
- **辅助 RPC（CountTokens / GenerateAccessToken 等）**：仅失败时采集，
  且绝不覆盖已存在的快照（防止 GenerateContent 之后执行的 CountTokens
  追随调用悄悄顶掉主调用的成功快照——同一 request_id 共享文件名）。

安全边界：
- 凭据头（authorization/cookie/x-goog-api-key/proxy-authorization/
  set-cookie）只保留长度，值不落盘（请求头与**响应头**同规则）；
- wire 体槽位中的 WAA proof（含账号 cookies 的加密信封）按形态特征
  （顶层超长字符串）截断存储，保留前缀与长度供形态比对，防快照被
  用作重放材料；
- 文件命名 ``req_*.upstream*.json`` 落在现有 dump 目录；已从
  ``_prune_dumps`` 的通配清理中排除（快照生命周期独立管理）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aistudio_api.infrastructure.utils.common import atomic_write_text
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("upstream-dump")

# 凭据类头：值不落盘（仅保留长度）；请求头与响应头共用同一集合
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "x-goog-api-key",
        "proxy-authorization",
        "set-cookie",
    }
)

# wire 请求体中 WAA proof：顶层槽位里的超长字符串（加密信封）。
# 槽位位置随 wire 格式演进（2026-11 实测已从 10 号槽移到 4 号槽），
# 因此不按位置识别，按「顶层槽位为超长字符串」这一形态特征截断。
_PROOF_MIN_CHARS = 256
_PROOF_HEAD_CHARS = 48

# 单次快照的响应样本上限（对齐 transport 流式错误采样的 64KiB）
_MAX_RESPONSE_SAMPLE = 65536

# 模型主调用 RPC 名（URL 末段）：成功快照只采集主调用
_MODEL_RPC_NAME = "GenerateContent"

# 成功快照默认保留条数
_DEFAULT_KEEP = 10
_KEEP_MAX = 1000

# 成功环修剪节流：写盘高峰（重试风暴）下避免每次写都全目录扫描
_PRUNE_INTERVAL = 5.0

_success_prune_lock = threading.Lock()
_last_success_prune = 0.0


def _sanitize_headers(
    headers: Mapping[str, str] | Sequence[tuple[str, str]],
) -> list[list[str]]:
    """把头集合脱敏为有序 ``[[name, value], ...]``（保留传入顺序）。"""
    items: list[tuple[str, str]] = (
        list(headers.items())
        if isinstance(headers, Mapping)
        else [(str(k), str(v)) for k, v in headers]
    )
    safe: list[list[str]] = []
    for name, value in items:
        text = str(value)
        if name.lower() in _SENSITIVE_HEADERS:
            safe.append([name, f"<redacted len={len(text)}>"])
        else:
            safe.append([name, text])
    return safe


# 快照侧解析上限（字符数）：成功快照默认开启（upstream_snapshot_keep=10），
# 长会话请求体随轮次涨到 MB 级后，json.loads 整树 + 后续 json.dumps 会在
# 线程池里再放大 3～4 倍正文，与请求路径本身的拷贝叠加把 512MB 档容器
# 的 RSS 顶过 OOM 阈值。超过上限只保留头部样本，不再整体解析。
_MAX_BODY_PARSE_CHARS = 256 * 1024
_BODY_HEAD_CHARS = 2000
_BODY_COUNT_CHUNK = 64 * 1024


def _utf8_len(text: str) -> int:
    """按 UTF-8 计算字节数，分块编码避免整段 .encode() 再复制一份正文。"""
    total = 0
    for start in range(0, len(text), _BODY_COUNT_CHUNK):
        total += len(text[start : start + _BODY_COUNT_CHUNK].encode("utf-8", "replace"))
    return total


def _sanitize_wire_body(body: str) -> Any:
    """解析 wire JSON 并截断各槽位中的 proof；非 JSON 原样截断返回。"""
    if len(body) > _MAX_BODY_PARSE_CHARS:
        return (
            f"<wire body truncated len={len(body)} "
            f"exceeds snapshot parse limit {_MAX_BODY_PARSE_CHARS}>"
            f" head={body[:_BODY_HEAD_CHARS]!r}"
        )
    try:
        arr = json.loads(body)
    except (ValueError, TypeError):
        return body[:_BODY_HEAD_CHARS]
    if isinstance(arr, list):
        for i, slot in enumerate(arr):
            if isinstance(slot, str) and len(slot) > _PROOF_MIN_CHARS:
                arr[i] = (
                    f"<proof truncated len={len(slot)} head={slot[:_PROOF_HEAD_CHARS]}>"
                )
    return arr


def _rpc_name_from_url(url: str) -> str:
    """从 RPC URL 提取末段方法名（去查询串）。"""
    path = url.split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else path


def _context_info() -> tuple[str, str, str]:
    """返回 (request_id, model, account_id)；无请求上下文的后台路径用占位。"""
    try:
        from aistudio_api.infrastructure.request_context import current_request

        context = current_request.get()
        if context is not None and getattr(context, "request_id", None):
            return (
                str(context.request_id),
                str(getattr(context, "model", "") or ""),
                str(getattr(context, "account_id", "") or ""),
            )
    except Exception:  # pragma: no cover - 防御性：无请求上下文的后台路径
        pass
    return f"req_bg_{int(time.time() * 1000)}", "", ""


def upstream_snapshot_keep() -> int:
    """成功快照保留条数：环境变量 → config.yaml logging 段 → 默认 10。"""
    raw = os.getenv("AISTUDIO_UPSTREAM_SNAPSHOT_KEEP", "").strip()
    if raw:
        try:
            return min(max(int(raw), 0), _KEEP_MAX)
        except ValueError:
            pass
    try:
        from aistudio_api.infrastructure.gateway.model_defaults import (
            get_configured_logging_settings,
        )

        value = get_configured_logging_settings().get("upstream_snapshot_keep")
        if isinstance(value, int) and not isinstance(value, bool):
            return min(max(value, 0), _KEEP_MAX)
        if isinstance(value, str) and value.strip().isdigit():
            return min(max(int(value.strip()), 0), _KEEP_MAX)
    except Exception:
        pass
    return _DEFAULT_KEEP


def _ok_marker_files(folder: Path) -> list[Path]:
    return [
        path
        for path in folder.glob("req_*.upstream.ok.json")
        if path.name.endswith(".upstream.ok.json")
    ]


def _err_marker_files(folder: Path) -> list[Path]:
    return [
        path
        for path in folder.glob("req_*.upstream.json")
        if path.name.endswith(".upstream.json")
    ]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prune_success_snapshots(folder: Path, keep: int, *, force: bool = False) -> int:
    """成功快照环形淘汰：仅保留最近 ``keep`` 条（0 = 清空全部成功快照）。

    返回删除的文件数。并发写盘来自线程池工作线程，用模块锁串行化；
    非强制调用按 ``_PRUNE_INTERVAL`` 节流。
    """
    global _last_success_prune
    removed = 0
    with _success_prune_lock:
        now = time.time()
        if not force and now - _last_success_prune < _PRUNE_INTERVAL:
            return 0
        _last_success_prune = now
        files = sorted(_ok_marker_files(folder), key=_mtime, reverse=True)
        for path in files[max(keep, 0) :]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def should_save_exchange(url: str, status: int) -> bool:
    """事件循环侧的廉价门禁：本次交换是否需要派发快照落盘线程。

    与 save_upstream_exchange 内部的跳过条件同源（辅助 RPC 成功无诊断
    价值、成功快照保留为 0 时全跳过），供调用方在构建 header 列表/字典
    与 to_thread 派发之前判断，避免每请求约 78µs 的空跑线程派发。
    """
    if not 200 <= status < 300:
        # 失败快照无条件保留（错误现场诊断价值最高，不受 keep 限制）。
        return True
    if _rpc_name_from_url(url) != _MODEL_RPC_NAME:
        return False
    return upstream_snapshot_keep() > 0


def save_upstream_exchange(
    *,
    url: str,
    wire_headers: Sequence[tuple[str, str]] | Mapping[str, str],
    body: str,
    status: int,
    response_sample: bytes | str,
    response_headers: Mapping[str, str] | None = None,
    response_http_version: str = "",
    elapsed_ms: float | None = None,
    proxy_url: str | None = None,
) -> Path | None:
    """保存上游交换快照（同步阻塞，调用方应放线程池）。

    写入规则见模块 docstring：主调用成功 → ok 文件 + 环形淘汰；主调用
    失败 → err 文件（覆盖旧尝试）；辅助 RPC 仅失败且不覆盖已有文件。
    返回写入的路径；跳过或失败返回 None（诊断功能不得影响请求路径）。
    """
    try:
        from aistudio_api.infrastructure.utils.logger import get_dump_dir

        rpc = _rpc_name_from_url(url)
        is_model_rpc = rpc == _MODEL_RPC_NAME
        success = 200 <= status < 300
        if success and not is_model_rpc:
            # 辅助 RPC（CountTokens 追随调用等）成功形态无诊断价值，
            # 且会顶掉同 request_id 的主调用成功快照——直接跳过。
            return None
        if success and upstream_snapshot_keep() <= 0:
            return None

        request_id, model, account_id = _context_info()
        if isinstance(response_sample, bytes):
            response_text = response_sample[:_MAX_RESPONSE_SAMPLE].decode(
                "utf-8", "replace"
            )
        else:
            response_text = response_sample[:_MAX_RESPONSE_SAMPLE]

        snapshot: dict[str, Any] = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": request_id,
            "kind": "upstream_exchange",
            "rpc": rpc,
            "success": success,
            "model": model,
            "account_id": account_id,
            "upstream": {
                "url": url,
                "method": "POST",
                "headers": _sanitize_headers(wire_headers),
                "wire_body": _sanitize_wire_body(body),
                "body_bytes": _utf8_len(body),
            },
            "proxy": (
                proxy_url.split("@")[-1] if proxy_url and "@" in proxy_url else None
            ),
            "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
            "response": {
                "status": status,
                "http_version": response_http_version,
                "headers": (
                    _sanitize_headers(response_headers)
                    if response_headers is not None
                    else []
                ),
                "body": response_text,
            },
        }

        dump_dir = get_dump_dir()
        if success:
            target = dump_dir / f"{request_id}.upstream.ok.json"
        else:
            target = dump_dir / f"{request_id}.upstream.json"
            if not is_model_rpc and target.exists():
                # 辅助 RPC 失败不得覆盖主调用（或先到失败现场）的快照。
                return None
        # 原子写（临时文件+rename）：SIGKILL 窗口留下的半截快照会让
        # 请求详情页的 json.loads 静默报“无上游交换快照”，恰好丢掉本模块
        # 要保留的错误现场。与同目录的请求 dump 落盘（logger.dump_request_
        # exchange → atomic_write_json private=True）同一契约。
        atomic_write_text(
            target,
            json.dumps(snapshot, ensure_ascii=False, indent=1),
            private=True,
        )
        if success:
            _prune_success_snapshots(dump_dir, upstream_snapshot_keep())
        return target
    except Exception as exc:
        logger.debug("上游交换快照保存失败: %s", exc)
        return None


def upstream_snapshot_stats() -> dict[str, object]:
    """当前快照库存：成功/错误条数、总字节与保留配置。"""
    try:
        from aistudio_api.infrastructure.utils.logger import get_dump_dir

        dump_dir = get_dump_dir()

        def measure(files: list[Path]) -> tuple[int, int]:
            total = 0
            for path in files:
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
            return len(files), total

        ok_count, ok_bytes = measure(_ok_marker_files(dump_dir))
        err_count, err_bytes = measure(_err_marker_files(dump_dir))
        return {
            "success": ok_count,
            "error": err_count,
            "bytes": ok_bytes + err_bytes,
            "keep": upstream_snapshot_keep(),
        }
    except Exception:
        return {"success": 0, "error": 0, "bytes": 0, "keep": _DEFAULT_KEEP}


def prune_upstream_snapshots() -> int:
    """强制执行一次成功快照环形淘汰（启动时/设置调低后调用）。"""
    try:
        from aistudio_api.infrastructure.utils.logger import get_dump_dir

        return _prune_success_snapshots(
            get_dump_dir(), upstream_snapshot_keep(), force=True
        )
    except Exception:
        return 0


def clear_upstream_snapshots(scope: str = "all") -> dict[str, int]:
    """手动清理快照：scope=all 全部 / scope=success 仅成功快照。"""
    removed = {"success": 0, "error": 0}
    try:
        from aistudio_api.infrastructure.utils.logger import get_dump_dir

        dump_dir = get_dump_dir()
        targets: list[tuple[str, Path]] = [
            ("success", path) for path in _ok_marker_files(dump_dir)
        ]
        if scope != "success":
            targets.extend(("error", path) for path in _err_marker_files(dump_dir))
        for kind, path in targets:
            try:
                path.unlink()
                removed[kind] += 1
            except OSError:
                continue
    except Exception as exc:
        logger.debug("清理上游快照失败: %s", exc)
    return removed
