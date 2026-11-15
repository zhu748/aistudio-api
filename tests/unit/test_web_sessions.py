"""持久会话令牌（/auth/session → 浏览器重开免密登录）的单元测试。

背景：3.17 起前端把令牌放 sessionStorage，浏览器重开即失效，用户每次
都要重输管理密码。现在后端签发与管理密码分离的随机会话令牌：30 天
过期、换密码（指纹失配）即全部失效、落盘重启不丢。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

from aistudio_api.api.dependencies import require_web_auth, verify_web_login_or_401
from aistudio_api.config import settings
from aistudio_api.infrastructure import web_sessions as ws


@pytest.fixture()
def session_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """隔离会话存储：独立数据目录 + 清空进程内状态（模拟冷启动）。"""
    monkeypatch.setattr(ws, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ws, "_loaded", False)
    monkeypatch.setattr(ws, "_sessions", {})
    return tmp_path


def _make_request(path: str, headers: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock(spec=Request)
    req.method = "GET"
    req.url = MagicMock()
    req.url.path = path
    req.query_params = {}
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ───────────── 基础语义 ─────────────


def test_issue_and_validate_roundtrip(session_env) -> None:
    token, expires = ws.issue_web_session("admin_pass_1")
    assert token
    assert expires > time.time()
    # 30 天有效期
    assert expires - time.time() == pytest.approx(ws.SESSION_TTL_SECONDS, abs=5)
    assert ws.validate_web_session(token, "admin_pass_1") is True


def test_validate_rejects_wrong_or_empty_token(session_env) -> None:
    token, _ = ws.issue_web_session("admin_pass_1")
    assert ws.validate_web_session("not-a-session", "admin_pass_1") is False
    assert ws.validate_web_session("", "admin_pass_1") is False
    assert ws.validate_web_session(f"{token} ", "admin_pass_1") is True  # 容忍空白


def test_password_change_invalidates_sessions(session_env) -> None:
    """会话与签发时的密码指纹绑定：换密码后旧会话全部失效。"""
    token, _ = ws.issue_web_session("old_password")
    assert ws.validate_web_session(token, "old_password") is True
    assert ws.validate_web_session(token, "new_password") is False


def test_expired_session_is_rejected_and_pruned(
    monkeypatch: pytest.MonkeyPatch, session_env
) -> None:
    token, _ = ws.issue_web_session("admin_pass_1")
    monkeypatch.setattr(ws, "SESSION_TTL_SECONDS", 0.01)
    short_token, _ = ws.issue_web_session("admin_pass_1")
    time.sleep(0.05)
    # 新签发的未过期会话仍有效（TTL 在签发时已确定）
    assert ws.validate_web_session(token, "admin_pass_1") is True
    assert ws.validate_web_session(short_token, "admin_pass_1") is False
    # 过期条目在下次写操作时被清理
    ws.issue_web_session("admin_pass_1")
    store = json.loads((session_env / "web_sessions.json").read_text(encoding="utf-8"))
    assert short_token not in store["sessions"]
    assert token in store["sessions"]


# ───────────── 持久化（服务重启不丢） ─────────────


def test_sessions_survive_process_restart(session_env) -> None:
    token, _ = ws.issue_web_session("admin_pass_1")
    # 模拟进程重启：进程内状态全部清零，从磁盘重新加载
    ws._loaded = False  # pyright: ignore[reportPrivateUsage]
    ws._sessions = {}  # pyright: ignore[reportPrivateUsage]
    assert ws.validate_web_session(token, "admin_pass_1") is True


def test_session_store_is_bounded(monkeypatch: pytest.MonkeyPatch, session_env) -> None:
    monkeypatch.setattr(ws, "SESSION_MAX", 4)
    issued = [ws.issue_web_session("admin_pass_1")[0] for _ in range(6)]
    # 溢出丢最旧（按过期时间）：最早的两枚被挤出
    assert ws.validate_web_session(issued[0], "admin_pass_1") is False
    assert ws.validate_web_session(issued[1], "admin_pass_1") is False
    assert ws.validate_web_session(issued[-1], "admin_pass_1") is True


def test_revoke_all(session_env) -> None:
    token, _ = ws.issue_web_session("admin_pass_1")
    assert ws.revoke_all_web_sessions() == 1
    assert ws.validate_web_session(token, "admin_pass_1") is False


# ───────────── 与管理端鉴权栈的集成 ─────────────


def test_require_web_auth_accepts_session_token(session_env) -> None:
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "web_password", "super_secret_admin_pass")
        token, _ = ws.issue_web_session("super_secret_admin_pass")
        # 不抛异常即视为通过
        require_web_auth(_make_request("/api/stats", _bearer(token)))
        # 密码直连依旧可用（兼容 API 客户端）
        require_web_auth(
            _make_request("/api/stats", _bearer("super_secret_admin_pass"))
        )
    finally:
        monkey.undo()


def test_require_web_auth_rejects_foreign_session_token(session_env) -> None:
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "web_password", "super_secret_admin_pass")
        ws.issue_web_session("super_secret_admin_pass")
        with pytest.raises(HTTPException) as exc_info:
            require_web_auth(_make_request("/api/stats", _bearer("random-token")))
        assert exc_info.value.status_code == 401
    finally:
        monkey.undo()


def test_verify_web_login_or_401_password_mode(session_env) -> None:
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "web_password", "super_secret_admin_pass")
        assert (
            verify_web_login_or_401("super_secret_admin_pass", "127.0.0.1")
            == "password"
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_web_login_or_401("wrong_password", "127.0.0.1")
        assert exc_info.value.status_code == 401
    finally:
        monkey.undo()


def test_verify_web_login_or_401_api_key_only_mode(session_env) -> None:
    """仅 API Key 模式：核验通过返回 api_key，前端回退为持久化该 Key。"""
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "web_password", "")
        monkey.setattr(settings, "api_keys", frozenset({"secret-token"}))
        assert verify_web_login_or_401("secret-token", "127.0.0.1") == "api_key"
        with pytest.raises(HTTPException):
            verify_web_login_or_401("not-the-key", "127.0.0.1")
    finally:
        monkey.undo()
