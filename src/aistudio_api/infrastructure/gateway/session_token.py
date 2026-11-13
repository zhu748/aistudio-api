"""会话令牌（wire 槽 11 / 响应 chunk 槽 7）传播缓存。

真实 AI Studio 浏览器行为（2026-11 抓包 + 04 号实验实证）：
- 每个 GenerateContent 响应 chunk 的 slot 7 携带 ``v1_...`` 会话令牌
  （同一页面会话内所有 chunk 恒同值）；
- 浏览器下一轮请求把该令牌回传到请求体 wire 槽 11；
- 携带与否不影响鉴权与内容（prompt_tokens 无变化），是会话续接/遥测
  信号——纯行为对齐项。

本项目按「账号作用域」缓存最新令牌（与 X-AIStudio-Visit-Id 的会话化
作用域一致），在 client.prepare_request 注入 wire 槽 11。
"""

from __future__ import annotations

import re

# 令牌形态校验：v1_ + base64 变体（实测 48+ 字符），拒绝把响应里其它
# v1_ 前缀串（如 visit-id 头值若被误传入）当作令牌。
_SESSION_TOKEN_RE = re.compile(r"^v1_[A-Za-z0-9+/=_-]{16,}$")
_MIN_TOKEN_LEN = 20

_SESSION_TOKEN_CACHE: dict[str, str] = {}


def _normalize_scope(scope: str | None) -> str:
    return scope or "default"


def record_session_token(scope: str | None, token: object) -> bool:
    """记录账号作用域下响应携带的最新会话令牌（非法形态静默拒绝）。"""
    value = token if isinstance(token, str) else ""
    if len(value) < _MIN_TOKEN_LEN or not _SESSION_TOKEN_RE.match(value):
        return False
    key = _normalize_scope(scope)
    if _SESSION_TOKEN_CACHE.get(key) == value:
        return False
    _SESSION_TOKEN_CACHE[key] = value
    return True


def get_session_token(scope: str | None) -> str | None:
    """读取账号作用域下最近一次响应携带的会话令牌（无则 None）。"""
    return _SESSION_TOKEN_CACHE.get(_normalize_scope(scope))


def account_session_token_scope(auth_file: str | None) -> str:
    """账号作用域键（与 visit-id 的 account: 前缀约定一致）。"""
    return f"account:{auth_file or 'default'}"


def clear_session_tokens() -> int:
    """清空全部缓存（测试/账号重置用），返回清除条数。"""
    count = len(_SESSION_TOKEN_CACHE)
    _SESSION_TOKEN_CACHE.clear()
    return count


__all__ = [
    "account_session_token_scope",
    "clear_session_tokens",
    "get_session_token",
    "record_session_token",
]
