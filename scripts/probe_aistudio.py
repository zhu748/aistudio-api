# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28,<1"]
# ///
"""本机 aistudio-api 实测；uv run probe_aistudio.py --model <模型> [--full]。

Cookie、Google 出口代理及签名器由运行中的网关配置。本脚本只访问指定网关，
不导入账号、不上传凭据、不执行模型建议的代码。报告不保存原始响应或请求头。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

PROTOCOLS = ("gemini", "chat", "responses", "claude")
PROMPTS = {
    "text": "Reply with exactly PROBE_OK and nothing else.",
    "stream": "Reply with exactly PROBE_OK and nothing else.",
    "thinking": "Compute 17 * 23 carefully. Reply with exactly 391.",
    "json": 'Return only this JSON object, without Markdown: {"ok":true,"value":7}',
    "tool": "You must call probe_sum with a=2 and b=5. Do not calculate it yourself.",
}
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "value": {"type": "integer"}},
    "required": ["ok", "value"],
}
FUNCTION = {
    "name": "probe_sum",
    "description": "Add two integers.",
    "parameters": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
}
USAGE_KEYS = frozenset(
    [
        "promptTokenCount",
        "candidatesTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
        "cachedContentTokenCount",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "input_tokens_details",
        "output_tokens_details",
        "completion_tokens_details",
        "prompt_tokens_details",
    ]
)


@dataclass
class Options:
    model: str
    base_url: str = "http://127.0.0.1:8080"
    protocols: tuple[str, ...] = PROTOCOLS
    full: bool = False
    key_env: str = "AISTUDIO_API_KEY"
    max_tokens: int = 2048
    timeout: float = 180


def obj(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def string(value: object) -> str:
    return value if isinstance(value, str) else ""


def safe_usage(value: object) -> dict[str, object]:
    return {
        key: safe_usage(item) if isinstance(item, dict) else item
        for key, item in obj(value).items()
        if key in USAGE_KEYS and (type(item) is int or isinstance(item, dict))
    }


def request_for(
    protocol: str, case: str, model: str, tokens: int
) -> tuple[str, dict[str, object]]:
    prompt = PROMPTS[case]
    stream = case == "stream"
    if protocol == "gemini":
        path = f"/v1beta/models/{quote(model, safe='')}:" + (
            "streamGenerateContent?alt=sse" if stream else "generateContent"
        )
        config: dict[str, object] = {"maxOutputTokens": tokens}
        body: dict[str, object] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [],
            "generationConfig": config,
        }
        if case == "thinking":
            config["thinkingConfig"] = {
                "thinkingLevel": "LOW",
                "includeThoughts": True,
            }
        if case == "json":
            config.update(responseMimeType="application/json", responseSchema=SCHEMA)
        if case == "tool":
            body["tools"] = [{"functionDeclarations": [FUNCTION]}]
            body["toolConfig"] = {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": ["probe_sum"],
                }
            }
        return path, body
    path = {
        "chat": "/v1/chat/completions",
        "responses": "/v1/responses",
        "claude": "/v1/messages",
    }[protocol]
    body = {"model": model, "stream": stream, "tools": []}
    if protocol == "responses":
        body.update(input=prompt, max_output_tokens=tokens)
    else:
        body.update(messages=[{"role": "user", "content": prompt}], max_tokens=tokens)
    if stream and protocol == "chat":
        body["stream_options"] = {"include_usage": True}
    if case == "thinking":
        if protocol == "chat":
            body["reasoning_effort"] = "low"
        elif protocol == "responses":
            body["reasoning"] = {"effort": "low"}
        else:
            body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    if case == "json" and protocol == "chat":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "probe", "schema": SCHEMA},
        }
    if case == "json" and protocol == "responses":
        body["text"] = {
            "format": {"type": "json_schema", "name": "probe", "schema": SCHEMA}
        }
    if case == "tool":
        if protocol == "chat":
            body.update(
                tools=[{"type": "function", "function": FUNCTION}],
                tool_choice="required",
            )
        elif protocol == "responses":
            body.update(
                tools=[{"type": "function", **FUNCTION}], tool_choice="required"
            )
        else:
            body.update(
                tools=[
                    {
                        "name": FUNCTION["name"],
                        "description": FUNCTION["description"],
                        "input_schema": FUNCTION["parameters"],
                    }
                ],
                tool_choice={"type": "tool", "name": "probe_sum"},
            )
    return path, body


def decode(
    protocol: str, payload: dict[str, object]
) -> tuple[str, list[dict[str, object]], str, dict[str, object], int, int]:
    """Return content in memory; only derived statistics are written to disk."""
    blocks: list[object] = []
    calls: list[dict[str, object]] = []
    text, thinking = "", 0
    if protocol == "gemini":
        candidate = obj(next(iter(items(payload.get("candidates"))), {}))
        blocks = items(obj(candidate.get("content")).get("parts"))
        finish, usage = candidate.get("finishReason"), payload.get("usageMetadata")
    elif protocol == "chat":
        choice = obj(next(iter(items(payload.get("choices"))), {}))
        message = obj(choice.get("message"))
        text = string(message.get("content"))
        thinking = len(str(message.get("reasoning_content") or ""))
        calls = [obj(call) for call in items(message.get("tool_calls"))]
        finish, usage = choice.get("finish_reason"), payload.get("usage")
    elif protocol == "responses":
        for raw_item in items(payload.get("output")):
            item = obj(raw_item)
            if item.get("type") == "function_call":
                calls.append(item)
            blocks.extend(items(item.get("content")))
            for summary in items(item.get("summary")):
                thinking += len(str(obj(summary).get("text") or ""))
        finish, usage = payload.get("status"), payload.get("usage")
    else:
        blocks = items(payload.get("content"))
        finish, usage = payload.get("stop_reason"), payload.get("usage")
    signatures = 0
    for raw in blocks:
        block = obj(raw)
        signatures += bool(block.get("thoughtSignature") or block.get("signature"))
        if block.get("thought") or block.get("type") == "thinking":
            thinking += len(str(block.get("text") or block.get("thinking") or ""))
        else:
            text += str(block.get("text") or "")
        if "functionCall" in block or block.get("type") == "tool_use":
            calls.append(block)
    return str(text), calls, str(finish or ""), safe_usage(usage), thinking, signatures


async def exchange(
    client: httpx.AsyncClient, path: str, body: dict[str, object] | None, timeout: float
) -> tuple[dict[str, object], dict[str, object]]:
    """Bounded read, overall deadline, no redirects/retries or raw error logging."""
    started = time.perf_counter()
    result: dict[str, object] = {"http_status": 0, "transport_ok": False}
    payload: dict[str, object] = {}
    try:
        async with (
            asyncio.timeout(timeout),
            client.stream(
                "POST" if body is not None else "GET", path, json=body
            ) as response,
        ):
            result["http_status"] = response.status_code
            result["content_type_ok"] = "json" in response.headers.get(
                "content-type", ""
            ) or "text/event-stream" in response.headers.get("content-type", "")
            if response.status_code != 200:
                result["error"] = "http_error"
                return result, payload
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if chunk and "first_data_ms" not in result:
                    result["first_data_ms"] = round(
                        (time.perf_counter() - started) * 1000, 1
                    )
                raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("response_limit")
            result["bytes"] = len(raw)
            if "text/event-stream" in response.headers.get("content-type", ""):
                events, done = [], False
                for block in raw.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(
                        line[5:].lstrip(" ")
                        for line in block.splitlines()
                        if line.startswith("data:")
                    )
                    if data == "[DONE]":
                        done = True
                    elif data:
                        event = json.loads(data)
                        if not isinstance(event, dict):
                            raise ValueError("event_type")
                        events.append(event)
                payload = {"_events": events, "_done": done}
            else:
                parsed: object = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("payload_type")
                payload = parsed
            result["transport_ok"] = True
    except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError) as exc:
        result["error"] = (
            "timeout" if isinstance(exc, TimeoutError) else "transport_or_decode_error"
        )
    finally:
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result, payload


def summarize(
    protocol: str, case: str, payload: dict[str, object]
) -> dict[str, object]:
    events = (
        [obj(event) for event in items(payload.get("_events"))]
        if "_events" in payload
        else [payload]
    )
    texts: list[str] = []
    calls: list[dict[str, object]] = []
    usage: dict[str, object] = {}
    finishes: list[object] = []
    thinking, signatures = 0, 0
    error = False
    error_class = ""
    for event in events:
        error |= "error" in event or event.get("type") in (
            "error",
            "response.failed",
            "response.incomplete",
        )
        kind = event.get("type")
        error_info = obj(event.get("error")) or obj(
            obj(event.get("response")).get("error")
        )
        code = error_info.get("code")
        error_type = error_info.get("type")
        if code == 429 or error_type == "rate_limit_error":
            error_class = "rate_limited"
        elif code in (401, 403) or error_type in (
            "authentication_error",
            "permission_error",
        ):
            error_class = "authentication_or_permission"
        if case == "stream" and protocol == "chat":
            for raw_choice in items(event.get("choices")):
                choice = obj(raw_choice)
                texts.append(str(obj(choice.get("delta")).get("content") or ""))
                if choice.get("finish_reason"):
                    finishes.append(choice["finish_reason"])
            usage.update(safe_usage(event.get("usage")))
        elif case == "stream" and protocol == "claude":
            delta = obj(event.get("delta"))
            texts.append(str(delta.get("text") or ""))
            usage.update(safe_usage(event.get("usage")))
            if delta.get("stop_reason"):
                finishes.append(delta["stop_reason"])
        elif case == "stream" and protocol == "responses":
            if kind == "response.completed":
                text, found, finish, used, thought, sig = decode(
                    protocol, obj(event.get("response"))
                )
                texts.append(text)
                calls.extend(found)
                finishes.append(finish)
                usage.update(used)
                thinking += thought
                signatures += sig
        else:
            text, found, finish, used, thought, sig = decode(protocol, event)
            texts.append(text)
            calls.extend(found)
            if finish:
                finishes.append(finish)
            usage.update(used)
            thinking += thought
            signatures += sig
    terminal = bool(
        finishes
        and finishes[-1]
        in ("STOP", "stop", "tool_calls", "completed", "end_turn", "tool_use")
    )
    if case == "stream":
        if protocol == "chat":
            terminal &= payload.get("_done") is True
        elif protocol == "claude":
            terminal &= bool(
                events
                and events[0].get("type") == "message_start"
                and events[-1].get("type") == "message_stop"
            )
        elif protocol == "responses":
            terminal &= bool(
                events
                and events[0].get("type") == "response.created"
                and events[-1].get("type") == "response.completed"
            )
    text = "".join(texts).strip()
    checks = {"no_error": not error, "complete": terminal}
    if case == "stream":
        checks["sse_response"] = "_events" in payload
    if case in ("text", "stream"):
        checks["expected_text"] = text == "PROBE_OK"
    elif case == "thinking":
        checks["expected_answer"] = text == "391"
    elif case == "json":
        try:
            parsed_json: object = json.loads(text)
            parsed_object = obj(parsed_json)
            checks["expected_json"] = (
                parsed_object.get("ok") is True
                and type(parsed_object.get("value")) is int
                and parsed_object.get("value") == 7
                and set(parsed_object) == {"ok", "value"}
            )
        except ValueError:
            checks["expected_json"] = False
    elif case == "tool":
        checks["expected_tool_arguments"] = len(calls) == 1 and tool_value(
            protocol, calls[0]
        ) == ("probe_sum", {"a": 2, "b": 5})
    else:
        checks["expected_answer"] = text == "7"
    # All response-derived strings, IDs, arguments and signatures stay in memory.
    return {
        "checks": checks,
        "error_class": error_class,
        "finish": finishes[-1]
        if finishes
        and finishes[-1]
        in (
            "STOP",
            "MAX_TOKENS",
            "SAFETY",
            "RECITATION",
            "OTHER",
            "stop",
            "length",
            "content_filter",
            "tool_calls",
            "completed",
            "incomplete",
            "failed",
            "end_turn",
            "tool_use",
            "max_tokens",
            "refusal",
            "stop_sequence",
        )
        else "unknown",
        "passed": all(checks.values()),
        "text_chars": len(text),
        "thinking_chars": thinking,
        "signature_parts": signatures,
        "tool_calls": len(calls),
        "events": len(events),
        "usage": usage,
        "event_types": {
            name: count
            for name, count in Counter(str(e.get("type", "")) for e in events).items()
            if name
            in (
                "response.created",
                "response.in_progress",
                "response.completed",
                "response.failed",
                "response.incomplete",
                "message_start",
                "message_delta",
                "message_stop",
                "error",
            )
        },
    }


def should_stop(status: dict[str, object], result: dict[str, object]) -> bool:
    return status.get("http_status") in (0, 401, 403, 429, 502, 503, 504) or result.get(
        "error_class"
    ) in ("rate_limited", "authentication_or_permission")


def tool_value(protocol: str, call: dict[str, object]) -> tuple[object, object]:
    call = obj(call.get("functionCall")) if protocol == "gemini" else call
    function = obj(call.get("function")) if protocol == "chat" else call
    args = (
        function.get("args")
        if protocol == "gemini"
        else function.get("input")
        if protocol == "claude"
        else function.get("arguments")
    )
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    return function.get("name"), args


def followup(
    protocol: str, body: dict[str, object], payload: dict[str, object]
) -> dict[str, object]:
    """Return the known sum 7; preserve opaque history metadata only in memory."""
    body = copy.deepcopy(body)
    instruction = "Return only the tool result as the digit 7. Do not call any tool."
    if protocol == "gemini":
        content = obj(obj(items(payload.get("candidates"))[0]).get("content"))
        call = next(
            obj(obj(part).get("functionCall"))
            for part in items(content.get("parts"))
            if "functionCall" in obj(part)
        )
        response: dict[str, object] = {"name": "probe_sum", "response": {"result": 7}}
        if call.get("id"):
            response["id"] = call["id"]
        body["contents"] = [
            *items(body.get("contents")),
            content,
            {
                "role": "user",
                "parts": [{"functionResponse": response}, {"text": instruction}],
            },
        ]
        body.pop("toolConfig", None)
    elif protocol == "chat":
        message = obj(obj(items(payload.get("choices"))[0]).get("message"))
        body["messages"] = [
            *items(body.get("messages")),
            message,
            {
                "role": "tool",
                "tool_call_id": obj(items(message.get("tool_calls"))[0])["id"],
                "content": "7",
            },
            {"role": "user", "content": instruction},
        ]
    elif protocol == "responses":
        outputs = [obj(item) for item in items(payload.get("output"))]
        call = next(item for item in outputs if item.get("type") == "function_call")
        body["input"] = [
            {"role": "user", "content": body["input"]},
            *outputs,
            {"type": "function_call_output", "call_id": call["call_id"], "output": "7"},
            {"role": "user", "content": instruction},
        ]
    else:
        blocks = [obj(block) for block in items(payload.get("content"))]
        call = next(block for block in blocks if block.get("type") == "tool_use")
        body["messages"] = [
            *items(body.get("messages")),
            {"role": "assistant", "content": blocks},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call["id"], "content": "7"},
                    {"type": "text", "text": instruction},
                ],
            },
        ]
    body["tools"] = []
    body.pop("tool_choice", None)
    return body


async def probe(args: Options) -> dict[str, object]:
    results: list[dict[str, object]] = []
    report: dict[str, object] = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "requested_model": args.model,
        "protocols": list(args.protocols),
        "max_output_tokens": args.max_tokens,
        "full": args.full,
        "results": results,
        "notes": [
            "Gateway protocol tests; not raw Google RPC captures.",
            "first_data_ms measures first HTTP body bytes, including protocol metadata; not model TTFT.",
            "Claude JSON test uses prompting, not a constrained schema.",
            "A thinking request can succeed without exposing reasoning text.",
            "No script retries; gateway may perform its own retries.",
        ],
    }
    key = os.environ.get(args.key_env, "aistudio-probe")
    headers = {
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "x-goog-api-key": key,
        "anthropic-version": "2023-06-01",
        "X-Anti-Truncation": "false",
    }
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        trust_env=False,
        follow_redirects=False,
        timeout=args.timeout,
    ) as client:
        status, payload = await exchange(client, "/v1beta/models", None, args.timeout)
        results.append(
            {
                "case": "models",
                **status,
                "passed": status["transport_ok"]
                and isinstance(payload.get("models"), list),
                "model_count": len(items(payload.get("models"))),
            }
        )
        blocked = not results[-1]["passed"]
        for protocol in args.protocols:
            for case in (
                ["text", "stream", "thinking", "json", "tool"]
                if args.full
                else ["text", "stream"]
            ):
                name = f"{protocol}.{case}"
                if blocked:
                    results.append({"case": name, "skipped": "gateway_unavailable"})
                    continue
                path, body = request_for(protocol, case, args.model, args.max_tokens)
                status, payload = await exchange(client, path, body, args.timeout)
                result = {"case": name, **status, **summarize(protocol, case, payload)}
                result["passed"] = bool(
                    result["passed"]
                    and status["transport_ok"]
                    and status.get("content_type_ok")
                )
                results.append(result)
                print(
                    f"{name}: {'PASS' if result['passed'] else 'FAIL'} HTTP={status['http_status']} time={status['elapsed_ms']}ms",
                    flush=True,
                )
                blocked = should_stop(status, result)
                if case == "tool" and result["passed"]:
                    try:
                        next_body = followup(protocol, body, payload)
                    except (KeyError, IndexError, StopIteration, TypeError):
                        results.append(
                            {
                                "case": f"{protocol}.tool_result",
                                "passed": False,
                                "error": "missing_tool_history_fields",
                            }
                        )
                        continue
                    status, payload = await exchange(
                        client, path, next_body, args.timeout
                    )
                    result = {
                        "case": f"{protocol}.tool_result",
                        **status,
                        **summarize(protocol, "tool_result", payload),
                    }
                    result["passed"] = bool(
                        result["passed"]
                        and status["transport_ok"]
                        and status.get("content_type_ok")
                    )
                    results.append(result)
                    blocked = should_stop(status, result)
    report["summary"] = {
        "passed": sum(r.get("passed") is True for r in results),
        "failed": sum(r.get("passed") is False for r in results),
        "skipped": sum("skipped" in r for r in results),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8080",
        help="网关根地址，不带 /v1；不是 SOCKS5 地址",
    )
    parser.add_argument("--model", required=True, help="本机账号可用的 Gemini 模型 ID")
    parser.add_argument(
        "--protocols", nargs="+", choices=PROTOCOLS, default=list(PROTOCOLS)
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="最多 24 次生成请求；默认最多 8 次；网关内部可能重试",
    )
    parser.add_argument(
        "--key-env", default="AISTUDIO_API_KEY", help="读取网关 API key 的环境变量名"
    )
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--out", type=Path, default=Path("probe-report.json"))
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error("base-url 必须是无凭据、查询参数或片段的 HTTP(S) 网关地址")
    args.model = args.model.removeprefix("models/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.model):
        parser.error("model 应为 Gemini 模型 ID")
    if args.max_tokens < 2048 or args.timeout <= 0:
        parser.error("max-tokens 至少 2048，timeout 必须大于 0")
    report = asyncio.run(
        probe(
            Options(
                model=args.model,
                base_url=args.base_url,
                protocols=tuple(dict.fromkeys(args.protocols)),
                full=args.full,
                key_env=args.key_env,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
        )
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".probe-", dir=args.out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        Path(temporary).replace(args.out)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps(report["summary"]))
    print("脱敏报告已保存；不包含请求头、原始响应或凭据。")
    return int(bool(obj(report["summary"])["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
