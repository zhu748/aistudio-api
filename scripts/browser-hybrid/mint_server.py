"""本地 mint + send 服务: 浏览器混合捕获实验的后端（复现脚本，凭据参数化）.

POST /mint {model, wireContents}
  -> {"ok": true, "proof": str, "headers": {...}}
     用远程 WAA 签名服务铸 proof（代理/Cookie 与账号一致）

POST /send {url, headers, body}
  -> {"ok": true, "status": int, "headers": {...}, "body": str}
     用项目 httpx transport 代发 GenerateContent（绕过浏览器 TLS/H2 指纹）

GET /health -> {"ok": true}

环境变量:
  HYBRID_AUTH_FILE   账号 auth.json 路径（必需）
  HYBRID_PROXY       socks5 代理 URL（必需，与账号实验代理一致）
  HYBRID_SIGNER_URL  远程签名服务 URL（默认 https://waa.spinsnow.fun）
  HYBRID_TIER        账号层级头（默认 Pro）
  HYBRID_LISTEN      监听地址（默认 127.0.0.1:8765）

原理见 docs/BROWSER_HYBRID_403_2026-11-14.md。
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_REPO_SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"
)
sys.path.insert(0, _REPO_SRC)

from aistudio_api.infrastructure.waa.signer import RemoteProofSigner  # noqa: E402

AUTH_PATH = os.environ.get("HYBRID_AUTH_FILE", "")
PROXY = os.environ.get("HYBRID_PROXY", "")
TIER = os.environ.get("HYBRID_TIER", "Pro")
SIGNER_URL = os.environ.get("HYBRID_SIGNER_URL", "https://waa.spinsnow.fun")
LISTEN = os.environ.get("HYBRID_LISTEN", "127.0.0.1:8765")

if not AUTH_PATH or not PROXY:
    raise SystemExit("missing HYBRID_AUTH_FILE / HYBRID_PROXY env")

with open(AUTH_PATH) as f:
    _auth = json.load(f)
COOKIES = {c["name"]: c["value"] for c in _auth["cookies"]}

# --- 单一后台事件循环 (连接池 / 签名器复用) ---
_loop = asyncio.new_event_loop()


def _loop_runner():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


threading.Thread(target=_loop_runner, daemon=True).start()


def run_async(coro, timeout=90):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


# --- 共享签名器与传输层 (同一循环内安全复用) ---
_signer = RemoteProofSigner(server_url=SIGNER_URL)
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport  # noqa: E402

_transport = HTTPStreamTransport(proxy_url=PROXY)


def digest_prompt_from_wire(wire_contents: list) -> str:
    """按 AI Studio 前端规则从 wire contents 提取 prompt.

    PartIndex: TEXT=1, INLINE_DATA=2, FILE_DATA=5；其余 part 计空串
    （因此改写工具历史不会破坏 proof 摘要）。
    """
    parts_out = []
    for content in wire_contents:
        for part in content[0]:
            if len(part) > 1 and isinstance(part[1], str) and part[1]:
                parts_out.append(part[1])
            elif len(part) > 2 and isinstance(part[2], list) and len(part[2]) > 1:
                parts_out.append(str(part[2][1]))
            elif len(part) > 5 and isinstance(part[5], list) and len(part[5]) > 0:
                parts_out.append(str(part[5][0]))
            else:
                parts_out.append("")
    return " ".join(parts_out)


async def mint_proof(model: str, wire_contents: list) -> tuple[str, dict]:
    prompt = digest_prompt_from_wire(wire_contents)
    proof = await _signer.mint_proof(
        cookies=COOKIES,
        auth_user="0",
        tier=TIER,
        model=model if model.startswith("models/") else f"models/{model}",
        prompt=prompt,
        wire_contents=wire_contents,
        upstream_proxy=PROXY,
    )
    return proof, dict(_signer.last_headers)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            if self.path == "/mint":
                proof, headers = run_async(
                    mint_proof(payload.get("model", "models/gemini-4-hydrogen"),
                               payload.get("wireContents") or []))
                print(f"[mint] ok proof_len={len(proof)}", flush=True)
                self._json(200, {"ok": True, "proof": proof, "headers": headers})
            elif self.path == "/send":
                status, resp_headers, body = run_async(_send(payload), timeout=150)
                self._json(200, {"ok": True, "status": status,
                                 "headers": resp_headers, "body": body})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            print(f"[{self.path}] FAIL {type(exc).__name__}: {exc}", flush=True)
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def _send(payload: dict) -> tuple[int, dict, str]:
    """用项目验证过的头集合发送 (忽略浏览器指纹头 — HeadlessChrome UA/sec-ch-ua 是 403 否决点).

    403 时自动重签 proof 重试 (模拟项目 Auth403RetryManager, 最多 3 次).
    """
    from aistudio_api.infrastructure.account.cookie_parser import (
        build_makersuite_headers,
        get_visit_id,
    )
    from aistudio_api.infrastructure.gateway.transport import (
        _ensure_authorization_header,
    )

    url = payload["url"]
    body = payload["body"]

    headers = build_makersuite_headers()
    headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    )
    headers["X-Goog-AuthUser"] = "0"
    headers["X-AIStudio-Visit-Id"] = get_visit_id("account:research")
    clean = _ensure_authorization_header(
        headers, auth_user="0", tier=TIER, auth_file=AUTH_PATH, cookies=COOKIES
    )

    current_body = body
    attempts = 3 if "/GenerateContent" in url else 1
    for attempt in range(1, attempts + 1):
        status, content = await _transport.send_request(
            url=url,
            headers=clean,
            body=current_body,
            auth_user="0",
            tier=TIER,
            auth_file=AUTH_PATH,
            cookies=COOKIES,
            proxy_url=PROXY,
        )
        print(f"[send] attempt {attempt}/{attempts} status={status}", flush=True)
        if status == 200 or attempt == attempts:
            break
        if status == 403:
            # 重签: 解析 body, 换新 proof
            try:
                arr = json.loads(current_body)
                model = arr[0] if isinstance(arr[0], str) else "models/gemini-4-hydrogen"
                proof, _ = await mint_proof(model, arr[1])
                arr[4] = proof
                current_body = json.dumps(arr, separators=(",", ":"))
                print(f"[send] 403 -> re-minted proof ({len(proof)}B), retrying", flush=True)
            except Exception as exc:
                print(f"[send] re-mint failed: {exc}", flush=True)
                break
    resp_headers = {"content-type": "application/json+protobuf"}
    return status, resp_headers, content.decode("utf-8", "replace")


if __name__ == "__main__":
    host, _, port = LISTEN.partition(":")
    server = ThreadingHTTPServer((host, int(port or 8765)), Handler)
    print(f"[mint-server] listening on {host}:{port or 8765}", flush=True)
    server.serve_forever()
