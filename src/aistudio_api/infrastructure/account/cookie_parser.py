"""解析 Google 账号 Cookie（支持 JSON 数组、Netscape 格式与 Header KV），并提供多账号探活。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from aistudio_api.infrastructure.utils.common import mask_email
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("cookie_parser")

DEFAULT_API_KEY = "AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs"
DEFAULT_ORIGIN = "https://aistudio.google.com"
DEFAULT_USER_AGENT = "grpc-web-javascript/0.1"
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
)


def _discover_region() -> str:
    """读取 AISTUDIO_REGION 环境变量（ISO 3166-1 alpha-2，默认 US）。

    地区码会写入 X-Goog-Ext-519733851-bin 头（protobuf field 12），
    影响 Google 的地理路由（延迟与配额区域归属）。早期实现硬编码 JP，
    与多数账号实际地区不符，故改为可配置。
    """
    raw = (os.getenv("AISTUDIO_REGION") or "US").strip().upper()
    if len(raw) == 2 and raw.isascii() and raw.isalpha():
        return raw
    return "US"


DEFAULT_REGION = _discover_region()

# X-Goog-Ext-519733851-bin 二进制模板（protobuf）:
#   08 00 12 01 42 30 01 38 04 40 00 50 04 58 03 62 02 <REG> 70 00 78 01 90 01 00 98 01 01
# 其中 62 02 为 field 12 (wire type 2, len 2)，后接 2 字节地区码 ASCII。
_EXT_BIN_PREFIX = bytes.fromhex("0800120142300138044000500458036202")
_EXT_BIN_SUFFIX = bytes.fromhex("70007801900100980101")


def build_ext_bin(region: str | None = None) -> str:
    """按地区码构建 X-Goog-Ext-519733851-bin 头值。"""
    reg = (region or DEFAULT_REGION).strip().upper()
    if len(reg) != 2 or not reg.isascii() or not reg.isalpha():
        reg = "US"
    return base64.b64encode(
        _EXT_BIN_PREFIX + reg.encode("ascii") + _EXT_BIN_SUFFIX
    ).decode("ascii")


DEFAULT_EXT_BIN = build_ext_bin()

# 2026-11 HAR 实测（Android Chrome 130 真机抓包）：Chrome 仅在访问
# aistudio.google.com/app/_/bscframe（浏览器信号收集 iframe，GET）时携带
# X-Browser-Channel/Copyright/Year/Validation 四个头；对 alkalimakersuite-pa
# 的所有 RPC 请求（GenerateContent/CountTokens/...）从不携带。在 API 请求上
# 发送这组头会构成真实客户端不存在的指纹偏离，故不再附加到 RPC 头集合。
# 若 WAA 签名服务返回动态同名头，仍会按 client.py 的合并逻辑生效。


def _default_accept_language() -> str:
    """读取 AISTUDIO_ACCEPT_LANGUAGE 环境变量，默认与实测浏览器一致。"""
    raw = (os.getenv("AISTUDIO_ACCEPT_LANGUAGE") or "").strip()
    return raw or "zh-CN,zh;q=0.9"


DEFAULT_ACCEPT_LANGUAGE = _default_accept_language()

# X-AIStudio-Visit-Id 会话级复用（2026-11 HAR 实测）：真实浏览器在
# 一个页面会话内对同一账号的所有 RPC（GenerateContent ×7、CountTokens、
# ListApplets……）复用同一个 visit-id（v1_ + base64(uuid) 48 字符）；
# 现在按“作用域”缓存复用，避免每次请求都换新值构成指纹偏离。
_VISIT_ID_CACHE: dict[str, str] = {}


def get_visit_id(scope: str = "default") -> str:
    """按作用域（通常为账号标识）返回稳定的 X-AIStudio-Visit-Id 值。"""
    vid = _VISIT_ID_CACHE.get(scope)
    if not vid:
        vid = "v1_" + base64.b64encode(str(uuid.uuid4()).encode("ascii")).decode(
            "ascii"
        )
        _VISIT_ID_CACHE[scope] = vid
    return vid


# Firefox/Chrome 发起跨域 fetch（aistudio.google.com → clients6.google.com）
# 时必带的 Sec-Fetch-* 三元组与 Accept 头；Python httpx 路径原先缺失
# （HAR 实测 2026-11：35/35 个 GenerateContent 请求均携带）。
CORS_FETCH_HEADERS: dict[str, str] = {
    "Accept": "*/*",
    "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

BENEFIT_TIER_TO_NAME: dict[int, str] = {
    0: "Free",
    1: "Pro",
    2: "Ultra",
    3: "Plus",
}

BENEFIT_TIER_TO_HEADER: dict[str, str] = {
    "Pro": "TIER1",
    "Ultra": "TIER2",
    "Ultra_20X": "TIER2",
    "Plus": "TIER0",
}

_CANONICAL_TIERS: dict[str, str] = {
    "free": "Free",
    "plus": "Plus",
    "pro": "Pro",
    "ultra": "Ultra",
    "ultra_20x": "Ultra_20X",
}


def normalize_benefit_tier(tier: str | None) -> str:
    """Normalize benefit tier string into canonical name (Free / Plus / Pro / Ultra / Ultra_20X)."""
    if not tier:
        return "Free"
    return _CANONICAL_TIERS.get(tier.strip().lower(), "Free")


def parse_benefit_tier_response(raw_data: object) -> str:
    """解析 GetAiStudioBenefitTier 响应数组为标准权益名称 (Free / Pro / Ultra / Plus)。"""
    if isinstance(raw_data, str):
        text = raw_data.strip().removeprefix(")]}'").strip()
        if text.startswith("[,"):
            text = "[null," + text[2:]
        try:
            raw_data = json.loads(text)
        except Exception:
            return "Free"
    if not isinstance(raw_data, list) or not raw_data or raw_data[0] is None:
        return "Free"
    val = raw_data[0]
    if isinstance(val, bool):
        return "Free"
    if isinstance(val, (int, float)):
        return BENEFIT_TIER_TO_NAME.get(int(val), "Free")
    return "Free"


def get_tier_header_value(tier: str | None) -> str | None:
    """根据账号权益等级返回对应的 X-AIStudio-G1-Tier 请求头取值。"""
    if not tier:
        return None
    clean = normalize_benefit_tier(tier)
    return BENEFIT_TIER_TO_HEADER.get(clean)


# 外部导入时过滤的遥测与设备绑定 Cookie。
DISCARDABLE_IMPORT_COOKIE_NAMES: frozenset[str] = frozenset(
    {
        "_ga",
        "_gid",
        "_gat",
        "_gcl_au",
        "1P_JAR",
        "DV",
        "AEC",
        "NID",
        "OSID",
        "__Secure-OSID",
    }
)

ACCOUNT_HOST_ONLY_COOKIES: frozenset[str] = frozenset(
    {
        "LSID",
        "__Host-1PLSID",
        "__Host-3PLSID",
        "__Host-GAPS",
        "ACCOUNT_CHOOSER",
    }
)

_ALLOWED_HOST_COOKIES: frozenset[str] = frozenset(
    {
        "__Host-1PLSID",
        "__Host-3PLSID",
    }
)

MINIMAL_COOKIE_WHITELIST: frozenset[str] = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "__Secure-1PSIDTS",
        "__Secure-3PSIDTS",
        "SIDCC",
        "__Secure-1PSIDCC",
        "__Secure-3PSIDCC",
        # 2026-11 真机抓包（16 项 Cookie 全集）实测：真实浏览器对
        # alkalimakersuite RPC 恒携带以下两项；此前被请求侧白名单过滤。
        # __Secure-STRP: Google Private State Token（风控信任信号）；
        # SEARCH_SAMESITE: SameSite 豁免标记。
        # 放开后随核心鉴权 Cookie 一并上行，并使 WAA 铸签 Cookie 集与浏览器一致。
        "__Secure-STRP",
        "SEARCH_SAMESITE",
    }
)

ROTATE_COOKIE_WHITELIST: frozenset[str] = (
    MINIMAL_COOKIE_WHITELIST | ACCOUNT_HOST_ONLY_COOKIES
)

ROTATING_SESSION_COOKIE_NAMES: tuple[str, ...] = (
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "SIDCC",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
)


def filter_minimal_cookies(
    cookies: dict[str, str], *, include_account_host: bool = False
) -> dict[str, str]:
    """过滤保留核心鉴权 Cookie；默认排除 accounts.google.com 专属 Cookie。

    先做快照再迭代：``cookies`` 可能是被工作线程（Set-Cookie 回写）并发
    更新的共享 SessionContext 字典，直接迭代会触发
    "dictionary changed size during iteration"（dict(cookies) 是单次
    C 级拷贝，原子可见）。
    """
    allowed = (
        ROTATE_COOKIE_WHITELIST if include_account_host else MINIMAL_COOKIE_WHITELIST
    )
    snapshot = dict(cookies)
    return {k: v for k, v in snapshot.items() if k in allowed and v}


def compute_session_anchor(cookies: dict[str, str]) -> str:
    """提取浏览器会话锚点（用于识别同一窗口下的多登子账号 u/0..u/N）。"""
    if not cookies:
        return ""
    return (
        cookies.get("SAPISID")
        or cookies.get("__Secure-3PAPISID")
        or cookies.get("__Secure-1PAPISID")
        or cookies.get("SID")
        or cookies.get("__Secure-3PSID")
        or ""
    )


def apply_set_cookies(
    cookies: dict[str, str],
    set_cookie_headers: list[str] | tuple[str, ...],
) -> list[str]:
    """将 HTTP 响应 Set-Cookie 头合并至 cookies 字典，返回发生变更的 Cookie 名称列表。"""
    updated: list[str] = []
    if not set_cookie_headers:
        return updated
    for raw in set_cookie_headers:
        if not raw or not isinstance(raw, str):
            continue
        parts = [s.strip() for s in raw.split(";")]
        if not parts or not parts[0]:
            continue
        nv, attrs = parts[0], parts[1:]
        eq = nv.find("=")
        if eq <= 0:
            continue
        name = nv[:eq].strip()
        val = nv[eq + 1 :].strip()
        if not name or not val or val == "deleted":
            continue
        if name not in ROTATE_COOKIE_WHITELIST:
            continue
        attr_str = "; ".join(attrs)
        if re.search(r"domain=[^;]*gstatic\.com", attr_str, re.IGNORECASE):
            continue
        if re.search(r"expires=[^;]*19[789]\d", attr_str, re.IGNORECASE):
            continue
        if re.search(r"max-age\s*=\s*(?:0|-\d+)", attr_str, re.IGNORECASE):
            continue
        if cookies.get(name) != val:
            cookies[name] = val
            updated.append(name)
    return updated


def parse_raw_cookies(raw: object) -> dict[str, str]:
    """万能 Cookie 解析器，支持：
    1. JSON 数组（EditThisCookie / Cookie-Editor 导出：`[{"name": "...", "value": "..."}, ...]`）
    2. JSON 对象（`{"cookies": [...]}` 或 `{"cookies": {"k": "v"}}`）
    3. Netscape / curl 格式（7 列 Tab 分隔）
    4. HTTP 请求头格式（`Cookie: k1=v1; k2=v2` 或带换行的多行键值对）
    """
    parsed: dict[str, str] = {}
    if not raw:
        return parsed

    text = raw.strip() if isinstance(raw, str) else json.dumps(raw)

    if re.match(r"^cookie:\s*", text, re.IGNORECASE):
        text = re.sub(r"^cookie:\s*", "", text, flags=re.IGNORECASE)

    # 1. 尝试 JSON 格式
    if text.startswith(("{", "[")):
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                for item in obj:
                    if (
                        isinstance(item, dict)
                        and item.get("name")
                        and item.get("value") is not None
                    ):
                        k = str(item["name"]).strip()
                        v = str(item["value"]).strip()
                        if k and v:
                            parsed[k] = v
                if parsed:
                    return parsed
            elif isinstance(obj, dict):
                if isinstance(obj.get("cookie"), str):
                    text = obj["cookie"]
                elif isinstance(obj.get("cookies"), str):
                    text = obj["cookies"]
                elif isinstance(obj.get("cookies"), list):
                    for item in obj["cookies"]:
                        if (
                            isinstance(item, dict)
                            and item.get("name")
                            and item.get("value") is not None
                        ):
                            k = str(item["name"]).strip()
                            v = str(item["value"]).strip()
                            if k and v:
                                parsed[k] = v
                    if parsed:
                        return parsed
                elif isinstance(obj.get("cookies"), dict):
                    for k, v in obj["cookies"].items():
                        if k and v is not None:
                            parsed[str(k).strip()] = str(v).strip()
                    return parsed
        except Exception:
            pass

    # 2. 按行与分号拆分解析
    lines = re.split(r"[\r\n;]+", text)
    ignored_keys = {
        "domain",
        "path",
        "expires",
        "samesite",
        "secure",
        "httponly",
        "priority",
        "hostonly",
    }

    for line in lines:
        trimmed = line.strip()
        # Netscape cookies.txt 标准：HttpOnly Cookie 以 #HttpOnly_ 前缀存储，
        # 并非注释行。Google 的 SID/HSID/SSID/__Secure-*PSID 全部是 HttpOnly，
        # 必须先剥离该前缀再走 Tab 分列解析，否则核心鉴权 Cookie 会被静默丢弃。
        if trimmed.startswith("#HttpOnly_"):
            trimmed = trimmed[len("#HttpOnly_") :].strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        # Netscape 7 列 Tab 分隔
        if "\t" in trimmed:
            cols = trimmed.split("\t")
            if len(cols) >= 7:
                k = cols[5].strip() if len(cols) > 5 else ""
                v = cols[6].strip() if len(cols) > 6 else ""
                if k and v and k.lower() not in ignored_keys:
                    parsed[k] = v
                continue

        # 标准 KV：name=value
        if "=" in trimmed:
            k, _, v = trimmed.partition("=")
            k = k.strip()
            v = v.strip()
            k = re.sub(r"^cookie:\s*", "", k, flags=re.IGNORECASE)
            if (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            ):
                v = v[1:-1]
            if k and v and k.lower() not in ignored_keys:
                parsed[k] = v

    return parsed


def calculate_sapisid_hash(
    cookies: dict[str, str], origin: str = DEFAULT_ORIGIN
) -> str:
    """计算 Google SAPISIDHASH 鉴权头部。"""
    sapisid = (
        cookies.get("SAPISID")
        or cookies.get("__Secure-3PAPISID")
        or cookies.get("APISID")
        or ""
    )
    if not sapisid:
        return ""

    ts = int(time.time())

    def calc_hash(val: str) -> str:
        return hashlib.sha1(f"{ts} {val} {origin}".encode()).hexdigest()

    parts = [f"SAPISIDHASH {ts}_{calc_hash(sapisid)}"]
    one = cookies.get("__Secure-1PAPISID") or sapisid
    parts.append(f"SAPISID1PHASH {ts}_{calc_hash(one)}")
    three = cookies.get("__Secure-3PAPISID") or sapisid
    parts.append(f"SAPISID3PHASH {ts}_{calc_hash(three)}")
    return " ".join(parts)


def build_makersuite_headers(
    cookies: dict[str, str] | None = None,
    *,
    auth_user: str = "0",
    tier: str | None = None,
    include_visit_id: bool = False,
    visit_scope: str | None = None,
) -> dict[str, str]:
    """Build standard Google AI Studio MakerSuiteService RPC headers."""
    headers: dict[str, str] = {
        "Content-Type": "application/json+protobuf",
        "X-User-Agent": DEFAULT_USER_AGENT,
        "X-Goog-Api-Key": DEFAULT_API_KEY,
        "X-Goog-AuthUser": str(auth_user or "0"),
        "X-Goog-Ext-519733851-bin": DEFAULT_EXT_BIN,
        "Origin": DEFAULT_ORIGIN,
        "Referer": f"{DEFAULT_ORIGIN}/",
        "User-Agent": DEFAULT_BROWSER_USER_AGENT,
        **CORS_FETCH_HEADERS,
    }
    if include_visit_id:
        if visit_scope:
            # 会话级复用：同作用域（账号）内稳定不变，对齐真实浏览器。
            headers["X-AIStudio-Visit-Id"] = get_visit_id(visit_scope)
        else:
            # 官网格式为 v1_ + base64(UUID 字符串)（HAR 实测 48 字符）；
            # 直接拼接裸 UUID 字符串会得到 40 字符的异常形态。
            headers["X-AIStudio-Visit-Id"] = "v1_" + base64.b64encode(
                str(uuid.uuid4()).encode("ascii")
            ).decode("ascii")
    tier_header = get_tier_header_value(tier)
    if tier_header:
        headers["X-AIStudio-G1-Tier"] = tier_header
    if cookies:
        # 快照后拼接（cookies 可能是被工作线程并发更新的共享 dict）
        snapshot = dict(cookies)
        auth_header = calculate_sapisid_hash(snapshot)
        if auth_header:
            headers["Authorization"] = auth_header
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in snapshot.items() if v)
    return headers


def parse_cookie_string(
    raw: object, *, discard_transient: bool = True
) -> dict[str, object]:
    """将任意格式 Cookie 解析为紧凑的持久化字典结构 `{"cookies": [{"name": ..., "value": ...}]}`。"""
    cookie_dict = parse_raw_cookies(raw)
    cookies: list[dict[str, str]] = []
    for name, value in cookie_dict.items():
        if name.startswith("__Host-") and name not in _ALLOWED_HOST_COOKIES:
            continue
        if discard_transient and (
            name in DISCARDABLE_IMPORT_COOKIE_NAMES
            or name.startswith(("_ga_", "_gcl_"))
        ):
            continue
        cookies.append({"name": name, "value": value})
    return {"cookies": cookies}


# ========== 环境变量 Cookie 注入（AISTUDIO_COOKIES / AISTUDIO_COOKIES_<N> / AISTUDIO_COOKIES_FILE） ==========

_PROXY_SCHEMES: tuple[str, ...] = (
    "http://",
    "https://",
    "socks5://",
    "socks5h://",
    "turn://",
    "turns://",
    "sstp://",
)

# host:port（如 127.0.0.1:7890 / proxy.example.com:8080）
_HOST_PORT_RE = re.compile(r"[A-Za-z0-9_.\-]+:\d{2,5}")
# user:pass@host:port（无协议头，如 user:pass@1.2.3.4:1080）
_USERINFO_HOST_PORT_RE = re.compile(r"[^@\s]+@[A-Za-z0-9_.\-]+:\d{2,5}")
# host:port:user:pass（curl -x 风格，如 1.2.3.4:1080:user:pass）
_HOST_PORT_USERINFO_RE = re.compile(r"[A-Za-z0-9_.\-]+:\d{2,5}:[^:\s]+:[^:\s]+")

# ================= TURN / TURNS / SSTP 桥接代理解析 =================
# 接法学习自 vertex2openai：这三类协议无法被 httpx 直接消费，由独立的
# Go 桥接器（proxybridge/）转成本地回环 SOCKS5。解析层只负责严格校验
# 与规范化；子进程管理见 infrastructure/proxy_bridge.py。

_BRIDGE_SCHEMES: tuple[str, ...] = ("turn", "turns", "sstp")
_BRIDGE_DEFAULT_PORTS: dict[str, int] = {"turn": 3478, "turns": 5349, "sstp": 443}
# RFC 7065 TURN URI 无斜线形态（turn:host:port?transport=tcp）；sstp 同样
# 接受无斜线简写以提升粘贴识别容错。
_BRIDGE_NO_SLASH_RE = re.compile(r"^(?:turns?|sstp):(?!//)", re.IGNORECASE)
# 桥接代理允许的高级参数（query 白名单，按协议分发）
_BRIDGE_ALLOWED_OPTIONS: dict[str, frozenset[str]] = {
    "turn": frozenset({"transport", "realm"}),
    "turns": frozenset({"transport", "realm", "sni", "ca", "insecure"}),
    "sstp": frozenset({"sni", "ca", "insecure", "mtu"}),
}


class ProxyFormatError(ValueError):
    """代理地址格式错误（文案不含凭据，可直接透传给调用方展示）。"""


def parse_bridge_proxy_uri(uri: str) -> dict[str, object]:
    """严格解析 TURN / TURNS / SSTP 代理 URI，返回桥接器配置字典。

    支持：RFC 7065 无斜线形态、query 参数凭据（``username`` / ``password``）、
    userinfo（含 base64 一段式自动解码）、高级参数白名单校验。
    返回键：``scheme / server / server_port / username / password /
    tls_server_name / ca_file / insecure / realm / mtu / options / name``。
    格式非法时抛 :class:`ProxyFormatError`（错误文案不回显凭据）。
    """
    from urllib.parse import parse_qs, unquote, urlsplit

    value = str(uri).strip()
    value = re.sub(r"^(turns?|sstp):(?!//)", r"\1://", value, flags=re.IGNORECASE)
    try:
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or re.search(
            r"%(?![0-9a-fA-F]{2})", value
        ):
            raise ValueError
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in _BRIDGE_SCHEMES:
            raise ValueError
        host = parts.hostname
        port = parts.port if parts.port is not None else _BRIDGE_DEFAULT_PORTS[scheme]
        if not host or not 1 <= port <= 65535 or parts.path not in {"", "/"}:
            raise ValueError
        if any(c.isspace() or c in "%/@?#\\" for c in host):
            raise ValueError
        if ":" in host:
            import ipaddress

            ipaddress.IPv6Address(host)
        else:
            host = host.encode("idna").decode("ascii")
        query = parse_qs(
            parts.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
        if any(len(values) != 1 for values in query.values()):
            raise ValueError
        query = {key: values[0] for key, values in query.items()}
        username = unquote(parts.username or "", errors="strict")
        password = unquote(parts.password or "", errors="strict")
        # 协议://Base64(用户名:密码)@主机 一段式 userinfo 自动解码
        if username and not password and ":" not in username:
            import base64

            try:
                decoded = base64.b64decode(
                    username + "=" * (-len(username) % 4),
                    altchars=b"-_",
                    validate=True,
                ).decode()
                if ":" in decoded:
                    username, _, password = decoded.partition(":")
            except (ValueError, UnicodeError):
                pass
        if (username or password) and ({"username", "password"} & query.keys()):
            raise ValueError
        username = username or query.pop("username", "")
        password = password or query.pop("password", "")
        name = unquote(parts.fragment, errors="strict")
        if any(
            ord(c) < 32 or ord(c) == 127
            for text in [host, username, password, name, *query.values()]
            for c in text
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ProxyFormatError(
            "TURN / SSTP 地址无效，请检查主机、端口、凭据编码和重复参数。"
        ) from None

    # SSTP 认证（MS-CHAPv2）必须提供凭据；TURN / TURNS 允许匿名分配服务器
    # （未认证 Allocate 直接成功），由桥接器启动期探测自动选择客户端形态。
    if scheme == "sstp" and (not username or not password):
        raise ProxyFormatError(
            "SSTP 需要同时填写用户名和密码；链接内的特殊字符须进行 URL 编码。"
        )
    allowed = _BRIDGE_ALLOWED_OPTIONS[scheme]
    if query.keys() - allowed:
        raise ProxyFormatError(
            f"{scheme.upper()} 代理包含不支持的高级参数（"
            f"{'/'.join(sorted(query.keys() - allowed))}），请查看接入文档。"
        )
    if scheme in {"turn", "turns"} and query.get("transport", "tcp").lower() != "tcp":
        raise ProxyFormatError(
            "TURN 代理仅支持 RFC 6062 TCP 中继，transport 必须为 tcp。"
        )
    if scheme == "turn" and ({"sni", "ca", "insecure"} & query.keys()):
        raise ProxyFormatError("TLS 参数仅适用于 turns:// 或 sstp://。")
    if "insecure" in query and query["insecure"].lower() not in {
        "0",
        "1",
        "false",
        "true",
    }:
        raise ProxyFormatError("insecure 必须是 true / false 或 1 / 0。")
    if "sni" in query and (
        not query["sni"] or any(c.isspace() or c in "/@?#\\%" for c in query["sni"])
    ):
        raise ProxyFormatError("证书名称（SNI）必须是有效的主机名或 IP。")
    try:
        if not re.fullmatch(r"[0-9]{3,4}", query.get("mtu", "1400")):
            raise ValueError
        mtu = int(query.get("mtu", "1400"))
        if not 576 <= mtu <= 1400:  # 与 Go 侧 TUN 钳位上限对齐
            raise ValueError
    except ValueError:
        raise ProxyFormatError("SSTP MTU 必须是 576–1400 的整数。") from None
    return {
        "scheme": scheme,
        "server": host,
        "server_port": port,
        "username": username,
        "password": password,
        "tls_server_name": query.get("sni") or host,
        "ca_file": query.get("ca", ""),
        "insecure": query.get("insecure", "false").lower() in {"true", "1"},
        "realm": query.get("realm", ""),
        "mtu": mtu,
        "options": query,
        "name": name,
    }


def build_bridge_proxy_uri(
    scheme: str,
    host: str,
    port: int | str | None,
    username: str = "",
    password: str = "",
    *,
    options: dict[str, object] | None = None,
    name: str = "",
) -> str:
    """构建无歧义的桥接代理 URI；凭据与保留字符一律百分号编码。

    显式默认值（``transport=tcp`` / ``insecure=false`` / ``mtu=1400`` /
    ``sni=主机``）会被剔除，保证等价输入得到同一规范 URI（进程池键稳定）。
    """
    import ipaddress
    from urllib.parse import quote, urlencode

    scheme = str(scheme).strip().lower()
    if scheme not in _BRIDGE_SCHEMES:
        raise ProxyFormatError("请选择 TURN、TURNS 或 SSTP 代理类型。")
    if port is None or str(port).strip() == "":
        port = _BRIDGE_DEFAULT_PORTS[scheme]
    host = str(host or "").strip().strip("[]")
    if not host or any(c.isspace() or c in "/@?#\\" for c in host):
        raise ProxyFormatError("请填写有效的代理 IP 或主机名。")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ProxyFormatError("IPv6 地址无效。") from None
        host = f"[{host}]"
    try:
        port_text = str(port).strip()
        if not re.fullmatch(r"[0-9]{1,5}", port_text):
            raise ValueError
        port_num = int(port_text)
        if not 1 <= port_num <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise ProxyFormatError("代理端口必须是 1–65535 的整数。") from None
    username, password = str(username or ""), str(password or "")
    if any(ord(c) < 32 or ord(c) == 127 for c in username + password):
        raise ProxyFormatError("代理账号和密码不能包含换行或控制字符。")
    if password and not username:
        raise ProxyFormatError("填写密码时也需要填写用户名。")
    auth = (
        quote(username, safe="") + ":" + quote(password, safe="") + "@"
        if username
        else ""
    )
    uri = f"{scheme}://{auth}{host}:{port_num}"
    if options:
        if not isinstance(options, dict):
            raise ProxyFormatError("高级代理参数必须是键值对。")
        normalized: dict[str, str] = {}
        for key, raw in options.items():
            value = (
                str(raw).lower()
                if isinstance(raw, bool)
                else str(raw)
                if raw is not None
                else ""
            )
            if value == "":
                continue
            normalized[str(key)] = value
        # 剔除与规范默认等价的参数，避免等价 URI 分裂进程池
        defaults = {
            "transport": "tcp",
            "insecure": "false",
            "mtu": "1400",
            "sni": host.strip("[]"),
        }
        normalized = {k: v for k, v in normalized.items() if defaults.get(k) != v}
        if normalized:
            uri += "?" + urlencode(sorted(normalized.items()))
    if name:
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ProxyFormatError("代理备注不能包含控制字符。")
        uri += "#" + quote(name, safe="")
    return uri


def _normalize_bridge_proxy_url(text: str) -> str | None:
    """TURN / TURNS / SSTP 输入的规范化入口；非法时返回 None。"""
    try:
        cfg = parse_bridge_proxy_uri(text)
    except ProxyFormatError:
        return None
    raw_options = cfg["options"]
    options = (
        {str(key): value for key, value in raw_options.items()}
        if isinstance(raw_options, dict)
        else {}
    )
    try:
        return build_bridge_proxy_uri(
            str(cfg["scheme"]),
            str(cfg["server"]),
            int(cfg["server_port"]),  # type: ignore[arg-type]
            str(cfg["username"]),
            str(cfg["password"]),
            options=options,
            name=str(cfg["name"]),
        )
    except (ProxyFormatError, TypeError, ValueError):
        return None


def normalize_proxy_url(candidate: str | None) -> str | None:
    """规整代理地址：无法识别时返回 None。

    支持：带协议头（``socks5://user:pass@host:port``，原样保留）、隐式 HTTP
    （``127.0.0.1:7890``）、无协议头带凭据（``user:pass@host:port``）、
    curl -x 风格（``host:port:user:pass`` → ``http://user:pass@host:port``），
    以及 TURN / TURNS / SSTP（含 RFC 7065 无斜线形态，规范化为桥接 URI）。
    """
    if not candidate:
        return None
    text = candidate.strip().rstrip("/")
    if not text:
        return None
    if _BRIDGE_NO_SLASH_RE.match(text):
        return _normalize_bridge_proxy_url(text)
    lowered = text.lower()
    if lowered.startswith(("turn://", "turns://", "sstp://")):
        return _normalize_bridge_proxy_url(text)
    if "://" not in text and _HOST_PORT_USERINFO_RE.fullmatch(text):
        host, port, user, password = text.split(":", 3)
        text = f"http://{user}:{password}@{host}:{port}"
    elif "://" not in text and (
        _USERINFO_HOST_PORT_RE.fullmatch(text) or _HOST_PORT_RE.fullmatch(text)
    ):
        text = f"http://{text}"
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme.lower() not in ("http", "https", "socks5", "socks5h")
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or re.search(r"[\s\x00-\x1f]", text)
            or parsed.port == 0
        ):
            return None
        if parsed.scheme.startswith("socks") and parsed.port is None:
            return None
    except ValueError:
        return None
    return text


def _looks_like_proxy(candidate: str) -> bool:
    """判断片段是否形似代理地址：不含 Cookie 特征（= / ; / 空白）且可规整。

    TURN / SSTP 链接例外：RFC 7065 的 query（``transport=tcp``）天然含 ``=``，
    按协议前缀优先识别后再交给 normalize 严格校验。
    """
    text = candidate.strip()
    if not text or re.search(r"\s", text):
        return False
    if re.match(r"^(?:turns?|sstp):", text, re.IGNORECASE):
        return normalize_proxy_url(text) is not None
    if "=" in text or ";" in text:
        return False
    return normalize_proxy_url(text) is not None


def _has_core_cookies(raw: object) -> bool:
    """判断内容能否解析出 Google 核心鉴权 Cookie。"""
    parsed = parse_raw_cookies(raw)
    if not parsed:
        return False
    return any(k in ROTATE_COOKIE_WHITELIST for k in parsed)


def split_proxy_cookie_entry(entry: str) -> tuple[str | None, str]:
    """拆分单条环境变量注入项，支持「代理+Cookie」「仅 Cookie」「Cookie+代理」三种格式。

    Cookie 值本身可能包含 ``+``（如 base64 取值），因此不能简单按第一个加号切分：
    只有当某一侧形似代理地址、且另一侧能解析出核心 Cookie 时才认定切分有效。
    优先从左向右扫描（代理在前），失败后从右向左扫描（代理在后），均失败则整体视为纯 Cookie。
    """
    text = entry.strip()
    if "+" not in text:
        return None, text

    # 代理在前：从左向右寻找第一个有效切点
    pos = 0
    while True:
        idx = text.find("+", pos)
        if idx < 0:
            break
        left, right = text[:idx], text[idx + 1 :]
        if _looks_like_proxy(left) and _has_core_cookies(right):
            return normalize_proxy_url(left), right.strip()
        pos = idx + 1

    # Cookie 在前：从右向左寻找第一个有效切点
    pos = len(text)
    while True:
        idx = text.rfind("+", 0, pos)
        if idx < 0:
            break
        left, right = text[:idx], text[idx + 1 :]
        if _looks_like_proxy(right) and _has_core_cookies(left):
            return normalize_proxy_url(right), left.strip()
        pos = idx

    return None, text


# 粘贴文本里代理与 Cookie 之间允许的分隔符（按优先级尝试）
_PASTE_PROXY_SEPARATORS: tuple[str, ...] = ("+", "\n", "|", ",", " ")

# 允许「代理：」「proxy=」这类前缀标签
_PROXY_LABEL_RE = re.compile(r"^(?:proxy|代理)\s*[:=]\s*", re.IGNORECASE)


def _strip_proxy_label(text: str) -> str:
    """去掉「代理：」「proxy=」之类的前缀标签。"""
    return _PROXY_LABEL_RE.sub("", text.strip()).strip()


def split_proxy_from_paste(text: str) -> tuple[str | None, str]:
    """从粘贴的 Cookie 文本中拆出代理地址，返回 ``(proxy_url, 纯 Cookie 文本)``。

    支持代理在前或在后，分隔符 ``+``、换行、``|``、逗号、空格；空格分隔只认
    首尾两个片段（移除中间片段会把相邻 Cookie 键值粘在一起）。JSON 数组/
    对象视为完整凭据包，不参与拆分。识别不出代理时原样返回文本。
    """
    raw = text.strip()
    if not raw or raw.startswith(("{", "[")):
        return None, text
    for sep in _PASTE_PROXY_SEPARATORS:
        if sep not in raw:
            continue
        parts = raw.split(sep)
        candidates = range(len(parts)) if sep != " " else (0, len(parts) - 1)
        for idx in candidates:
            if idx < 0 or idx >= len(parts):
                continue
            candidate = _strip_proxy_label(parts[idx])
            if not _looks_like_proxy(candidate):
                continue
            rest = sep.join(part for i, part in enumerate(parts) if i != idx).strip()
            if _has_core_cookies(rest):
                return normalize_proxy_url(candidate), rest
    return None, text


@dataclass(slots=True)
class EnvCookieEntry:
    """单条环境变量 Cookie 注入项（已解析、已校验）。"""

    index: int
    raw: str
    proxy_url: str | None
    cookie_raw: str
    cookies: dict[str, str] = field(default_factory=dict)
    storage_state: dict[str, object] = field(default_factory=dict)
    anchor: str = ""
    id_hash: str = ""


def _split_env_lines(value: str) -> list[str]:
    """按换行拆分环境变量值，兼容 JSON 字符串数组与字面 ``\\n`` 转义两种写法。"""
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                return [str(x).strip() for x in obj if str(x).strip()]
        except Exception:
            pass
    # docker run -e / PaaS 面板无法传入真实换行时，允许用字面 \n 分隔多条
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def collect_env_cookie_raw_values() -> list[str]:
    """收集 AISTUDIO_COOKIES / AISTUDIO_COOKIES_<N> / AISTUDIO_COOKIES_FILE 的原始注入项。"""
    raw_values: list[str] = []

    main_value = (os.getenv("AISTUDIO_COOKIES") or "").strip()
    if main_value:
        raw_values.extend(_split_env_lines(main_value))

    # 编号变量：AISTUDIO_COOKIES_1、AISTUDIO_COOKIES_2 ... 按序号升序合并
    numbered: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        m = re.fullmatch(r"AISTUDIO_COOKIES_(\d{1,3})", name)
        if m and value.strip():
            numbered.append((int(m.group(1)), value.strip()))
    numbered.sort(key=lambda kv: kv[0])
    raw_values.extend(v for _, v in numbered)

    file_path = (os.getenv("AISTUDIO_COOKIES_FILE") or "").strip()
    if file_path:
        try:
            content = Path(file_path).expanduser().read_text(encoding="utf-8")
            raw_values.extend(_split_env_lines(content))
        except OSError as e:
            logger.warning("读取 AISTUDIO_COOKIES_FILE 失败: %s (%s)", file_path, e)

    return raw_values


def parse_env_cookie_entries(raw_values: list[str]) -> list[EnvCookieEntry]:
    """将原始注入项列表解析为已校验的 EnvCookieEntry 列表（无效项跳过并告警）。"""
    entries: list[EnvCookieEntry] = []
    seen_hashes: set[str] = set()
    for idx, raw in enumerate(raw_values, start=1):
        if not raw.strip():
            continue
        proxy_url, cookie_raw = split_proxy_cookie_entry(raw)
        if not _has_core_cookies(cookie_raw):
            logger.warning(
                "环境变量 Cookie 注入项 #%d 未解析出核心鉴权 Cookie，已跳过", idx
            )
            continue
        cookies = parse_raw_cookies(cookie_raw)
        storage_state = parse_cookie_string(cookie_raw)
        anchor = compute_session_anchor(cookies)
        digest_src = anchor or json.dumps(cookies, sort_keys=True, ensure_ascii=False)
        id_hash = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:10]
        if id_hash in seen_hashes:
            logger.warning(
                "环境变量 Cookie 注入项 #%d 与此前条目属于同一会话，已跳过重复项", idx
            )
            continue
        seen_hashes.add(id_hash)
        entries.append(
            EnvCookieEntry(
                index=idx,
                raw=raw,
                proxy_url=proxy_url,
                cookie_raw=cookie_raw,
                cookies=cookies,
                storage_state=storage_state,
                anchor=anchor,
                id_hash=id_hash,
            )
        )
    return entries


def load_account_meta(auth_file: str | Path | None) -> dict[str, object]:
    """一次性读取 auth_file 同目录的 meta.json。

    切换账号时原先分别调用 get_auth_user_from_meta / get_tier_from_meta /
    get_proxy_from_meta，同一个 1KB 的 meta.json 被解析三遍（实测 0.28ms
    纯浪费）。这里提供单次读取入口，三个兼容包装函数复用它。
    """
    if not auth_file:
        return {}
    try:
        meta_path = Path(auth_file).parent / "meta.json"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                return meta
    except Exception:
        pass
    return {}


def get_auth_user_from_meta(auth_file: str | Path | None) -> str:
    """Read auth_user index from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta = load_account_meta(auth_file)
            if meta:
                return str(meta.get("auth_user") or "0")
        except Exception:
            pass
    return "0"


