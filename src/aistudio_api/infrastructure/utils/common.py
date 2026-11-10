"""Shared utility helpers."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import time
from pathlib import Path


def _atomic_write_text(target_path: Path, text: str) -> None:
    """把 text 写入 target_path：先写同目录唯一临时文件，再原子替换。"""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(f".tmp.{os.getpid()}_{time.time_ns()}")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(target_path)
    except Exception:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def atomic_write_json(path: Path | str, data: object, *, compact: bool = False) -> None:
    """原子写入 JSON 文件（写唯一临时文件后原子替换，防止写穿或损坏）。"""
    # 用 absolute() 而非 resolve()：Windows 上 resolve() 会逐段做符号链接与
    # 大小写归一化，实测约 100µs/次，而落盘是最频繁的写路径之一。
    serialized = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if compact
        else json.dumps(data, ensure_ascii=False, indent=2)
    )
    _atomic_write_text(Path(path).absolute(), serialized)


def atomic_write_text(path: Path | str, text: str) -> None:
    """原子写入文本文件（写唯一临时文件后原子替换，防止写穿或残留半截配置）。"""
    _atomic_write_text(Path(path).absolute(), text)


def extract_outer_json(raw: str) -> list[object]:
    stripped = raw.strip()
    if not stripped:
        return []

    if stripped.startswith(")]}'"):
        stripped = stripped[4:].lstrip()

    try:
        return [json.loads(stripped)]
    except json.JSONDecodeError:
        pass

    results = []
    for line in stripped.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results


def decode_base64_images(images: list[dict[str, object]]) -> list[dict[str, object]]:
    decoded: list[dict[str, object]] = []
    for img in images:
        try:
            raw = img["data"]
            # str(bytes) produces "b'...'" which is NOT valid base64 payload;
            # only encode genuine text to bytes, pass real bytes through unchanged.
            data = base64.b64decode(
                raw if isinstance(raw, bytes) else str(raw).encode("ascii"),
                validate=False,
            )
        except (KeyError, TypeError, ValueError, binascii.Error):
            continue
        decoded.append(
            {
                "mime": str(img.get("mime", "image/jpeg")),
                "bytes": data,
                "size": len(data),
            }
        )
    return decoded


def mask_email(email: str | None) -> str:
    """对邮箱进行脱敏（如 user123@gmail.com -> u***3@gmail.com）。"""
    if not email or "@" not in email:
        return str(email or "")
    name, _, domain = email.partition("@")
    masked_name = name[0] + "***" if len(name) <= 2 else name[0] + "***" + name[-1]
    return f"{masked_name}@{domain}"
