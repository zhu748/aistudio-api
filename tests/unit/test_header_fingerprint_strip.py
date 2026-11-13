"""上游请求头指纹剥离（白名单）测试。

验证网关出站请求只携带 AI Studio 逆向指纹头：客户端/应用侧的任何
头（SDK 指纹、代理头、自定义头）在 transport 边界被剥除；且大小写
变体折叠为小写键（防止 WAA 签名服务的 Go canonical 头与 Python 侧
字面量大小写不一致时产生重复头）。
"""

from __future__ import annotations

from aistudio_api.infrastructure.gateway.transport import (
    _UPSTREAM_ALLOWED_HEADERS,
    strip_to_aistudio_fingerprint,
)


def test_client_fingerprint_headers_stripped() -> None:
    """客户端应用指纹头（SDK / 代理 / 追踪）全部剥除。"""
    headers = {
        "Content-Type": "application/json+protobuf",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) rv:152.0",
        # —— 客户端应用侧头，绝不允许出站 ——
        "X-Stainless-Lang": "python",
        "X-Stainless-Package-Version": "3.50.0",
        "X-Stainless-Retry-Count": "2",
        "X-App-Name": "ClaudeCode",
        "X-Client-Version": "1.0.0",
        "anthropic-version": "2023-06-01",
        "X-Title": "my-app",
        "HTTP-Referer": "https://myapp.example.com",
        "X-Forwarded-For": "203.0.113.5",
        "X-Real-IP": "203.0.113.5",
        "X-Request-Id": "req-abc",
        "traceparent": "00-trace-span-01",
        "CF-Connecting-IP": "198.51.100.7",
        "Accept-Encoding": "br, gzip",  # httpx 传输层管辖
    }
    result = strip_to_aistudio_fingerprint(headers)
    # 键统一折叠为小写（大小写变体折叠是防重复头的关键契约）
    assert set(result) == {"content-type", "user-agent"}


def test_aistudio_fingerprint_headers_kept() -> None:
    """AI Studio 逆向指纹头全部保留（键折叠为小写）。"""
    headers = {
        "content-type": "application/json+protobuf",
        "origin": "https://aistudio.google.com",
        "referer": "https://aistudio.google.com/",
        "user-agent": "Mozilla/5.0 Firefox/152.0",
        "x-user-agent": "grpc-web-javascript/0.1",
        "x-goog-api-key": "AIzaSy...",
        "x-goog-authuser": "0",
        "x-goog-ext-519733851-bin": "CAASAUIw...",
        "x-aistudio-g1-tier": "TIER1",
        "x-aistudio-visit-id": "v1_abcd",
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "authorization": "SAPISIDHASH auth",
        "cookie": "SAPISID=xxx",
    }
    result = strip_to_aistudio_fingerprint(headers)
    # 全部在白名单内：值保留，键折叠为小写
    assert set(result) == set(headers)
    assert result == headers


def test_case_variants_fold_to_single_key() -> None:
    """大小写变体折叠为单键（后写入者胜出），绝不产生重复头。

    WAA 签名服务（Go）返回 ``X-Goog-Authuser``（Go canonical），Python
    侧字面量是 ``X-Goog-AuthUser``——若不折叠，dict.update 会新增而非
    覆盖，上游将收到值冲突的重复头。
    """
    headers = {
        "X-Goog-AuthUser": "u0",  # Python canonical
        "X-Goog-Authuser": "u5",  # Go canonical（后写入，应胜出）
        "AUTHORIZATION": "SAPISIDHASH old",
        "authorization": "SAPISIDHASH new",
    }
    result = strip_to_aistudio_fingerprint(headers)
    assert set(result) == {"x-goog-authuser", "authorization"}
    assert result["x-goog-authuser"] == "u5"
    assert result["authorization"] == "SAPISIDHASH new"


def test_whitelist_matches_makersuite_header_set() -> None:
    """白名单必须覆盖 build_makersuite_headers 的全部产物头名。"""
    from aistudio_api.infrastructure.account.cookie_parser import (
        build_makersuite_headers,
    )

    produced = build_makersuite_headers(
        {"SAPISID": "x"}, auth_user="1", tier="Pro", include_visit_id=True
    )
    stripped = strip_to_aistudio_fingerprint(dict(produced))
    assert set(stripped) == {k.lower() for k in produced}, (
        f"build_makersuite_headers 产物被误剥离: "
        f"{ {k.lower() for k in produced} - set(stripped) }"
    )


def test_waa_dynamic_header_names_within_whitelist() -> None:
    """waa-signer 返回的动态头名集合是白名单子集（返回头集与真实浏览器
    VM 会话绑定的 UA / visit-id / api-key 等，全部落在允许集内）。"""
    waa_dynamic = {
        "User-Agent": "Mozilla/5.0 ...",
        "X-User-Agent": "grpc-web-javascript/0.1",
        "X-Goog-Api-Key": "AIzaSy...",
        "X-Goog-Authuser": "0",
        "X-Goog-Ext-519733851-Bin": "CAASAUIw...",
        "X-AIStudio-Visit-Id": "v1_xyz",
        "X-AIStudio-G1-Tier": "TIER1",
    }
    stripped = strip_to_aistudio_fingerprint(waa_dynamic)
    assert set(stripped) == {k.lower() for k in waa_dynamic}


def test_empty_and_junk_input() -> None:
    assert strip_to_aistudio_fingerprint({}) == {}
    assert strip_to_aistudio_fingerprint({"Junk-Header": "x"}) == {}


def test_allowed_set_is_frozen_and_minimal() -> None:
    """允许集是冻结常量，且不含任何非浏览器头。"""
    assert isinstance(_UPSTREAM_ALLOWED_HEADERS, frozenset)
    forbidden = {
        "x-stainless-lang",
        "anthropic-version",
        "x-app-name",
        "x-forwarded-for",
        "host",
        "content-length",
        "accept-encoding",
        "connection",
        "upgrade-insecure-requests",
        "sec-ch-ua",
    }
    assert not (forbidden & _UPSTREAM_ALLOWED_HEADERS)
