"""管理面板持久会话令牌（浏览器重开免密登录）。

历史（3.17）：前端把管理令牌存进 sessionStorage，浏览器重开即失效，
用户每次打开面板都要重输密码。本模块让后端签发**与管理密码分离**的
随机会话令牌：前端持久化在 localStorage 的是可过期（默认 30 天）、
换密码即全部失效、可服务端撤销的会话令牌，而非管理密码本身——保留
3.17 的安全考量（XSS 窃取的是可撤销凭据而非长期密码），只去掉反复
登录的体验损耗。

会话落盘在数据目录（web_sessions.json），服务重启不丢；原子写 +
0600 权限与其他持久化文件一致。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from typing import TypedDict

from aistudio_api.config import resolve_data_dir
from aistudio_api.infrastructure.utils.common import atomic_write_json

# 会话有效期：30 天。到期后前端下一次 verify 401 → 清令牌 → 重新登录。
SESSION_TTL_SECONDS = 30 * 86400.0
# 单机面板不需要无限会话：封顶 64 个（多设备/多浏览器），溢出丢最旧。
SESSION_MAX = 64


class _SessionMeta(TypedDict):
    expires_at: float
    fingerprint: str


_lock = threading.Lock()
_loaded = False
_sessions: dict[str, _SessionMeta] = {}


def _store_path() -> Path:
    return resolve_data_dir() / "web_sessions.json"


def password_fingerprint(password: str) -> str:
    """管理密码指纹：会话与其签发时的密码绑定，换密码即全部失效。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_store_path().read_text(encoding="utf-8"))
    except Exception:
        return
    entries = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return
    now = time.time()
    for token, meta in entries.items():
        if not isinstance(token, str) or not isinstance(meta, dict):
            continue
        expires = meta.get("expires_at")
        fingerprint = meta.get("fingerprint")
        if not isinstance(expires, (int, float)) or not isinstance(fingerprint, str):
            continue
        if expires <= now:
            continue
        _sessions[token] = {
            "expires_at": float(expires),
            "fingerprint": fingerprint,
        }


def _persist_locked() -> None:
    payload = {"version": 1, "sessions": _sessions}
    # private=True：令牌是可登录凭据，临时文件与最终文件均按 0600 收紧
    # （与其余凭据文件同规），替换后无需二次 chmod——原先先写成默认
    # 权限再 chmod，存在一瞬全局可读窗口。
    # 持久化失败不阻断登录：会话退化为进程内存活（重启需重登）。
    with contextlib.suppress(Exception):
        atomic_write_json(_store_path(), payload, private=True)


def issue_web_session(password: str) -> tuple[str, float]:
    """签发新会话令牌，返回 (token, expires_at)。调用方必须已核验密码。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + SESSION_TTL_SECONDS
    with _lock:
        _load_locked()
        # 顺带清理过期会话，防止长期运行下文件无界增长
        for key in [k for k, meta in _sessions.items() if meta["expires_at"] <= now]:
            _sessions.pop(key, None)
        while len(_sessions) >= SESSION_MAX:
            oldest = min(_sessions, key=lambda k: _sessions[k]["expires_at"])
            _sessions.pop(oldest, None)
        _sessions[token] = {
            "expires_at": expires,
            "fingerprint": password_fingerprint(password),
        }
        _persist_locked()
    return token, expires


def validate_web_session(token: str, password: str) -> bool:
    """校验会话令牌：存在、未过期、且与当前管理密码指纹匹配。"""
    clean = (token or "").strip()
    if not clean:
        return False
    fingerprint = password_fingerprint(password)
    with _lock:
        _load_locked()
        meta = _sessions.get(clean)
        if meta is None:
            return False
        if meta["expires_at"] <= time.time():
            _sessions.pop(clean, None)
            _persist_locked()
            return False
        # 非 str 比较：指纹定长十六进制，异常输入直接不匹配
        if meta["fingerprint"] != fingerprint:
            return False
    return True


def revoke_all_web_sessions() -> int:
    """撤销全部会话（换密码/安全事件时由运维手动触发或换密码自动失效）。"""
    with _lock:
        _load_locked()
        count = len(_sessions)
        _sessions.clear()
        if count:
            _persist_locked()
    return count
