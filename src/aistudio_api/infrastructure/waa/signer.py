"""WAA proof signing client powered by aistudio-pg-bridge.

Connects to Lianues/aistudio-pg-bridge (https://github.com/Lianues/aistudio-pg-bridge)
or compatible WAA proof servers (e.g. https://waa.spinsnow.fun) to mint proof tokens
without local browser or heavy JavaScript runtime dependencies.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
import re
import time
from contextlib import suppress
from typing import Any

import httpx

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.account.cookie_parser import (
    filter_minimal_cookies,
    get_tier_header_value,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("waa.signer")

DEFAULT_PROOF_SERVER_URL = "https://waa.spinsnow.fun"


def signer_authorization(server_url: str) -> dict[str, str]:
    """Bind credentials to a node, preventing fallback from disclosing its secret."""
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_waa_settings,
    )
    from aistudio_api.infrastructure.waa.daemon import is_local_signer_url

    url = server_url.rstrip("/")
    secret = ""
    servers = get_configured_waa_settings().get("servers")
    if isinstance(servers, list):
        for server in servers:
            if (
                isinstance(server, dict)
                and str(server.get("url") or "").rstrip("/") == url
            ):
                secret = os.getenv(str(server.get("auth_token_env") or ""), "")
                break
    if not secret and (is_local_signer_url(url) or url == settings.signer_secret_url):
        secret = settings.signer_secret
    return {"Authorization": f"Bearer {secret}"} if secret else {}


# Per-task storage: WaaSigner/RemoteProofSigner is shared across concurrent
# requests. A plain instance attribute lets one request's proof headers be
# overwritten by another task before generate_snapshot_with_headers reads them,
# mismatching the proof with the headers and leaking cookies across accounts.
_LAST_HEADERS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "aistudio_waa_last_headers", default=None
)


def _retry_delay(attempt: int) -> float:
    """指数退避：0.5s、1.0s……上限 2s（签名服务抖动通常秒级恢复）。"""
    return min(2.0, 0.5 * (2 ** (attempt - 1)))


# 仅这些网关级状态码代表「上游瞬时不可用」，可安全退避重试。
# 500 常带业务语义（Cookie 失效等），重试只会重复同一失败。
_RETRYABLE_GATEWAY_STATUS = frozenset({502, 503, 504})


def extract_content_digest(contents: list[AistudioContent]) -> tuple[str, str]:
    """Strictly extract contents for SHA-256 digest calculation.

    Follows Google AI Studio frontend logic (live_m=_b.js: _.gD / _.jn / _.VC):
    1. Only extracts contents (field 2), never includes system_instruction
    2. Parts: text -> getText(), inline_data -> data.Fe(), file -> getId()
    3. Joined by single space " " and hashed with sha256.
    Returns (prompt_string, sha256_hexdigest).
    """
    hash_parts: list[str] = []
    for content in contents:
        for part in content.parts:
            if part.text is not None:
                hash_parts.append(str(part.text))
            elif part.inline_data is not None:
                hash_parts.append(str(part.inline_data[1]))
            elif part.file_id is not None:
                hash_parts.append(str(part.file_id))
            else:
                hash_parts.append("")
    prompt = " ".join(hash_parts)
    content_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return prompt, content_hash


class RemoteProofSigner:
    """Signs requests using external WAA proof server (aistudio-pg-bridge / spinsnow)."""

    def __init__(
        self,
        server_url: str = DEFAULT_PROOF_SERVER_URL,
        proxy_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.proxy_url = proxy_url or None
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def last_headers(self) -> dict[str, str]:
        """Return dynamic headers minted by the *current* asyncio task."""
        return _LAST_HEADERS.get() or {}

    @last_headers.setter
    def last_headers(self, value: dict[str, str]) -> None:
        _LAST_HEADERS.set(value)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or lazily initialize shared HTTP client with connection pooling."""
        if self._client is None or self._client.is_closed:
            from aistudio_api.infrastructure.waa.daemon import is_local_signer_url

            is_local = is_local_signer_url(self.server_url)
            effective_proxy = None if is_local else self.proxy_url
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                proxy=effective_proxy,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
        return self._client

    async def close(self) -> None:
        """Close shared HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _masked_proxy(self) -> str:
        """脱敏代理地址中的账号密码，用于日志输出。"""
        if not self.proxy_url:
            return "(直连)"
        return re.sub(r"//[^@/]+@", "//***@", str(self.proxy_url))

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
    ) -> str:
        """Request proof from remote proof server with automatic retry on transient failure."""
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
            "prompt": prompt,
        }
        if wire_contents is not None:
            payload["wireContents"] = wire_contents
        if upstream_proxy:
            payload["proxy"] = upstream_proxy
        request_headers = {
            "Content-Type": "application/json",
            **signer_authorization(self.server_url),
        }

        max_attempts = 2
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.perf_counter()
                client = await self._get_client()
                resp = await client.post(
                    f"{self.server_url}/v1/proof/mint",
                    json=payload,
                    headers=request_headers,
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000

                if resp.status_code != 200:
                    detail = (
                        f"Remote proof server returned HTTP {resp.status_code}: "
                        f"{resp.text[:300]}"
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
                            "签名服务 %d (%d/%d): server=%s, proxy=%s, detail=%s",
                            resp.status_code,
                            attempt,
                            max_attempts,
                            self.server_url,
                            self._masked_proxy(),
                            resp.text[:300],
                        )
                        await asyncio.sleep(_retry_delay(attempt))
                        continue
                    raise TransportError(detail)

                data = resp.json()
                if not data.get("ok") or not data.get("proof"):
                    err_msg = data.get("error") or "unknown proof server error"
                    raise TransportError(f"Remote proof minting failed: {err_msg}")

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
                    "连接签名服务失败 (%d/%d): server=%s, proxy=%s, error=%s",
                    attempt,
                    max_attempts,
                    self.server_url,
                    self._masked_proxy(),
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
            f"Remote proof minting failed after {max_attempts} attempts "
            f"(server={self.server_url}): {last_error}"
        )


class WaaSigner:
    """Unified WAA signing facade backed by RemoteProofSigner."""

    def __init__(
        self,
        proof_server_url: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        url = proof_server_url or DEFAULT_PROOF_SERVER_URL
        self._remote_signer = RemoteProofSigner(server_url=url, proxy_url=proxy_url)
        self._reconfigure_lock: asyncio.Lock | None = None
        self._delayed_close_tasks: set[asyncio.Task[None]] = set()
        self._retired_signers: set[RemoteProofSigner] = set()

    def _get_reconfigure_lock(self) -> asyncio.Lock:
        """惰性创建切换锁（避免 import 时无事件循环可用）。"""
        if self._reconfigure_lock is None:
            self._reconfigure_lock = asyncio.Lock()
        return self._reconfigure_lock

    async def reconfigure(
        self,
        server_url: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        """热切换签名节点（3.5）。

        旧实现直接改写 remote.server_url 并 close 共享连接池：正在途中的
        并发签名请求会因连接池被关闭而失败，检查与赋值之间的 await 间隙
        还存在双重进入竞态。改为整实例原子替换：新请求立即走新实例，
        旧实例延迟一个宽限期（覆盖在途请求的最大超时）后关闭。
        """
        lock = self._get_reconfigure_lock()
        current = self._remote_signer
        target_url = (server_url or current.server_url).rstrip("/")
        target_proxy = (
            (proxy_url or None) if proxy_url is not None else current.proxy_url
        )
        if current.server_url == target_url and current.proxy_url == target_proxy:
            return  # 快路径：目标未变，不进入切换序列

        async with lock:
            current = self._remote_signer
            if current.server_url == target_url and current.proxy_url == target_proxy:
                return  # 双重检查：并发切换已由先到者完成
            new_signer = RemoteProofSigner(
                server_url=target_url, proxy_url=target_proxy
            )
            # 原子替换：后续新请求立即使用新实例，在途请求仍持有旧实例引用
            self._remote_signer = new_signer
            self._retired_signers.add(current)
            logger.info(
                "签名节点已切换: %s -> %s",
                current.server_url,
                target_url,
            )

        # 旧实例延迟关闭：给在途请求一个宽限期（其超时上限为 30s）
        async def _delayed_close() -> None:
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(35.0)
            try:
                await current.close()
            except Exception as e:
                logger.debug("延迟关闭旧签名器失败: %s", e)
            finally:
                self._retired_signers.discard(current)

        try:
            task = asyncio.create_task(_delayed_close())
            self._delayed_close_tasks.add(task)
            task.add_done_callback(self._delayed_close_tasks.discard)
        except RuntimeError:
            # 无运行中的事件循环（如同步测试环境）：直接关闭
            await current.close()

    async def close(self) -> None:
        """Close underlying remote signer resources."""
        tasks = list(self._delayed_close_tasks)
        for task in tasks:
            task.cancel()
        self._delayed_close_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for retired in tuple(self._retired_signers):
            await retired.close()
        self._retired_signers.clear()
        await self._remote_signer.close()

    @property
    def server_url(self) -> str:
        """当前远程签名节点地址（只读，热切换请使用 reconfigure）。"""
        return self._remote_signer.server_url

    @property
    def last_headers(self) -> dict[str, str]:
        """Get dynamic protocol headers returned by the last successful proof minting."""
        return self._remote_signer.last_headers

    async def generate_snapshot_with_headers(
        self,
        contents: list[AistudioContent],
        cookies: dict[str, str],
        auth_user: str = "0",
        tier: str = "Free",
        model: str = "models/gemini-3.7-flash",
        upstream_proxy: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Generate fresh WAA proof token along with dynamic session headers."""
        proof = await self.generate_snapshot(
            contents=contents,
            cookies=cookies,
            auth_user=auth_user,
            tier=tier,
            model=model,
            upstream_proxy=upstream_proxy,
        )
        return proof, dict(self._remote_signer.last_headers)

    async def generate_snapshot(
        self,
        contents: list[AistudioContent],
        cookies: dict[str, str],
        auth_user: str = "0",
        tier: str = "Free",
        model: str = "models/gemini-3.7-flash",
        upstream_proxy: str | None = None,
    ) -> str:
        """Compute content hash and generate fresh WAA proof token."""
        prompt, content_hash = extract_content_digest(contents)
        wire_contents = [c.to_wire() for c in contents]
        logger.debug(
            "计算请求摘要: hash=%s, prompt_len=%d, u/%s",
            content_hash[:8],
            len(prompt),
            auth_user,
        )

        return await self._remote_signer.mint_proof(
            cookies=cookies,
            auth_user=auth_user,
            tier=tier,
            model=model,
            prompt=prompt,
            wire_contents=wire_contents,
            upstream_proxy=upstream_proxy,
        )
