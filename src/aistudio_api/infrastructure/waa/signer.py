"""WAA proof signing client for the local waa-signer daemon.

与容器内置的本地原生签名引擎（waa-signer，127.0.0.1:8081 的
/v1/proof/mint HTTP 协议）通信铸造 proof token。远程公共签名服务
（spinsnow / aistudio-pg-bridge 等）与多节点切换支持已移除：本地
引擎是唯一签名方式，无浏览器、无外部依赖。
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

import httpx

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.account.cookie_parser import (
    filter_minimal_cookies,
    get_tier_header_value,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.proxy_bridge import (
    ProxyBridgeError,
    normalize_proxy_for_go_signer,
    resolve_proxy_url_async,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("waa.signer")


def signer_authorization(server_url: str) -> dict[str, str]:
    """本地签名节点的共享密钥鉴权头（AISTUDIO_SIGNER_SECRET 可选）。

    绑定节点凭证防止把密钥发给非预期地址；本地节点恒为本机回环。
    """
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.waa.daemon import is_local_signer_url

    secret = settings.signer_secret if is_local_signer_url(server_url) else ""
    return {"Authorization": f"Bearer {secret}"} if secret else {}


# Per-task storage: WaaSigner/LocalProofSigner is shared across concurrent
# requests. A plain instance attribute lets one request's proof headers be
# overwritten by another task before last_headers reads them, mismatching the
# proof with the headers and leaking cookies across accounts.
_LAST_HEADERS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "aistudio_waa_last_headers", default=None
)


def _retry_delay(attempt: int) -> float:
    """指数退避：0.5s、1.0s……上限 2s（签名服务抖动通常秒级恢复）。"""
    return min(2.0, 0.5 * (2 ** (attempt - 1)))


# 仅这些网关级状态码代表「上游瞬时不可用」，可安全退避重试。
# 500 常带业务语义（Cookie 失效等），重试只会重复同一失败。
_RETRYABLE_GATEWAY_STATUS = frozenset({502, 503, 504})


def _iter_digest_parts(contents: list[AistudioContent]) -> Iterator[str]:
    """按 digest 规则逐个产出部件文本（两条摘要函数的唯一真源）。

    AI Studio 前端（live_m=_b.js: _.gD / _.jn / _.VC）只取 contents：
    text → getText()、inline_data → data.Fe()、file → getId()，没有载荷
    的部件贡献空串；各部件之间用单个空格连接。
    """
    for content in contents:
        for part in content.parts:
            if part.text is not None:
                yield str(part.text)
            elif part.inline_data is not None:
                yield str(part.inline_data[1])
            elif part.file_id is not None:
                yield str(part.file_id)
            else:
                yield ""


def extract_content_digest(contents: list[AistudioContent]) -> tuple[str, str]:
    """Strictly extract contents for SHA-256 digest calculation.

    Returns (prompt_string, sha256_hexdigest)。仅测试与调试使用；生产签名
    路径走 :func:`compute_content_digest`，避免拼出整段 prompt 字符串。
    """
    prompt = " ".join(_iter_digest_parts(contents))
    # errors="replace"：text 内的孤立 UTF-16 代理项会让严格编码抛
    # UnicodeEncodeError（签名前奏即崩，请求 500）。摘要仅需稳定字节串，
    # 替换为 U+FFFD 后 hash 仍确定（上游 DeconstructedCube 036e618 同款）。
    content_hash = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()
    return prompt, content_hash


def compute_content_digest(contents: list[AistudioContent]) -> tuple[int, str]:
    """Compute the same SHA-256 digest as :func:`extract_content_digest` without
    materialising the joined prompt string.

    与 extract_content_digest 字节级一致（同一组部件、同一单空格分隔、同一
    ``errors="replace"``，均来自 :func:`_iter_digest_parts`），但按部件增量
    喂给 sha256。签名链路只需要摘要，不再把整段对话文本拼成一个 str
    （PEP 393：正文含一个 emoji 就会让整段 str 按 4 字节/字符驻留）再
    encode 出一份 bytes。
    Returns (prompt_chars, sha256_hexdigest)。
    """
    hasher = hashlib.sha256()
    prompt_chars = 0
    first = True
    for text in _iter_digest_parts(contents):
        if first:
            first = False
        else:
            hasher.update(b" ")
            prompt_chars += 1
        hasher.update(text.encode("utf-8", errors="replace"))
        prompt_chars += len(text)
    return prompt_chars, hasher.hexdigest()


def _encode_json_bytes(payload: dict[str, Any]) -> bytes:
    """Encode a payload to the exact bytes httpx ``json=`` would send, chunk by chunk.

    httpx 的 ``json=`` 先 ``json.dumps`` 出整份 str 再 ``.encode()``：含非 BMP
    字符时该 str 按 4 字节/字符驻留，一份 mint 载荷会同时以 str 与 bytes 两份
    形态存在。这里用同一组参数（ensure_ascii=False, allow_nan=False,
    separators=(",", ":")）走 ``iterencode`` 分块编码，字节输出完全一致，
    但不再物化整份 str。
    """
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return b"".join(chunk.encode("utf-8") for chunk in encoder.iterencode(payload))


def _resolve_signer_timeout() -> float:
    """解析 mint 请求的 HTTP 总超时（秒）。

    Go 侧 /v1/proof/mint 的处理预算是 90s（冷启动含 loadPage 网络往返 +
    SpiderMonkey bootstrap，慢代理下可到 30-90s）。原先固定 30s：冷启动
    落在 30~90s 区间的本地签名器会被 Python 侧先掐断（Go 收到断连取消
    冷启动、进度全丢），重试又从零开始 → 2 次耗尽后签名器不可用。
    故默认 95s 覆盖 Go 的 90s 预算；``AISTUDIO_SIGNER_TIMEOUT`` 显式
    覆盖（下限 5s）。
    """
    raw = os.getenv("AISTUDIO_SIGNER_TIMEOUT")
    if raw and raw.strip():
        try:
            return max(5.0, float(raw.strip()))
        except ValueError:
            pass
    return 95.0


class LocalProofSigner:
    """本地 WAA 签名守护进程（waa-signer）的 proof 铸造客户端。

    ``server_url`` 参数仅供测试注入模拟服务地址；生产路径省略时固定
    解析为本地节点（127.0.0.1:8081）。
    """

    def __init__(
        self,
        server_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        if server_url is None:
            from aistudio_api.infrastructure.gateway.model_defaults import (
                get_local_waa_server,
            )

            server_url = str(get_local_waa_server().get("url") or "")
        self.server_url = server_url.rstrip("/")
        self.timeout = _resolve_signer_timeout() if timeout is None else timeout
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        # 惰性建池互斥：进程级单例被首批并发签名请求共享，
        # 无锁并发首调会各建一个 AsyncClient、后赋值者胜出，
        # 其余实例失去引用泄漏连接池（同 transport._get_client 的教训）。
        self._client_lock = threading.Lock()

    @property
    def last_headers(self) -> dict[str, str]:
        """Return dynamic headers minted by the *current* asyncio task."""
        return _LAST_HEADERS.get() or {}

    @last_headers.setter
    def last_headers(self, value: dict[str, str]) -> None:
        _LAST_HEADERS.set(value)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or lazily initialize shared HTTP client with connection pooling.

        本机回环连接不走出站代理：签名器与主服务同容器，走了代理只会把
        127.0.0.1:8081 的请求发往代理侧的回环地址（永远探不到本机）。
        httpx.AsyncClient 的构造不触网，同步临界区即可，无需 await；
        双重检查防并发首调重复建池。
        """
        if self._closed:
            raise TransportError("Proof signer is closed")
        if self._client is not None and not self._client.is_closed:
            return self._client
        with self._client_lock:
            if self._closed:
                raise TransportError("Proof signer is closed")
            if self._client is None or self._client.is_closed:
                from aistudio_api.infrastructure.gateway.transport import (
                    _shared_ssl_context,
                )

                self._client = httpx.AsyncClient(
                    timeout=self.timeout,
                    trust_env=False,
                    verify=_shared_ssl_context(),
                    limits=httpx.Limits(
                        max_keepalive_connections=20, max_connections=50
                    ),
                )
        return self._client

    async def close(self) -> None:
        """Close shared HTTP client."""
        with self._client_lock:
            self._closed = True
            client = self._client
            self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def mint_proof(
        self,
        *,
        cookies: dict[str, str],
        auth_user: str = "0",
        tier: str | None = None,
        model: str = "models/gemini-3.7-flash",
        prompt: str = "",
        wire_contents: list[Any] | None = None,
        upstream_proxy: str | None = None,
        digest: str | None = None,
    ) -> str:
        """Request proof from remote proof server with automatic retry on transient failure.

        ``digest`` 非空时只发送摘要（签名器在 ``req.Digest`` 非空时跳过
        ``sha256(req.Prompt)``），整段对话文本 ``prompt`` 不再进入载荷——
        否则每次签名都要把全文 JSON 再序列化一遍。``prompt`` 仍保留给
        直接调用的旧路径（未提供 digest 时行为与此前完全一致）。
        """
        min_cookies = filter_minimal_cookies(cookies)
        cookie_header = "; ".join(f"{k}={v}" for k, v in min_cookies.items())
        if not cookie_header:
            raise ValueError("Cannot mint proof: cookies are empty")

        g1_tier = get_tier_header_value(tier) or "TIER0"
        normalized_model = model if model.startswith("models/") else f"models/{model}"

        payload: dict[str, Any] = {
            "cookies": cookie_header,
            "authUser": int(auth_user) if auth_user.isdigit() else 0,
            "g1Tier": g1_tier,
            "model": normalized_model,
        }
        if digest:
            payload["digest"] = digest
        else:
            payload["prompt"] = prompt
        if wire_contents is not None:
            payload["wireContents"] = wire_contents
        if upstream_proxy:
            # Go 签名器的 scheme 白名单只认 http/https/socks5（详见
            # normalize_proxy_for_go_signer），turn/turns/sstp 也必须先解析为
            # 本地回环 SOCKS5。签名与模型请求必须同出口 IP，故始终随请求传
            # 当前代理；显式 direct 哨兵需透传，覆盖签名器启动期默认代理。
            try:
                resolved_proxy = await resolve_proxy_url_async(upstream_proxy)
            except ProxyBridgeError as exc:
                raise TransportError(f"出站代理解析失败: {exc}") from exc
            if resolved_proxy:
                payload["proxy"] = normalize_proxy_for_go_signer(resolved_proxy)
            else:
                payload["proxy"] = "direct"
        request_headers = {
            "Content-Type": "application/json",
            **signer_authorization(self.server_url),
        }
        # 重试循环外编码一次：重试时复用同一份 bytes，不再重复序列化。
        payload_bytes = _encode_json_bytes(payload)

        max_attempts = 2
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.perf_counter()
                client = await self._get_client()
                resp = await client.post(
                    f"{self.server_url}/v1/proof/mint",
                    content=payload_bytes,
                    headers=request_headers,
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000

                if resp.status_code != 200:
                    # resp.text 只取一次：它是 MB 级响应体的完整解码副本。
                    detail_text = resp.text[:300]
                    detail = (
                        f"Local proof signer returned HTTP {resp.status_code}: "
                        f"{detail_text}"
                    )
                    if (
                        resp.status_code in _RETRYABLE_GATEWAY_STATUS
                        and attempt < max_attempts
                    ):
                        # 仅网关级瞬时不可用（502/503/504，通常为重启或过载）值得退避重试；
                        # 500 多数携带业务语义（如 Cookie 失效），重试只会重复失败，
                        # 4xx 同理，故直接上抛。
                        last_error = TransportError(detail)
                        logger.warning(
                            "签名服务 %d (%d/%d): server=%s, detail=%s",
                            resp.status_code,
                            attempt,
                            max_attempts,
                            self.server_url,
                            detail_text,
                        )
                        await asyncio.sleep(_retry_delay(attempt))
                        continue
                    raise TransportError(detail)

                data = resp.json()
                if not data.get("ok") or not data.get("proof"):
                    err_msg = data.get("error") or "unknown proof server error"
                    raise TransportError(f"Proof minting failed: {err_msg}")

                proof = str(data["proof"])
                raw_headers = data.get("headers")
                if isinstance(raw_headers, dict):
                    self.last_headers = {str(k): str(v) for k, v in raw_headers.items()}
                else:
                    self.last_headers = {}
                pool_state = str(data.get("poolState") or "unknown")
                logger.debug(
                    "生成 WAA 签名成功: pool=%s, len=%d, headers=%d, 耗时=%.1fms",
                    pool_state,
                    len(proof),
                    len(self.last_headers),
                    elapsed_ms,
                )
                return proof

            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_error = e
                logger.warning(
                    "连接签名服务失败 (%d/%d): server=%s, error=%s",
                    attempt,
                    max_attempts,
                    self.server_url,
                    e,
                )
                # HTTPX discards failed connections from its pool. Keep the shared
                # client alive so one request cannot close connections used by peers.
                if attempt < max_attempts:
                    await asyncio.sleep(_retry_delay(attempt))
            except TransportError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    "请求签名服务异常 (%d/%d): server=%s, error=%s",
                    attempt,
                    max_attempts,
                    self.server_url,
                    e,
                )
                # Client is shared across concurrent requests; a single malformed
                # response must not close connections currently used by other tasks.
                if attempt < max_attempts:
                    await asyncio.sleep(_retry_delay(attempt))

        raise TransportError(
            f"Proof minting failed after {max_attempts} attempts "
            f"(server={self.server_url}): {last_error}"
        )


