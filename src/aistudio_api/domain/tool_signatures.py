"""服务端工具调用的 thought_signature 校验与防护。

上游 AI Studio 对「服务端执行的工具」的 functionCall 回传强制校验
thoughtSignature（缺失或非法 base64 直接 400 INVALID_ARGUMENT：
"Function call is missing a thought_signature in functionCall parts"）。

触发场景：开启 Saved Info（记忆）等账号级功能后，上游会给请求动态注入
服务端工具（如 ``default_api:context`` 记忆保存调用）；模型在响应中调用
它们时携带签名。而 OpenAI Chat Completions / Claude Messages 兼容协议
没有官方工具签名字段，客户端（酒馆 SillyTavern 等）保存历史时必然丢失
签名，下一轮回传即触发上游 400，整轮请求被拒。

防护策略（application 层三条输入路径统一应用）：
1. 服务端工具调用无有效签名 → 剥离 functionCall part 及配对的
   functionResponse（服务端工具由上游执行，Saved Info 状态已在上游
   持久化，剥离仅丢掉"模型曾调用过"的历史事实，不破坏对话语义）；
2. 普通客户端工具（functionDeclarations 声明）缺失签名上游宽容
   （实测 200），不受影响；但非法 base64 签名会被上游拒为
   "Base64 decoding failed"（thought_signature 字段类型 TYPE_BYTES），
   因此对签名统一做 base64 有效性过滤。
"""

from __future__ import annotations

import base64

# default_api:* 是 AI Studio 服务端工具命名空间
# （如 default_api:context = Saved Info 记忆保存调用）
_SERVER_SIDE_TOOL_PREFIX = "default_api:"

# 内置工具在 functionCall 中的调用名（上游注入、服务端执行）
_SERVER_SIDE_TOOL_NAMES = frozenset(
    {
        "google_search",
        "url_context",
        "code_execution",
        "google_maps",
        "context",
    }
)


def is_server_side_tool_call(name: str) -> bool:
    """判断函数名是否为上游服务端执行的内置工具调用。"""
    normalized = name.strip().lower()
    return (
        normalized.startswith(_SERVER_SIDE_TOOL_PREFIX)
        or normalized in _SERVER_SIDE_TOOL_NAMES
    )


def valid_thought_signature(signature: object) -> bool:
    """校验 thoughtSignature 为非空合法 base64。

    上游 thought_signature 字段类型为 TYPE_BYTES：非法 base64 透传会被
    拒为 400 "Base64 decoding failed"，与缺失签名同等处理。
    """
    if not isinstance(signature, str) or not signature:
        return False
    try:
        base64.b64decode(signature, validate=True)
    except (ValueError, UnicodeEncodeError):
        return False
    return True


def valid_thought_signature_or_none(signature: object) -> str | None:
    """签名合法则原样返回，否则返回 None（丢弃签名字段本身）。"""
    if valid_thought_signature(signature):
        assert isinstance(signature, str)
        return signature
    return None
