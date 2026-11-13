"""X-Goog-Ext-519733851-bin 静态模板与官网动态派生的等价性回归锁。

背景（2026-11-13 活体实测，docs/REQUEST_BEHAVIOR_ALIGNMENT.md §2）：
真实页面每次加载调用 GetLoggingContext，把响应 JSPB 按字段序号（=下标+1）
编码为 protobuf 再 base64 得到该头。US 地区实测：

    响应 JSPB:  [0,"B",null,null,null,1,4,0,null,4,3,"US",null,0,1,null,null,0,1]
    动态派生:   CAASAUIwATgEQABQBFgDYgJVU3AAeAGQAQCYAQE=
    项目静态:   CAASAUIwATgEQABQBFgDYgJVU3AAeAGQAQCYAQE=   （逐字节一致）

本测试锁定该等价关系：若 Google 变更 GetLoggingContext 形态，重跑活体派生
即可在此处显式失败，避免静态模板静默漂移。
"""

from __future__ import annotations

import base64

from aistudio_api.infrastructure.account.cookie_parser import build_ext_bin

# 2026-11-13 GetLoggingContext 实测响应（US 地区，Pro 账号，代理出口）
LIVE_LOGGING_CONTEXT_JSPB = (
    '[0,"B",null,null,null,1,4,0,null,4,3,"US",null,0,1,null,null,0,1]'
)
LIVE_DERIVED_EXT = "CAASAUIwATgEQABQBFgDYgJVU3AAeAGQAQCYAQE="


def _varint(value: int) -> bytes:
    out = b""
    while value >= 0x80:
        out += bytes([value & 0x7F | 0x80])
        value >>= 7
    return out + bytes([value])


def _encode_jspb_fields(payload: str) -> bytes:
    """复刻 Go encodeJSPBFields：JSPB 数组 → protobuf（字段号 = 下标 + 1）。"""
    import json

    fields = json.loads(payload)
    out = b""
    for index, field in enumerate(fields):
        number = index + 1
        if field is None:
            continue
        if isinstance(field, str):
            out += _varint(number << 3 | 2) + _varint(len(field)) + field.encode()
        elif isinstance(field, bool):
            out += _varint(number << 3) + _varint(1 if field else 0)
        else:
            out += _varint(number << 3) + _varint(int(field))
    return out


def test_static_ext_bin_matches_live_derivation() -> None:
    """项目静态模板必须与官网动态派生逐字节一致（US 地区实测锚点）。"""
    assert build_ext_bin("US") == LIVE_DERIVED_EXT


def test_jspb_encoder_reproduces_static_template() -> None:
    """JSPB→protobuf→base64 派生链必须复现静态模板（形态变更探测器）。"""
    encoded = _encode_jspb_fields(LIVE_LOGGING_CONTEXT_JSPB)
    assert base64.b64encode(encoded).decode() == build_ext_bin("US")


def test_region_swap_only_changes_field_12() -> None:
    """换地区仅改 f12（62 02 <REG>），其余字节不变。"""
    us = base64.b64decode(build_ext_bin("US"))
    cn = base64.b64decode(build_ext_bin("CN"))
    # 前缀 16 字节后是 f12 标记 62 02（偏移 15-16），地区码在偏移 17-18：
    assert us[15] == 0x62 and us[16] == 0x02
    assert us[17:19] == b"US" and cn[17:19] == b"CN"
    assert us[:17] == cn[:17]
    assert us[19:] == cn[19:]
    assert len(us) == len(cn) == 29