def get_tier_from_meta(auth_file: str | Path | None) -> str:
    """Read benefit tier from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta = load_account_meta(auth_file)
            if meta:
                return normalize_benefit_tier(str(meta.get("tier") or "Free"))
        except Exception:
            pass
    return "Free"


def get_proxy_from_meta(auth_file: str | Path | None) -> str | None:
    """Read per-account proxy URL from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            raw = load_account_meta(auth_file).get("proxy_url")
            if raw:
                return normalize_proxy_url(str(raw))
        except Exception:
            pass
    return None


def load_cookies_from_auth_file(auth_file: str | Path | None) -> dict[str, str]:
    """Read cookies from auth_file into a name-value dictionary."""
    if not auth_file:
        return {}
    path = Path(auth_file)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if isinstance(cookies, list):
            return {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in cookies
                if isinstance(c, dict) and c.get("name")
            }
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items() if k}
    except Exception as e:
        logger.warning("读取 Cookie 文件失败 (%s): %s", auth_file, e)
    return {}


def parse_emails_from_ogs_html(
    html_text: str, default_auth_user: str = "0"
) -> dict[str, str]:
    """从 Google OGS Widget 或 AI Studio HTML 中提取 u/{index} 对应的真实邮箱字典。"""
    mapping: dict[str, str] = {}
    if not html_text:
        return mapping
    m_primary = re.search(r'"oPEP7c"\s*:\s*"([^"@\s]+@[^"\s]+)"', html_text)
    if m_primary:
        mapping[str(default_auth_user)] = m_primary.group(1).strip().lower()
    for m in re.finditer(
        r'data-au="(\d+)"\s+data-email="([^"@\s]+@[^"\s]+)"', html_text
    ):
        mapping[m.group(1)] = m.group(2).strip().lower()
    for m in re.finditer(
        r'data-email="([^"@\s]+@[^"\s]+)"\s+data-au="(\d+)"', html_text
    ):
        mapping[m.group(2)] = m.group(1).strip().lower()
    return mapping


