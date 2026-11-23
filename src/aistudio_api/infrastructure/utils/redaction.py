"""Redact credentials from free text, URL paths, queries and proxy userinfo."""

from __future__ import annotations

import re

# turn/turns/sstp 桥接 URI（含 RFC 7065 无斜线形态）同样会携带
# user:pass@host userinfo，此处与 http/socks5 一并遮蔽，避免 TURN
# 凭据在管理面板与日志里明文展示。
_URL_USERINFO = re.compile(
    r"(https?|socks5h?|turns?|sstp):(//)?[^\s/?@]+@", re.IGNORECASE
)
_KEY_PATH = re.compile(r"(/api-keys/)[^\s?\"'<>]+", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"([?&](?:key|api[_-]?key|token|ticket|password|secret|auth)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(Bearer\s+)[^\s,;\"'<>]+", re.IGNORECASE)
# 整行 Authorization 头（含本项目自产的 "SAPISIDHASH hash/timestamp" 与
# Basic 等任意 scheme）：_BEARER 只认 Bearer 前缀，其余 scheme 的完整
# 凭据会整段漏进日志与 /api/logs。
_AUTH_HEADER = re.compile(r"(Authorization:\s*)[^\r\n]+", re.IGNORECASE)
# 无 scheme 前缀的裸 userinfo（手工编辑 config.yaml 的非规范代理值可能
# 以 user:pass@host 形态存在；规范化写入路径恒补 scheme，仅读取侧兑底）。
# 要求 @ 后紧跟主机名形态，避免误伤普通邮箱/提及。
_BARE_USERINFO = re.compile(
    r"(?<![\w/.-])[\w.%+-]+:[\w.%+-]+@(?=[\w.-]+(?::\d{1,5})?(?:[/?\s\"'<>]|$))"
)
# Google 会话 Cookie 名（HSID/SSID/SID/APISID/SIDCC/NID）常以自由文本
# 出现在异常消息与上游响应片段中；SAPISID 变体此前已覆盖，其余漏网
_NAMED_SECRET = re.compile(
    r"((?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"SAPISID|__Secure-[13]P(?:A?P?I?SID(?:TS)?)|HSID|SSID|APISID|SIDCC|NID|"
    r"\bSID\b)\s*[=:]\s*)[^\s;,\"'<>]+",
    re.IGNORECASE,
)
_COOKIE_HEADER = re.compile(r"((?:Set-)?Cookie:\s*)[^\r\n]+", re.IGNORECASE)


def redact_text(value: str) -> str:
    text = _URL_USERINFO.sub(r"\1://***@", value)
    text = _KEY_PATH.sub(r"\1***", text)
    text = _QUERY_SECRET.sub(r"\1***", text)
    text = _AUTH_HEADER.sub(r"\1***", text)
    text = _BEARER.sub(r"\1***", text)
    text = _COOKIE_HEADER.sub(r"\1***", text)
    text = _BARE_USERINFO.sub("***@", text)
    return _NAMED_SECRET.sub(r"\1***", text)
