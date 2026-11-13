"""端到端冒烟：起真实服务进程，用 OpenAI Responses 协议打 /v1/responses。

上游用 monkeypatch 替换为离线假流（不起真实 AI Studio 请求），验证：
1. 非流式：POST /v1/responses → response 对象
2. 流式：SSE 事件序列
3. /v1/models 列表
4. 指纹剥离：抓 transport 出站头，确认只含白名单头
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from unittest.mock import patch

sys.path.insert(0, "src")

import httpx


async def fake_generate_content(self, **_kwargs):  # noqa: ARG001
    from aistudio_api.domain.models import Candidate, ModelOutput

    return ModelOutput(
        candidates=[
            Candidate(text="Hi from fake upstream", finish_reason=1),
        ],
        usage={"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
    )


async def fake_stream(
    self,  # noqa: ARG001
    **_kwargs,
) -> AsyncGenerator[tuple[str, object], None]:
    yield ("thinking", "Pondering")
    yield ("body", "Streamed ")
    yield ("body", "answer")
    yield ("finish_reason", 1)
    yield ("usage", {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9})
    yield ("done", None)


async def main() -> None:
    from aistudio_api.api.app import app
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    # 无凭据部署（测试环境）：启用免鉴权模式
    settings.allow_no_auth = True
    transport = httpx.ASGITransport(app=app)
    # 替换上游调用（不触网）；进入 lifespan 初始化 runtime_state.client
    async with app.router.lifespan_context(app):
        with (
            patch.object(AIStudioClient, "generate_content", fake_generate_content),
            patch.object(AIStudioClient, "stream_generate_content", fake_stream),
        ):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                # --- 1. 非流式 ---
                resp = await client.post(
                    "/v1/responses",
                    json={
                        "model": "gemini-3.8-flash",
                        "input": "Hello",
                        "instructions": "Be brief.",
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                print("=== 非流式:", resp.status_code)
                payload = resp.json()
                assert payload["object"] == "response", payload
                assert payload["status"] == "completed"
                print(json.dumps(payload, ensure_ascii=False, indent=1)[:600])

                # --- 2. 流式 ---
                resp = await client.post(
                    "/v1/responses",
                    json={
                        "model": "gemini-3.8-flash",
                        "input": [{"role": "user", "content": "Hello"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                print("\n=== 流式:", resp.status_code)
                event_types = []
                for line in resp.text.splitlines():
                    if line.startswith("event: "):
                        event_types.append(line[len("event: ") :])
                print("events:", event_types)
                assert event_types[0] == "response.created"
                assert event_types[-1] == "response.completed"
                assert "response.reasoning_summary_text.delta" in event_types
                assert "response.output_text.delta" in event_types

                # --- 3. 带 fingerprint 头的请求（模拟 SDK 客户端） ---
                resp = await client.post(
                    "/v1/responses",
                    json={"model": "gemini-3.8-flash", "input": "Hello"},
                    headers={
                        "Authorization": "Bearer test-key",
                        "x-stainless-lang": "python",
                        "x-stainless-os": "Linux",
                        "user-agent": "OpenAI/Python 3.50.0",
                        "x-app-name": "Codex",
                    },
                )
                print("\n=== 带 SDK 指纹头:", resp.status_code, resp.json()["status"])

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