async def _resolve_probe_proxy(proxy_url: str | None) -> str | None:
    """把传入代理（或全局默认代理）解析为 httpx 可用形态。

    统一委托 infrastructure/proxy_bridge.resolve_effective_proxy_async：
    turn / turns / sstp 转换为本地回环 SOCKS5（子进程冷启动放线程池），
    解析失败直接上抛（fail-closed，绝不静默降级直连导致真实 IP 泄漏）。
    延迟导入避免与 proxy_bridge 的模块循环。
    """
    from aistudio_api.infrastructure.proxy_bridge import (
        resolve_effective_proxy_async,
    )

    return await resolve_effective_proxy_async(proxy_url)


async def probe_account_emails_map(
    raw_or_dict: object,
    auth_user: str = "0",
    *,
    client: httpx.AsyncClient | None = None,
    proxy_url: str | None = None,
) -> dict[str, str]:
    """通过 Google OneGoogleWidget / AI Studio 页面探测全部已登录子账号的邮箱映射。"""
    cookie_dict = (
        raw_or_dict
        if isinstance(raw_or_dict, dict)
        and all(isinstance(v, str) for v in raw_or_dict.values())
        else parse_raw_cookies(raw_or_dict)
    )
    if not cookie_dict:
        return {}

    import httpx

    u_index = str(auth_user or "0")
    # 最小化 Cookie 白名单（L12）：探活只发核心鉴权 Cookie，避免把
    # accounts.google.com 域专属 Cookie 多发给 makersuite 域，减少风控而。
    cookie_header = "; ".join(
        f"{k}={v}" for k, v in filter_minimal_cookies(cookie_dict).items()
    )
    headers = {
        "User-Agent": DEFAULT_BROWSER_USER_AGENT,
        "Cookie": cookie_header,
    }

    async def _do_fetch(http_client: httpx.AsyncClient) -> dict[str, str]:
        resp = await http_client.get(
            f"https://ogs.google.com/u/{u_index}/widget/account",
            headers=headers,
        )
        if resp.status_code == 200:
            emails = parse_emails_from_ogs_html(resp.text, u_index)
            if emails:
                return emails
        resp_ai = await http_client.get(
            f"https://aistudio.google.com/u/{u_index}/prompts/new_chat",
            headers=headers,
        )
        if resp_ai.status_code == 200:
            return parse_emails_from_ogs_html(resp_ai.text, u_index)
        return {}

    try:
        if client is not None:
            return await _do_fetch(client)
        async with httpx.AsyncClient(
            timeout=8.0,
            proxy=await _resolve_probe_proxy(proxy_url),
            follow_redirects=True,
        ) as new_client:
            return await _do_fetch(new_client)
    except Exception as e:
        logger.debug("探测子账号 u/%s 邮箱失败: %s", u_index, e)
    return {}


