"""Cookie 导入时的代理地址识别与拆分测试。

历史行为：代理只能通过环境变量注入（`AISTUDIO_COOKIES` 的 `代理 + Cookie`
写法），网页导入只能先导入 Cookie、再单独设置代理。现在导入接口支持显式
`proxy_url`，也支持把代理直接写进粘贴的 Cookie 文本里自动识别。

2026-11 扩展：代理协议在 http/https/socks5/socks5h 之外新增 TURN / TURNS /
SSTP（RFC 7065 无斜线写法、query 高级参数、默认端口与凭据约束），由
proxybridge 桥接器转换为本地 SOCKS5。
"""

from __future__ import annotations

import pytest

from aistudio_api.infrastructure.account.cookie_parser import (
    ProxyFormatError,
    build_bridge_proxy_uri,
    normalize_proxy_url,
    parse_bridge_proxy_uri,
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # RFC 7065 无斜线写法（含大写协议头与 query 参数）
        (
            "turn:1.2.3.4:3478?transport=tcp",
            "turn://1.2.3.4:3478",
        ),
        (
            "TURN:user:pass@turn.example.com?transport=tcp",
            "turn://user:pass@turn.example.com:3478",
        ),
        # 默认端口：turn 3478 / turns 5349 / sstp 443
        ("turn://1.2.3.4", "turn://1.2.3.4:3478"),
        ("turns://u:p@host", "turns://u:p@host:5349"),
        ("sstp://u:p@host", "sstp://u:p@host:443"),
        # 显式默认参数在规范化时被剔除（等价 URI 收敛为同一形态，进程池键稳定）
        (
            "turns://u:p@host?transport=tcp&insecure=false",
            "turns://u:p@host:5349",
        ),
        (
            "sstp://u:p@host?insecure=false&mtu=1400",
            "sstp://u:p@host:443",
        ),
        # 非默认参数保留
        (
            "turns://u:p@host?transport=tcp&realm=example.org",
            "turns://u:p@host:5349?realm=example.org",
        ),
        (
            "sstp://user:pass@vpn.example.com:8443?sni=cert.example.com&mtu=1280",
            "sstp://user:pass@vpn.example.com:8443?mtu=1280&sni=cert.example.com",
        ),
        # IPv6 端点
        (
            "TURNS:[2001:db8::1]?transport=tcp&username=u%40ser&password=p%3Aa%23ss",
            "turns://u%40ser:p%3Aa%23ss@[2001:db8::1]:5349",
        ),
        # base64 一段式 userinfo 自动解码
        (
            "turn://dXNlcjpwYXNz@192.0.2.10:3478",
            "turn://user:pass@192.0.2.10:3478",
        ),
        # 匿名 TURN 分配服务器合法（缺省凭据）
        ("turn://192.0.2.10:3479", "turn://192.0.2.10:3479"),
        # 凭据放在 query 参数
        (
            "turn:host:3478?username=u&password=p&transport=tcp",
            "turn://u:p@host:3478",
        ),
    ],
)
def test_normalize_proxy_url_accepts_bridge_formats(raw: str, expected: str) -> None:
    assert normalize_proxy_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "turn://u:p@host?transport=udp",  # 仅支持 RFC 6062 TCP 中继
        "turn://u:p@host?sni=other",  # TLS 参数仅适用于 turns/sstp
        "sstp://192.0.2.10:443",  # SSTP 必须提供 MS-CHAPv2 凭据
        "sstp://onlypass@host:443",  # 缺用户名
        "turn://u:p@host?unused=1",  # 未在白名单内的高级参数
        "sstp://u:p@host?mtu=100",  # MTU 下限 576
        "sstp://u:p@host?mtu=9000",  # MTU 上限 1400（与 Go 侧 TUN 钳位一致）
        "sstp://u:p@host?insecure=maybe",  # insecure 取值非法
        "turns://u:p@host:0",  # 端口越界
        "turns://u:p@[invalid-ipv6]:3478",
        "turn://u:p@host/unsupported",  # 不允许路径
        "sstp://u:p@host?ca=a&ca=b",  # 重复参数（strict_parsing 拒绝）
        "turn://u:p@host#frag%0A",  # 控制字符
        "turns://u:p@host?password=p2",  # userinfo 与 query 凭据混用
    ],
)
def test_normalize_proxy_url_rejects_invalid_bridge_formats(raw: str) -> None:
    assert normalize_proxy_url(raw) is None


def test_parse_bridge_proxy_uri_returns_full_config() -> None:
    config = parse_bridge_proxy_uri(
        "sstp://user:pass@vpn.example.com:8443?sni=cert.example.com&mtu=1280"
    )
    assert config["scheme"] == "sstp"
    assert config["server"] == "vpn.example.com"
    assert config["server_port"] == 8443
    assert config["username"] == "user"
    assert config["password"] == "pass"
    assert config["tls_server_name"] == "cert.example.com"
    assert config["mtu"] == 1280
    assert config["insecure"] is False
    assert config["realm"] == ""


def test_parse_bridge_proxy_uri_error_never_echoes_credentials() -> None:
    with pytest.raises(ProxyFormatError) as exc:
        parse_bridge_proxy_uri("sstp://private-user:private-pass@host?mtu=100")
    assert "private-pass" not in str(exc.value)


def test_build_bridge_proxy_uri_round_trip_is_stable() -> None:
    raw = "turns://u:p@host:5349?transport=tcp&realm=r"
    canonical = normalize_proxy_url(raw)
    assert canonical == "turns://u:p@host:5349?realm=r"
    # 规范 URI 二次规范化不变（幂等），等价写法收敛到同一形态
    assert normalize_proxy_url(canonical) == canonical
    rebuilt = build_bridge_proxy_uri(
        "turns", "host", 5349, "u", "p", options={"realm": "r"}
    )
    assert rebuilt == canonical


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


def test_split_turn_uri_before_cookie_on_own_line() -> None:
    # RFC 7065 无斜线写法 + query 参数：含 '=' 不再被拒（_looks_like_proxy
    # 对 turn/turns/sstp 前缀走了专门通道）
    proxy, cookies = split_proxy_from_paste(
        f"turn:1.2.3.4:3478?transport=tcp\n{_COOKIE}"
    )
    assert proxy == "turn://1.2.3.4:3478"
    assert "SID=sid_value" in cookies


def test_json_payload_is_never_split() -> None:
    payload = '[{"name": "SID", "value": "abc"}]'
    proxy, cookies = split_proxy_from_paste(payload)
    assert proxy is None
    assert cookies == payload
