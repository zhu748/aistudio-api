"""Cookie 导入时的代理地址识别与拆分测试。

历史行为：代理只能通过环境变量注入（`AISTUDIO_COOKIES` 的 `代理 + Cookie`
写法），网页导入只能先导入 Cookie、再单独设置代理。现在导入接口支持显式
`proxy_url`，也支持把代理直接写进粘贴的 Cookie 文本里自动识别。
"""

from __future__ import annotations

import pytest

from aistudio_api.infrastructure.account.cookie_parser import (
    normalize_proxy_url,
    split_proxy_from_paste,
)

_COOKIE = "SID=sid_value; SAPISID=sapisid_value; HSID=hsid_value"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("proxy.example.com:8080", "http://proxy.example.com:8080"),
        ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
        ("socks5://user:pass@127.0.0.1:1080", "socks5://user:pass@127.0.0.1:1080"),
        ("user:pass@10.0.0.2:1080", "http://user:pass@10.0.0.2:1080"),
        ("10.0.0.2:1080:user:pass", "http://user:pass@10.0.0.2:1080"),
    ],
)
def test_normalize_proxy_url_accepts_common_formats(raw: str, expected: str) -> None:
    assert normalize_proxy_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not a proxy", "SID=xxx", "http://"])
def test_normalize_proxy_url_rejects_unrecognised(raw: str) -> None:
    assert normalize_proxy_url(raw) is None


def test_split_proxy_before_cookie_with_plus() -> None:
    proxy, cookies = split_proxy_from_paste(f"socks5://127.0.0.1:1080+{_COOKIE}")
    assert proxy == "socks5://127.0.0.1:1080"
    assert "SID=sid_value" in cookies


def test_split_proxy_after_cookie_with_plus() -> None:
    proxy, cookies = split_proxy_from_paste(f"{_COOKIE}+127.0.0.1:7890")
    assert proxy == "http://127.0.0.1:7890"
    assert cookies == _COOKIE


def test_split_proxy_on_its_own_line_before_cookie() -> None:
    proxy, cookies = split_proxy_from_paste(f"socks5://u:p@1.2.3.4:1080\n{_COOKIE}")
    assert proxy == "socks5://u:p@1.2.3.4:1080"
    assert "SAPISID=sapisid_value" in cookies


def test_split_proxy_on_its_own_line_after_cookie() -> None:
    proxy, cookies = split_proxy_from_paste(f"{_COOKIE}\n1.2.3.4:1080:user:pass")
    assert proxy == "http://user:pass@1.2.3.4:1080"
    assert "SID=sid_value" in cookies


def test_split_proxy_with_label() -> None:
    proxy, cookies = split_proxy_from_paste(f"代理: socks5://127.0.0.1:1080\n{_COOKIE}")
    assert proxy == "socks5://127.0.0.1:1080"
    assert "SID=sid_value" in cookies


def test_split_proxy_space_separated_at_tail() -> None:
    proxy, cookies = split_proxy_from_paste(f"{_COOKIE} socks5://127.0.0.1:1080")
    assert proxy == "socks5://127.0.0.1:1080"
    assert "SID=sid_value" in cookies


def test_no_proxy_returns_original_text() -> None:
    proxy, cookies = split_proxy_from_paste(_COOKIE)
    assert proxy is None
    assert cookies == _COOKIE


def test_json_payload_is_never_split() -> None:
    payload = '[{"name": "SID", "value": "abc"}]'
    proxy, cookies = split_proxy_from_paste(payload)
    assert proxy is None
    assert cookies == payload
