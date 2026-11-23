"""compat_errors 三张映射表的逐键穷举回归。

原先测试只抽查约 8 个键：向 dict 新增/改错枚举映射不会有任何测试失败
（OTHER→length 那类语义修复曾经出问题的区域）。
"""

from __future__ import annotations

import pytest

from aistudio_api.application import compat_errors

_OPENAI_FINISH_VALUES = {"stop", "length", "tool_calls", "content_filter"}
_OPENAI_ERROR_TYPES = {
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "rate_limit_error",
    "server_error",
    "api_error",
    "internal_error",
    "overloaded_error",
}
_CLAUDE_ERROR_TYPES = {
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "rate_limit_error",
    "api_error",
    "overloaded_error",
    "request_too_large",
}


@pytest.mark.parametrize("finish", sorted(compat_errors._GEMINI_TO_OPENAI_FINISH))
def test_gemini_to_openai_finish_map_values(finish: str):
    mapped = compat_errors._GEMINI_TO_OPENAI_FINISH[finish]
    assert mapped in _OPENAI_FINISH_VALUES, (
        f"finishReason {finish!r} 映射到未知 finish_reason {mapped!r}"
    )


@pytest.mark.parametrize("code", sorted(compat_errors._OPENAI_ERROR_TYPE))
def test_openai_error_type_map_values(code: int):
    mapped = compat_errors._OPENAI_ERROR_TYPE[code]
    assert mapped in _OPENAI_ERROR_TYPES, (
        f"HTTP {code} 映射到未知 OpenAI 错误类型 {mapped!r}"
    )


@pytest.mark.parametrize("code", sorted(compat_errors._CLAUDE_ERROR_TYPE))
def test_claude_error_type_map_values(code: int):
    mapped = compat_errors._CLAUDE_ERROR_TYPE[code]
    assert mapped in _CLAUDE_ERROR_TYPES, (
        f"HTTP {code} 映射到未知 Claude 错误类型 {mapped!r}"
    )


def test_common_status_codes_present():
    # 冷门但语义固定的状态码必须存在映射（413/429/5xx 等）
    assert 413 in compat_errors._OPENAI_ERROR_TYPE
    assert 429 in compat_errors._OPENAI_ERROR_TYPE
    assert compat_errors._OPENAI_ERROR_TYPE[429] == "rate_limit_error"
    assert compat_errors._CLAUDE_ERROR_TYPE[429] == "rate_limit_error"
    assert compat_errors._CLAUDE_ERROR_TYPE[413] == "request_too_large"
    for code in (500, 502, 503):
        assert code in compat_errors._OPENAI_ERROR_TYPE
        assert code in compat_errors._CLAUDE_ERROR_TYPE


def test_safety_finish_reasons_map_to_content_filter():
    for reason in ("SAFETY", "PROHIBITED_CONTENT", "RECITATION"):
        assert compat_errors._GEMINI_TO_OPENAI_FINISH[reason] == "content_filter", (
            reason
        )
