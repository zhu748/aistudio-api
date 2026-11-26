"""WAA (Web Authentication / Attestation) signing engine.

本地 waa-signer 守护进程（Go + SpiderMonkey）的 proof 铸造客户端与
健康熔断。远程公共签名服务支持已移除：本地引擎是唯一签名方式。
"""

from aistudio_api.infrastructure.waa.signer import (
    LocalProofSigner,
    WaaSigner,
    extract_content_digest,
)

__all__ = [
    "LocalProofSigner",
    "WaaSigner",
    "extract_content_digest",
]
