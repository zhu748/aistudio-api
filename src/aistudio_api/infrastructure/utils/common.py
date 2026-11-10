"""Shared utility helpers."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import time
from pathlib import Path


def _atomic_write_text(target_path: Path, text: str, *, private: bool = False) -> None:
    """把 text 写入 target_path：先写同目录唯一临时文件，再原子替换。

    持久化保证（3.13）：写后 fsync 文件内容再 rename，rename 后 fsync
    父目录——掉电/系统崩溃时不会产生零字节目标文件或改名回滚，波及
    全部账号凭据与注册表的写入路径均由此受益。
    private=True 时临时文件与目标文件都收紧为 0600（仅属主可读写），
    避免多用户主机上 auth.json/meta.json 按 umask 默认全局可读。
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(f".tmp.{os.getpid()}_{time.time_ns()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if private:
            with contextlib.suppress(OSError):
                tmp_path.chmod(0o600)
        tmp_path.replace(target_path)
        if private:
            with contextlib.suppress(OSError):
                target_path.chmod(0o600)
        # 同步父目录使 rename 持久化（POSIX）；Windows/异常平台尽力而为
        dir_fd = None
        try:
            dir_fd = os.open(str(target_path.parent), os.O_RDONLY)
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            if dir_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(dir_fd)
    except Exception:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def cleanup_stale_tmp_files(directory: Path, *, pattern: str = "*.tmp.*") -> int:
    """清扫残留的原子写临时文件（SIGKILL 窗口遗留，可能含完整凭据）。

    仅删除与目标文件同目录、匹配 pattern 且修改时间超过 1 小时的临时文件，
    避免误删并发写入中的文件。返回删除数量。
    """
    removed = 0
    now = time.time()
    if not directory.is_dir():
        return 0
    for p in directory.glob(pattern):
        try:
            if now - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def atomic_write_json(
    path: Path | str, data: object, *, compact: bool = False, private: bool = False
) -> None:
    """原子写入 JSON 文件（写唯一临时文件后原子替换，防止写穿或损坏）。"""
    # 用 absolute() 而非 resolve()：Windows 上 resolve() 会逐段做符号链接与
    # 大小写归一化，实测约 100µs/次，而落盘是最频繁的写路径之一。
    serialized = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if compact
        else json.dumps(data, ensure_ascii=False, indent=2)
    )
    _atomic_write_text(Path(path).absolute(), serialized, private=private)


def atomic_write_text(path: Path | str, text: str, *, private: bool = False) -> None:
    """原子写入文本文件（写唯一临时文件后原子替换，防止写穿或残留半截配置）。"""
    _atomic_write_text(Path(path).absolute(), text, private=private)


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