async def probe_account_email(
    raw_or_dict: object,
    auth_user: str = "0",
    *,
    client: httpx.AsyncClient | None = None,
    proxy_url: str | None = None,
) -> str | None:
    """探测单个指定 auth_user 的登录邮箱。"""
    u_index = str(auth_user or "0")
    emails_map = await probe_account_emails_map(
        raw_or_dict, auth_user=u_index, client=client, proxy_url=proxy_url
    )
    if u_index in emails_map:
        return emails_map[u_index]
    if u_index != "0":
        root_map = await probe_account_emails_map(
            raw_or_dict, auth_user="0", client=client, proxy_url=proxy_url
        )
        if u_index in root_map:
            return root_map[u_index]
    return None


async def probe_account_benefit_tier(
    raw_or_dict: object,
    auth_user: str = "0",
    *,
    client: httpx.AsyncClient | None = None,
    proxy_url: str | None = None,
) -> str:
    """通过 GetAiStudioBenefitTier RPC 探测指定子账号的订阅权益等级。"""
    cookie_dict = (
        raw_or_dict
        if isinstance(raw_or_dict, dict)
        and all(isinstance(v, str) for v in raw_or_dict.values())
        else parse_raw_cookies(raw_or_dict)
    )
    if not cookie_dict:
        return "Free"

    import httpx

    u_index = str(auth_user or "0")
    headers = build_makersuite_headers(
        filter_minimal_cookies(cookie_dict),
        auth_user=u_index,
        include_visit_id=True,
    )
    tier_url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GetAiStudioBenefitTier"
    try:
        if client is not None:
            resp = await client.post(tier_url, headers=headers, content="[]")
            if resp.status_code == 200:
                return parse_benefit_tier_response(resp.text)
            return "Free"
        async with httpx.AsyncClient(
            timeout=8.0, proxy=await _resolve_probe_proxy(proxy_url)
        ) as new_cli:
            resp = await new_cli.post(tier_url, headers=headers, content="[]")
            if resp.status_code == 200:
                return parse_benefit_tier_response(resp.text)
    except Exception as e:
        logger.debug("探测子账号 u/%s 权益等级失败: %s", u_index, e)
    return "Free"