class WaaSigner:
    """Unified WAA signing facade backed by the local proof signer."""

    # WAA proof 复用缓存（2026-11 HAR + 代理实测逆向结论，行为复杂):
    #   - 浏览器级 attestation（真机抓包 proof / 本地 SpiderMonkey 引擎）
    #     重放 3 天后仍 200，且与 contents 的 SHA-256 摘要严格绑定
    #     （同 token 换 prompt 立即 403）；
    #   - 历史远程签名服务（spinsnow / NodeVM，已移除支持）的 proof 曾
    #     观测到「单次使用」倾向，E2E 实测同一 proof 第 3 次发送可恢复
    #     200 —— 上游拒绝存在概率性，403 阶梯前两跳免费重放具有真实
    #     恢复价值；
    #   - X-AIStudio-Visit-Id / UA 与 proof 无确定性绑定关系（3×3 交叉
    #     实验证实 403 为概率性而非头缺失导致）。
    #   - 2026-11-12 实测补充：远程服务级挑战过期（全部节点 mint 携带
    #     同一过期 challenge_version）时 mint ok:true 但 proof 被上游
    #     100% 拒绝 —— 由 SignerHealthTracker 跨账号熔断兜底
    #     （waa/health.py），本地引擎不受该类服务级事故影响。
    # 因此缓存默认关闭（TTL=0）：本地 SpiderMonkey 签名器属
    # “浏览器级 attestation”场景，可按需开启（建议先小 TTL 实测重放性）。
    # 403 重签路径会主动失效缓存，作为兜底防护。
    _PROOF_CACHE_TTL_ENV = "AISTUDIO_WAA_PROOF_CACHE_TTL"
    _PROOF_CACHE_MAX_ENTRIES = 256

    @staticmethod
    def _proof_cache_ttl() -> float:
        raw = os.getenv(WaaSigner._PROOF_CACHE_TTL_ENV, "0")
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 0.0

    def __init__(self) -> None:
        self._local_signer = LocalProofSigner()
        self._closed = False
        # Bind proofs to the complete credential fingerprint and upstream route.
        self._proof_cache: OrderedDict[
            tuple[str, str, str, str, str, str],
            tuple[float, str, dict[str, str]],
        ] = OrderedDict()

    def invalidate_proof_cache(self, content_hash: str | None = None) -> int:
        """失效 proof 缓存；content_hash 为空时清空全部，返回清除条数。

        403 重签路径调用（Auth403RetryManager 第 3 次重试）：此时缓存
        proof 可能已失效或被风控标记，必须强制重新走签名服务。
        带摘要失效时清除该 content_hash 下全部条目（跨账号/模型/层级：
        缓存键同时包含凭据和出口，同一摘要可存在多条）。
        """
        if content_hash is None:
            removed = len(self._proof_cache)
            self._proof_cache.clear()
            if removed:
                logger.info("WAA proof 缓存已全量失效: %d 条", removed)
            return removed
        stale = [key for key in self._proof_cache if key[0] == content_hash]
        for key in stale:
            self._proof_cache.pop(key)
        return len(stale)

    def _cache_key(
        self,
        content_hash: str,
        cookies: dict[str, str],
        auth_user: str,
        tier: str,
        model: str,
        upstream_proxy: str | None,
    ) -> tuple[str, str, str, str, str, str]:
        credentials = json.dumps(
            filter_minimal_cookies(cookies), sort_keys=True, separators=(",", ":")
        )
        anchor = hashlib.sha256(
            credentials.encode("utf-8", errors="replace")
        ).hexdigest()
        return (
            content_hash,
            str(auth_user),
            str(tier),
            str(model),
            anchor,
            upstream_proxy or "",
        )

    async def close(self) -> None:
        """Close underlying local signer resources."""
        self._closed = True
        self.invalidate_proof_cache()
        await self._local_signer.close()

    @property
    def server_url(self) -> str:
        """本地签名节点地址（固定 127.0.0.1:8081）。"""
        return self._local_signer.server_url

    @property
    def last_headers(self) -> dict[str, str]:
        """Get dynamic protocol headers returned by the last successful proof minting."""
        return self._local_signer.last_headers

    async def generate_snapshot(
        self,
        contents: list[AistudioContent],
        cookies: dict[str, str],
        auth_user: str = "0",
        tier: str = "Free",
        model: str = "models/gemini-3.7-flash",
        upstream_proxy: str | None = None,
    ) -> str:
        """Compute content hash and generate WAA proof token (with reuse cache)."""
        if self._closed:
            raise TransportError("Proof signer is closed")
        prompt_chars, content_hash = compute_content_digest(contents)
        logger.debug(
            "计算请求摘要: hash=%s, prompt_len=%d, u/%s",
            content_hash[:8],
            prompt_chars,
            auth_user,
        )

        # 1) 缓存命中路径：命中时同时恢复动态头（签名服务返回的 UA/头
        # 需要与 proof 成对使用，避免 proof 与指纹头错配）。
        ttl = self._proof_cache_ttl()
        key = (
            self._cache_key(
                content_hash, cookies, auth_user, tier, model, upstream_proxy
            )
            if ttl > 0
            else None
        )
        if key is not None:
            cached = self._proof_cache.get(key)
            if cached is not None:
                ts, proof, cached_headers = cached
                if time.monotonic() - ts < ttl:
                    self._proof_cache.move_to_end(key)
                    self._local_signer.last_headers = dict(cached_headers)
                    logger.debug(
                        "WAA proof 缓存命中: hash=%s, age=%.0fs, u/%s",
                        content_hash[:8],
                        time.monotonic() - ts,
                        auth_user,
                    )
                    return proof
                self._proof_cache.pop(key, None)

        # 2) 未命中：请求本地签名守护进程铸造 proof（mint_proof 内部已
        # 含瞬时故障的退避重试）。只传摘要：签名器侧 digest 非空即直接使用，
        # wireContents 仍需携带（签名器从中取最后一轮文本作为 pagePrompt）。
        proof = await self._local_signer.mint_proof(
            cookies=cookies,
            auth_user=auth_user,
            tier=tier,
            model=model,
            digest=content_hash,
            wire_contents=[c.to_wire() for c in contents],
            upstream_proxy=upstream_proxy,
        )

        # 3) 回填缓存（LRU 容量淘汰）。失败不回填；mint_proof 内部已重试。
        if key is not None and proof and not self._closed:
            self._proof_cache[key] = (
                time.monotonic(),
                proof,
                dict(self._local_signer.last_headers),
            )
            self._proof_cache.move_to_end(key)
            while len(self._proof_cache) > self._PROOF_CACHE_MAX_ENTRIES:
                self._proof_cache.popitem(last=False)
        return proof
