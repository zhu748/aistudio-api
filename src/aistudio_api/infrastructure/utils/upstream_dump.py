"""上游失败请求的完整交换快照（URL / 头 / 线缆体 / 响应样本）。

用途：上游 400/403 等拒绝发生时，把「实际发往 AI Studio 的最终请求」
落盘为 ``<dump_dir>/<request_id>.upstream.json``，供请求详情页查看与
下载（排除「客户端侧原始请求 vs 网关转换后 wire 请求」的歧义——
2026-11-14 排查 403/400 时两次都需要人工 dump 才能定位线缆形态）。

安全边界：
- 凭据头（authorization/cookie/x-goog-api-key）只保留长度，值不落盘；
- wire 体槽位 10 的 WAA proof（含账号 cookies 的加密信封）截断存储，
  保留前缀与长度供形态比对，防快照被用作重放材料；
- 文件命名 ``req_*.upstream.json`` 落在现有 dump 目录，复用
  ``_prune_dumps`` 的数量/时长/总量清理。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("upstream-dump")

# 凭据类头：值不落盘（仅保留长度）
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "x-goog-api-key", "proxy-authorization"}
)

# wire 请求体中 WAA proof 的槽位
_PROOF_SLOT = 10
_PROOF_HEAD_CHARS = 48

# 单次快照的响应样本上限（对齐 transport 流式错误采样的 64KiB）
_MAX_RESPONSE_SAMPLE = 65536


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _SENSITIVE_HEADERS:
            safe[name] = f"<redacted len={len(value)}>"
        else:
            safe[name] = str(value)
    return safe


def _sanitize_wire_body(body: str) -> Any:
    """解析 wire JSON 并截断 proof 槽位；非 JSON 原样截断返回。"""
    try:
        arr = json.loads(body)
    except (ValueError, TypeError):
        return body[:2000]
    if isinstance(arr, list) and len(arr) > _PROOF_SLOT:
        proof = arr[_PROOF_SLOT]
        if isinstance(proof, str) and proof:
            arr[_PROOF_SLOT] = (
                f"<proof truncated len={len(proof)} head={proof[:_PROOF_HEAD_CHARS]}>"
            )
    return arr


def _resolve_request_id() -> str:
    try:
        from aistudio_api.infrastructure.request_context import current_request

        context = current_request.get()
        if context is not None and getattr(context, "request_id", None):
            return str(context.request_id)
    except Exception:  # pragma: no cover - 防御性：无请求上下文的后台路径
        pass
    return f"req_bg_{int(time.time() * 1000)}"


def save_failed_upstream_exchange(
    *,
    url: str,
    headers: dict[str, str],
    body: str,
    status: int,
    response_sample: bytes | str,
    proxy_url: str | None = None,
) -> Path | None:
    """上游非 200 响应时保存完整交换快照（同步阻塞，调用方应放线程池）。

    返回写入的路径；任何失败都静默降级（诊断功能不得影响请求路径）。
    """
    try:
        from aistudio_api.infrastructure.utils.logger import get_dump_dir

        request_id = _resolve_request_id()
        if isinstance(response_sample, bytes):
            response_text = response_sample[:_MAX_RESPONSE_SAMPLE].decode(
                "utf-8", "replace"
            )
        else:
            response_text = response_sample[:_MAX_RESPONSE_SAMPLE]

        snapshot = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": request_id,
            "kind": "upstream_exchange",
            "upstream": {
                "url": url,
                "method": "POST",
                "headers": _sanitize_headers(headers),
                "wire_body": _sanitize_wire_body(body),
                "body_bytes": len(body.encode("utf-8", "replace")),
            },
            "proxy": (
                proxy_url.split("@")[-1] if proxy_url and "@" in proxy_url else None
            ),
            "response": {"status": status, "body": response_text},
        }

        target = get_dump_dir() / f"{request_id}.upstream.json"
        target.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return target
    except Exception as exc:
        logger.debug("上游交换快照保存失败: %s", exc)
        return None