async def probe_aistudio_session(
    raw_or_dict: object,
    *,
    client: httpx.AsyncClient | None = None,
    user_agent: str | None = None,
    proxy_url: str | None = None,
) -> tuple[bool, str]:
    """探测 AI Studio 登录会话是否仍然存活。

    访问 https://aistudio.google.com/（不跟随重定向）：
    - 200 OK：会话存活 -> (True, "ok")
    - 302 跳转至 /welcome 或 ServiceLogin：会话已登出/失效 -> (False, "signed_out")
    - 其他状态或网络抖动：默认视为存活，避免误杀有效凭据。
    """
    cookie_dict = (
        raw_or_dict
        if isinstance(raw_or_dict, dict)
        and all(isinstance(v, str) for v in raw_or_dict.values())
        else parse_raw_cookies(raw_or_dict)
    )
    min_cookies = filter_minimal_cookies(cookie_dict)
    if not min_cookies:
        return False, "no_cookies"

    import httpx

    cookie_header = "; ".join(f"{k}={v}" for k, v in min_cookies.items())
    headers = {
        "Cookie": cookie_header,
        "User-Agent": user_agent or DEFAULT_BROWSER_USER_AGENT,
    }

    async def _do_probe(http_client: httpx.AsyncClient) -> tuple[bool, str]:
        resp = await http_client.get(
            "https://aistudio.google.com/",
            headers=headers,
            follow_redirects=False,
        )
        if 300 <= resp.status_code < 400:
            loc = resp.headers.get("location") or ""
            if re.search(
                r"/welcome\b|ServiceLogin|accounts\.google\.com", loc, re.IGNORECASE
            ):
                return False, "signed_out"
            return True, "redirect"
        if resp.status_code == 200:
            return True, "ok"
        return True, f"http_{resp.status_code}"

    try:
        if client is not None:
            return await _do_probe(client)
        async with httpx.AsyncClient(
            timeout=10.0,
            proxy=await _resolve_probe_proxy(proxy_url),
            follow_redirects=False,
        ) as new_client:
            return await _do_probe(new_client)
    except Exception as e:
        return True, f"probe_error:{e}"


