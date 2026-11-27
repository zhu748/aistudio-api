"""Redact credentials from free text, URL paths, queries and proxy userinfo."""

from __future__ import annotations

import logging
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


# 记录级脱敏标记：同一条 LogRecord 会被 5 个落点各扫一遍（内存环、控制台
# 格式器、文件 ContextFilter、文件 JsonFormatter）。8 条正则在 85 字符消息
# 上约 17.6 µs、6 KB 转储消息上约 1.07 ms，重复扫描纯属浪费；脱敏是幂等
# 的，扫一次后打标记，其余落点直接跳过。
_REDACTED_FLAG = "_aistudio_redacted"


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """就地脱敏记录正文并补齐请求上下文（幂等，可重复调用）。

    调用方：内存环 handler、控制台格式器、文件日志的 ContextFilter 与
    JsonFormatter。首次调用把 ``record.msg`` 换成脱敏后的完整消息（
    ``args`` 清空，避免后续再次格式化），预先把异常尾文本脱敏进
    ``record.exc_text``（logging.Formatter 会复用它，不会再走
    formatException 产出未脱敏文本），并写入请求上下文字段；之后任何
    落点再次调用都只是读标记直接返回。
    """
    if record.__dict__.get(_REDACTED_FLAG):
        return record
    # 与 request_context.log_context() 同构；在此延迟导入避免模块级循环依赖
    # （request_context 自身依赖本模块的 redact_text）。
    from aistudio_api.infrastructure.request_context import log_context

    record.msg = redact_text(record.getMessage())
    record.args = ()
    if record.exc_info and not record.exc_text:
        record.exc_text = redact_text(
            logging.Formatter().formatException(record.exc_info)
        )
    for key, value in log_context().items():
        if not getattr(record, key, ""):
            setattr(record, key, value)
    record.__dict__[_REDACTED_FLAG] = True
    return record
