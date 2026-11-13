"""Redact credentials from free text, URL paths, queries and proxy userinfo."""

from __future__ import annotations

import re

_URL_USERINFO = re.compile(r"(https?|socks5h?)://[^\s/@]+@", re.IGNORECASE)
_KEY_PATH = re.compile(r"(/api-keys/)[^\s?\"'<>]+", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"([?&](?:key|api[_-]?key|token|ticket|password|secret|auth)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(Bearer\s+)[^\s,;\"'<>]+", re.IGNORECASE)
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
    text = _BEARER.sub(r"\1***", text)
    text = _COOKIE_HEADER.sub(r"\1***", text)
    return _NAMED_SECRET.sub(r"\1***", text)