async def rotate_google_cookies(
    cookies: dict[str, str],
    *,
    client: httpx.AsyncClient | None = None,
    user_agent: str | None = None,
    timeout: float = 15.0,
    proxy_url: str | None = None,
) -> dict[str, object]:
    """调用 accounts.google.com/RotateCookies 刷新短效滚动凭据 (__Secure-1PSIDTS / SIDCC)。

    若调用成功，原地更新传入的 cookies 字典并返回 {"ok": True, "status": 200, "updated": [...]}。
    """
    if not cookies:
        return {"ok": False, "status": 0, "updated": []}

    has_rotation_seed = any(
        cookies.get(k)
        for k in (
            "LSID",
            "__Host-1PLSID",
            "__Host-3PLSID",
            "__Secure-1PSIDTS",
            "__Secure-3PSIDTS",
        )
    )
    if not has_rotation_seed:
        return {"ok": False, "status": 0, "updated": [], "skipped": "missing_seed"}

    import httpx

    rotate_cookies_map = filter_minimal_cookies(cookies, include_account_host=True)
    cookie_header = "; ".join(f"{k}={v}" for k, v in rotate_cookies_map.items())
    if not cookie_header:
        return {"ok": False, "status": 0, "updated": []}

    effective_ua = user_agent
    if not effective_ua:
        try:
            from aistudio_api.infrastructure.gateway.model_defaults import (
                get_active_waa_server,
            )

            active_waa = get_active_waa_server()
            effective_ua = str(
                active_waa.get("user_agent") or DEFAULT_BROWSER_USER_AGENT
            )
        except Exception:
            effective_ua = DEFAULT_BROWSER_USER_AGENT

    headers = {
        "Cookie": cookie_header,
        "User-Agent": effective_ua,
        "Content-Type": "application/json",
        "Origin": "https://accounts.google.com",
        "Referer": "https://accounts.google.com/RotateCookiesPage",
    }
    body = '[null,"-1234567890",0]'

    async def _do_rotate(http_client: httpx.AsyncClient) -> dict[str, object]:
        resp = await http_client.post(
            "https://accounts.google.com/RotateCookies",
            headers=headers,
            content=body,
        )
        set_cookies = resp.headers.get_list("set-cookie")
        updated = apply_set_cookies(cookies, set_cookies)
        return {
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "updated": updated,
        }

    try:
        if client is not None:
            return await _do_rotate(client)
        async with httpx.AsyncClient(
            timeout=timeout, proxy=await _resolve_probe_proxy(proxy_url)
        ) as new_client:
            return await _do_rotate(new_client)
    except Exception as e:
        logger.debug("请求 RotateCookies 失败: %s", e)
        return {"ok": False, "status": 0, "updated": [], "error": str(e)}


