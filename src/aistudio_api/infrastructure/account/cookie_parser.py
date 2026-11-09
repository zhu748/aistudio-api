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
    return base64.b64encode(_EXT_BIN_PREFIX + reg.encode("ascii") + _EXT_BIN_SUFFIX).decode("ascii")


DEFAULT_EXT_BIN = build_ext_bin()

# 真实 Chrome / Edge 发送 AI Studio 请求时附带的固定浏览器特征头。
# 缺失这些头可能被风控指纹识别（HAR 实测于 2026-11）。
# x-browser-validation 为会话级校验值；若 WAA 签名服务返回动态同名头，会覆盖此静态值。
BROWSER_SIGNATURE_HEADERS: dict[str, str] = {
    "X-Browser-Channel": "stable",
    "X-Browser-Copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
    "X-Browser-Year": "2026",
    "X-Browser-Validation": "Whavu7Z6lTQOg+XljzhS45x/NWQ=",
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
    """过滤保留核心鉴权 Cookie；默认排除 accounts.google.com 专属 Cookie。"""
    allowed = (
        ROTATE_COOKIE_WHITELIST if include_account_host else MINIMAL_COOKIE_WHITELIST
    )
    return {k: v for k, v in cookies.items() if k in allowed and v}


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
        **BROWSER_SIGNATURE_HEADERS,
    }
    if include_visit_id:
        headers["X-AIStudio-Visit-Id"] = f"v1_{uuid.uuid4()}"
    tier_header = get_tier_header_value(tier)
    if tier_header:
        headers["X-AIStudio-G1-Tier"] = tier_header
    if cookies:
        auth_header = calculate_sapisid_hash(cookies)
        if auth_header:
            headers["Authorization"] = auth_header
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
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
    "socks4://",
    "socks5://",
    "socks5h://",
)


def normalize_proxy_url(candidate: str | None) -> str | None:
    """规整代理地址：补全 ``http://`` 前缀，无法识别时返回 None。"""
    if not candidate:
        return None
    text = candidate.strip().rstrip("/")
    if not text:
        return None
    if text.lower().startswith(_PROXY_SCHEMES):
        return text
    # host:port 形式视为隐式 HTTP 代理（如 127.0.0.1:7890）
    if re.fullmatch(r"[A-Za-z0-9_.\-]+:\d{2,5}", text):
        return f"http://{text}"
    return None


def _looks_like_proxy(candidate: str) -> bool:
    """判断片段是否形似代理地址：含协议头或 host:port，且不含 Cookie 特征（= / ;）。"""
    text = candidate.strip()
    if not text or "=" in text or ";" in text:
        return False
    if "://" in text:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]+:\d{2,5}", text))


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


def get_auth_user_from_meta(auth_file: str | Path | None) -> str:
    """Read auth_user index from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta_path = Path(auth_file).parent / "meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return str(meta.get("auth_user") or "0")
        except Exception:
            pass
    return "0"


def get_tier_from_meta(auth_file: str | Path | None) -> str:
    """Read benefit tier from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta_path = Path(auth_file).parent / "meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return normalize_benefit_tier(str(meta.get("tier") or "Free"))
        except Exception:
            pass
    return "Free"


def get_proxy_from_meta(auth_file: str | Path | None) -> str | None:
    """Read per-account proxy URL from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta_path = Path(auth_file).parent / "meta.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                raw = meta.get("proxy_url")
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

    from aistudio_api.config import settings

    u_index = str(auth_user or "0")
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
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
            timeout=8.0, proxy=proxy_url or settings.proxy_url, follow_redirects=True
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

    from aistudio_api.config import settings

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
            timeout=8.0, proxy=proxy_url or settings.proxy_url
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

    from aistudio_api.config import settings

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
            proxy=proxy_url or settings.proxy_url,
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

    from aistudio_api.config import settings

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
            timeout=timeout, proxy=proxy_url or settings.proxy_url
        ) as new_client:
            return await _do_rotate(new_client)
    except Exception as e:
        logger.debug("请求 RotateCookies 失败: %s", e)
        return {"ok": False, "status": 0, "updated": [], "error": str(e)}


async def probe_google_accounts_infinite(
    raw_or_dict: object,
    *,
    max_fails_in_a_row: int = 1,
    proxy_url: str | None = None,
) -> list[dict[str, object]]:
    """一路向下探测多账号登录索引（u/0, u/1, u/2...），直到探测失败为止。

    Returns:
        有效账号列表，例如 [{"auth_user": "0", "status_code": 200, "name": "Google Account (u/0)"}, ...]
    """
    cookie_dict = parse_raw_cookies(raw_or_dict)
    if not cookie_dict:
        return []

    import httpx

    from aistudio_api.config import settings

    valid_accounts: list[dict[str, object]] = []

    auth_user_idx = 0
    consecutive_failures = 0
    async with httpx.AsyncClient(
        timeout=10.0, proxy=proxy_url or settings.proxy_url
    ) as client:
        emails_map = await probe_account_emails_map(
            cookie_dict, auth_user="0", client=client
        )
        while True:
            u_index = str(auth_user_idx)
            headers = build_makersuite_headers(
                cookie_dict,
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

            auth_user_idx += 1

    return valid_accounts
