"""Local signer lifecycle: fixed local node, close semantics and proof cache identity."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.gateway.model_defaults import get_local_waa_server
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart
from aistudio_api.infrastructure.waa import signer as module

CONTENTS = [AistudioContent(role="user", parts=[AistudioPart(text="hi")])]
COOKIES = {"SAPISID": "same-prefix-12345-A"}


async def test_client_creation_cannot_complete_after_close():
    """close 之后惰性建池必须直接拒绝，不允许复活连接池。"""
    local = module.LocalProofSigner(server_url="http://127.0.0.1:8081")
    await local.close()
    with pytest.raises(TransportError, match="closed"):
        await local._get_client()


async def test_closed_facade_cannot_mint(monkeypatch):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner()
    signer._local_signer.mint_proof = AsyncMock(return_value="!cached")
    await signer.generate_snapshot(CONTENTS, COOKIES)
    await signer.close()
    with pytest.raises(TransportError, match="closed"):
        await signer.generate_snapshot(CONTENTS, COOKIES)


@pytest.mark.parametrize("changed", ["cookie", "route"])
async def test_proof_cache_isolated_by_full_cookie_and_upstream_route(
    monkeypatch, changed
):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner()
    mint = AsyncMock(side_effect=["!first", "!second"])
    signer._local_signer.mint_proof = mint
    try:
        assert (
            await signer.generate_snapshot(
                CONTENTS, COOKIES, upstream_proxy="http://one:1"
            )
            == "!first"
        )
        cookies = {"SAPISID": "same-prefix-12345-B"} if changed == "cookie" else COOKIES
        route = "http://two:2" if changed == "route" else "http://one:1"
        assert (
            await signer.generate_snapshot(CONTENTS, cookies, upstream_proxy=route)
            == "!second"
        )
        assert mint.await_count == 2
    finally:
        await signer.close()


async def test_empty_cached_headers_clear_previous_signature_metadata(monkeypatch):
    monkeypatch.setenv("AISTUDIO_WAA_PROOF_CACHE_TTL", "60")
    signer = module.WaaSigner()

    async def mint(**kwargs):
        signer._local_signer.last_headers = {}
        return "!empty-headers"

    signer._local_signer.mint_proof = mint
    try:
        await signer.generate_snapshot(CONTENTS, COOKIES)
        signer._local_signer.last_headers = {"Cookie": "different-account"}
        assert await signer.generate_snapshot(CONTENTS, COOKIES) == "!empty-headers"
        assert signer.last_headers == {}
    finally:
        await signer.close()


def test_facade_targets_fixed_local_node():
    """本地引擎是唯一签名方式：WaaSigner 无参构造即指向本地节点。"""
    signer = module.WaaSigner()
    local = get_local_waa_server()
    assert signer.server_url == str(local["url"])
    assert signer.server_url == "http://127.0.0.1:8081"


async def test_close_closes_underlying_client():
    signer = module.WaaSigner()
    closed = AsyncMock()
    signer._local_signer.close = closed
    await signer.close()
    closed.assert_awaited_once()


def test_signer_authorization_binds_secret_to_local_node(monkeypatch):
    """共享密钥只发给本机回环节点；非本机地址不带 Authorization。"""
    monkeypatch.setattr(
        "aistudio_api.config.settings.signer_secret", "top-secret", raising=False
    )
    headers = module.signer_authorization("http://127.0.0.1:8081")
    assert headers == {"Authorization": "Bearer top-secret"}
    # 局域网/外网地址不获取密钥（防误配泄漏）
    assert module.signer_authorization("http://192.168.1.5:8081") == {}


def test_resolve_signer_timeout_env_override(monkeypatch):
    monkeypatch.setenv("AISTUDIO_SIGNER_TIMEOUT", "7")
    assert module._resolve_signer_timeout() == 7.0
    monkeypatch.setenv("AISTUDIO_SIGNER_TIMEOUT", "1")  # 下限 5s
    assert module._resolve_signer_timeout() == 5.0


def test_resolve_signer_timeout_local_default(monkeypatch):
    """本地节点默认 95s：覆盖 Go 侧 90s 处理预算，避免冷启动被掐断。"""
    monkeypatch.delenv("AISTUDIO_SIGNER_TIMEOUT", raising=False)
    assert module._resolve_signer_timeout() == 95.0