async def probe_google_accounts_infinite(
    raw_or_dict: object,
    *,
    max_fails_in_a_row: int = 1,
    max_sub_accounts: int = 16,
    probe_deadline_seconds: float = 120.0,
    proxy_url: str | None = None,
) -> list[dict[str, object]]:
    """向下探测多账号登录索引（u/0, u/1, u/2...），直到探测失败为止。

    防护：设硬上限 max_sub_accounts 与总时长 deadline；连续多次 403
    （中间从未出现 200/400）视为 IP/配额级封锁并停止，避免无限请求风暴。

    Returns:
        有效账号列表，例如 [{"auth_user": "0", "status_code": 200, "name": "Google Account (u/0)"}, ...]
    """
    cookie_dict = parse_raw_cookies(raw_or_dict)
    if not cookie_dict:
        return []

    import httpx

    valid_accounts: list[dict[str, object]] = []

    auth_user_idx = 0
    consecutive_failures = 0
    consecutive_forbidden = 0
    deadline = time.monotonic() + probe_deadline_seconds
    async with httpx.AsyncClient(
        timeout=10.0, proxy=await _resolve_probe_proxy(proxy_url)
    ) as client:
        emails_map = await probe_account_emails_map(
            cookie_dict, auth_user="0", client=client
        )
        while auth_user_idx < max_sub_accounts:
            if time.monotonic() > deadline:
                logger.warning(
                    "子账号探活达到总时长上限 %.0fs，已探测 %d 个索引后停止",
                    probe_deadline_seconds,
                    auth_user_idx,
                )
                break
            u_index = str(auth_user_idx)
            headers = build_makersuite_headers(
                filter_minimal_cookies(cookie_dict),
                auth_user=u_index,
                include_visit_id=True,
            )
            url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels"
            try:
                resp = await client.post(url, headers=headers, content="[]")
                # 200 (OK), 400 (Bad Request on param but authed), 403 (Forbidden/Permission/Quota) 均表示此账号 Session 存在
                # 401 表示该 auth_user 未登录或 Session 无效
                if resp.status_code in (200, 400, 403):
                    consecutive_failures = 0
                    if resp.status_code == 403:
                        consecutive_forbidden += 1
                        if consecutive_forbidden >= 3:
                            # 连续 403 且从未出现 200/400：大概率是出口 IP
                            # 或账号整体被封锁，继续探测只会触发风控。
                            logger.warning(
                                "子账号探活连续 %d 次 HTTP 403（无成功响应），判定为 IP/配额级封锁，停止探测",
                                consecutive_forbidden,
                            )
                            break
                    else:
                        consecutive_forbidden = 0
                    detected_tier = "Free"
                    tier_url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GetAiStudioBenefitTier"
                    try:
                        tier_resp = await client.post(
                            tier_url, headers=headers, content="[]"
                        )
                        if tier_resp.status_code == 200:
                            detected_tier = parse_benefit_tier_response(tier_resp.text)
                    except Exception:
                        pass
                    detected_email = emails_map.get(u_index)
                    if not detected_email:
                        try:
                            extra_map = await probe_account_emails_map(
                                cookie_dict, auth_user=u_index, client=client
                            )
                            emails_map.update(extra_map)
                            detected_email = emails_map.get(u_index)
                        except Exception:
                            pass
                    valid_accounts.append(
                        {
                            "auth_user": u_index,
                            "status_code": resp.status_code,
                            "name": f"Google Account (u/{u_index})",
                            "email": detected_email,
                            "tier": detected_tier,
                        }
                    )
                    logger.info(
                        "探活子账号 u/%s 成功: 状态码=%d, 邮箱=%s, 等级=%s",
                        u_index,
                        resp.status_code,
                        mask_email(detected_email) if detected_email else "无",
                        detected_tier,
                    )
                elif resp.status_code == 401:
                    consecutive_failures += 1
                    logger.debug("子账号 u/%s 未授权 (HTTP 401)", u_index)
                else:
                    consecutive_failures += 1
                    logger.debug(
                        "子账号 u/%s 探活返回 HTTP %d", u_index, resp.status_code
                    )
            except Exception as e:
                consecutive_failures += 1
                logger.debug("子账号 u/%s 探活请求异常: %s", u_index, e)

            if consecutive_failures >= max_fails_in_a_row:
                break

            if auth_user_idx + 1 >= max_sub_accounts:
                logger.info(
                    "子账号探活达到硬上限 %d 个索引，停止（如需探测更多请调整 max_sub_accounts）",
                    max_sub_accounts,
                )
                break

            auth_user_idx += 1

    return valid_accounts
