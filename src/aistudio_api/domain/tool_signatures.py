"""服务端工具调用的 thought_signature 校验与防护。

上游 AI Studio 对「服务端执行的工具」的 functionCall 回传强制校验
thoughtSignature（缺失或非法 base64 直接 400 INVALID_ARGUMENT：
"Function call is missing a thought_signature in functionCall parts"）。

触发场景：开启 Saved Info（记忆）等账号级功能后，上游会给请求动态注入
服务端工具（如 ``default_api:context`` 记忆保存调用）；模型在响应中调用
它们时携带签名。而 OpenAI Chat Completions / Claude Messages 兼容协议
没有官方工具签名字段，客户端（酒馆 SillyTavern 等）保存历史时必然丢失
签名，下一轮回传即触发上游 400，整轮请求被拒。

2026-11-14 补充实测（本地兼容服务 e2e 判别）：

- 上游对**客户端声明函数**（functionDeclarations）的 functionCall
  回传**同样强制**校验签名（gemini-4-hydrogen 与 gemini-3.8-flash 均
  400 "missing thought_signature ... default_api:<函数名>"，错误信息
  表明上游内部把客户端函数也纳入 ``default_api:`` 命名空间）；
- 伪造签名（随机合法 base64）→ 400 "Corrupted thought signature."
  （密码学信封校验，不可伪造）；
- **跨调用交换真实签名实测 200**：校验不绑定签名与具体调用的内容，
  任何模型真实铸造的签名均可通过。

因此防护策略升级为「签名缓存回注」：响应侧凡见到 functionCall part
携带有效 thoughtSignature，就按 call_id（首选）与函数名（兜底）缓存；
请求侧客户端丢失签名时从缓存回注，缓存未命中才剥离（此时模型会重新
发起调用，功能降级但不再 400）。
"""

from __future__ import annotations

import base64
import json
import threading
from collections import OrderedDict
from pathlib import Path

# default_api:* 是 AI Studio 服务端工具命名空间
# （如 default_api:context = Saved Info 记忆保存调用）
_SERVER_SIDE_TOOL_PREFIX = "default_api:"

# 内置工具在 functionCall 中的调用名（上游注入、服务端执行）
_SERVER_SIDE_TOOL_NAMES = frozenset(
    {
        "google_search",
        "url_context",
        "code_execution",
        "google_maps",
        "context",
    }
)


def is_server_side_tool_call(name: str) -> bool:
    """判断函数名是否为上游服务端执行的内置工具调用。"""
    normalized = name.strip().lower()
    return (
        normalized.startswith(_SERVER_SIDE_TOOL_PREFIX)
        or normalized in _SERVER_SIDE_TOOL_NAMES
    )


def valid_thought_signature(signature: object) -> bool:
    """校验 thoughtSignature 为非空合法 base64。

    上游 thought_signature 字段类型为 TYPE_BYTES：非法 base64 透传会被
    拒为 400 "Base64 decoding failed"，与缺失签名同等处理。
    """
    if not isinstance(signature, str) or not signature:
        return False
    try:
        base64.b64decode(signature, validate=True)
    except (ValueError, UnicodeEncodeError):
        return False
    return True


def valid_thought_signature_or_none(signature: object) -> str | None:
    """签名合法则原样返回，否则返回 None（丢弃签名字段本身）。"""
    if valid_thought_signature(signature):
        assert isinstance(signature, str)
        return signature
    return None


class ThoughtSignatureCache:
    """call_id / 函数名 → thoughtSignature 的有界 LRU 缓存（JSON 持久化）。

    - 键策略：``call_id`` 精确匹配（签名即该次调用铸造）优先；函数名
      兜底（最新签名，跨调用交换实测上游接受）；
    - 持久化：``{"by_call_id": {...}, "by_name": {...}}``，每次 remember
      重写（工具调用频率低，文件数百字节量级）；进程重启后缓存仍在；
    - 上限：call_id 2048 条 / 函数名 256 条，超出按 LRU 淘汰；
    - 线程安全：互斥锁保护（uvicorn 线程池端点 + 后台线程共用）。
    """

    _MAX_CALL_IDS = 2048
    _MAX_NAMES = 256

    def __init__(self, path=None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._by_call_id: OrderedDict[str, str] = OrderedDict()
        self._by_name: OrderedDict[str, str] = OrderedDict()
        self._loaded = False
        self._load_error_logged = False

    # -- 公共接口 ----------------------------------------------------------

    def remember(self, call_id: str | None, name: str | None, signature: str) -> None:
        """缓存一条模型下发的真实签名（仅接受合法 base64）。"""
        if not valid_thought_signature(signature):
            return
        with self._lock:
            self._ensure_loaded()
            if call_id:
                self._by_call_id[call_id] = signature
                self._by_call_id.move_to_end(call_id)
                while len(self._by_call_id) > self._MAX_CALL_IDS:
                    self._by_call_id.popitem(last=False)
            if name:
                self._by_name[name] = signature
                self._by_name.move_to_end(name)
                while len(self._by_name) > self._MAX_NAMES:
                    self._by_name.popitem(last=False)
            self._save_locked()

    def lookup(self, call_id: str | None, name: str | None = None) -> str | None:
        """按 call_id（首选）或函数名（兜底）查缓存签名。"""
        with self._lock:
            self._ensure_loaded()
            if call_id:
                sig = self._by_call_id.get(call_id)
                if sig is not None:
                    self._by_call_id.move_to_end(call_id)
                    return sig
            if name:
                sig = self._by_name.get(name)
                if sig is not None:
                    self._by_name.move_to_end(name)
                    return sig
        return None

    # -- 持久化 ------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._resolve_path()
        if path is None:
            return
        try:
            with Path(path).open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key, value in data.get("by_call_id", {}).items():
                    if isinstance(key, str) and valid_thought_signature(value):
                        self._by_call_id[key] = value
                for key, value in data.get("by_name", {}).items():
                    if isinstance(key, str) and valid_thought_signature(value):
                        self._by_name[key] = value
        except (OSError, ValueError):
            # 首次运行 / 文件损坏：从空缓存开始即可
            pass

    def _save_locked(self) -> None:
        path = self._resolve_path()
        if path is None:
            return
        try:
            payload = {
                "by_call_id": dict(self._by_call_id),
                "by_name": dict(self._by_name),
            }
            with path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except (OSError, ValueError) as exc:
            if not self._load_error_logged:
                self._load_error_logged = True
                # 持久化失败不阻断请求路径，仅记录一次
                try:
                    from aistudio_api.infrastructure.utils.logger import get_logger

                    get_logger("tool-signatures").warning(
                        "签名缓存持久化失败（继续使用内存缓存）: %s", exc
                    )
                except Exception:
                    pass

    def _resolve_path(self) -> Path | None:
        if self._path is not None:
            return self._path
        # 延迟解析：domain 层不静态依赖 infrastructure/config（避免环）
        try:
            from aistudio_api.config import resolve_data_dir

            path = resolve_data_dir() / "tool_signature_cache.json"
            self._path = path
            return path
        except Exception:
            return None


_cache_instance: ThoughtSignatureCache | None = None
_cache_instance_lock = threading.Lock()


def get_signature_cache() -> ThoughtSignatureCache:
    """进程级单例（延迟初始化，路径由 config 数据目录解析）。"""
    global _cache_instance
    with _cache_instance_lock:
        if _cache_instance is None:
            _cache_instance = ThoughtSignatureCache()
        return _cache_instance


def remember_function_signature(
    call_id: str | None, name: str | None, signature: object
) -> None:
    """缓存模型下发的真实签名（应用层响应路径统一调用）。"""
    if valid_thought_signature(signature):
        assert isinstance(signature, str)
        get_signature_cache().remember(call_id, name, signature)


def lookup_function_signature(
    call_id: str | None, name: str | None = None
) -> str | None:
    """客户端丢失签名时按 call_id/函数名回注（请求路径统一调用）。"""
    if not call_id and not name:
        return None
    return get_signature_cache().lookup(call_id, name)
