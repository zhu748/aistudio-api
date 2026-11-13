"""账号存储层，管理多 Google 账号的注册表与 Cookie 凭据。"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from os.path import basename, join, realpath
from pathlib import Path
from typing import ClassVar

from aistudio_api.infrastructure.utils.common import atomic_write_json


def _atomic_write_json(path: Path, data: object, *, private: bool = True) -> None:
    # 账号目录下的 auth.json/meta.json/registry.json 全部含凭据或会话
    # 指纹，默认收紧为 0600（3.13）。
    atomic_write_json(path, data, compact=True, private=private)


# 默认搜索路径（与 config.py 保持一致）
_SEARCH_ROOTS: list[Path] = [
    Path.cwd(),
    Path(__file__)
    .resolve()
    .parents[4],  # src/aistudio_api/infrastructure/account -> 项目根
]


def _safe_account_dir(accounts_dir: Path, account_id: str) -> Path:
    """验证并返回安全的账号子目录路径，防止路径遍历攻击。"""
    raw = str(account_id or "").strip()
    clean_id = "".join(c for c in raw if c.isalnum() or c in ("-", "_", ".", "@"))
    safe_name = basename(clean_id)
    if (
        not safe_name
        or safe_name != account_id
        or safe_name in (".", "..")
        or ".." in safe_name
    ):
        raise ValueError(f"Invalid or unsafe account_id: {account_id!r}")
    base_str = realpath(str(accounts_dir))
    target_str = realpath(join(base_str, safe_name))
    prefix = base_str if base_str.endswith(os.sep) else base_str + os.sep
    if not target_str.startswith(prefix):
        raise ValueError(f"Account path escapes base directory: {account_id!r}")
    return Path(target_str)


def _resolve_accounts_dir() -> Path:
    """发现 accounts 目录，默认为 data/accounts。"""
    env = os.getenv("AISTUDIO_ACCOUNTS_DIR")
    if env:
        return Path(env).resolve()
    for root in _SEARCH_ROOTS:
        candidate = root / "data" / "accounts"
        if candidate.is_dir():
            return candidate
    # 默认在第一个搜索根下创建
    return (_SEARCH_ROOTS[0] / "data" / "accounts").resolve()


def _generate_account_id() -> str:
    """生成 acc_ 前缀的随机 ID。"""
    import secrets

    return f"acc_{secrets.token_hex(4)}"


# auth.json Cookie 加载的 mtime 缓存（P9）：save_account 每次保存都会触发
# 兄弟账号锚点同步，批量导入 M 个账号时要重读全部 N 个 auth.json（O(M×N)
# 次文件读）。按 (路径, mtime) 缓存加载结果后，只有真正变化的文件需要重读。
_auth_cookie_cache: dict[Path, tuple[float, dict[str, str], str]] = {}
_auth_cookie_cache_lock = threading.Lock()


def _load_auth_cookies_cached(
    auth_path: Path,
) -> tuple[dict[str, str], str] | None:
    """mtime 缓存的 auth.json 读取，返回 (cookies, session_anchor) 或 None。"""
    from aistudio_api.infrastructure.account.cookie_parser import (
        compute_session_anchor,
        load_cookies_from_auth_file,
    )

    try:
        mtime = auth_path.stat().st_mtime
    except OSError:
        return None
    with _auth_cookie_cache_lock:
        cached = _auth_cookie_cache.get(auth_path)
        if cached is not None and cached[0] == mtime:
            return cached[1], cached[2]
    cdict = load_cookies_from_auth_file(auth_path)
    if not cdict:
        return None
    anchor = compute_session_anchor(cdict)
    with _auth_cookie_cache_lock:
        # 简单防膨胀：账号数有限（数百级），超过阈值时整体重建
        if len(_auth_cookie_cache) > 4096:
            _auth_cookie_cache.clear()
        _auth_cookie_cache[auth_path] = (mtime, cdict, anchor)
    return cdict, anchor


@dataclass
class AccountMeta:
    """账号元数据。"""

    id: str
    name: str
    email: str | None
    created_at: str
    last_used: str | None = None
    auth_user: str = "0"
    cookie_id: str | None = None
    tier: str = "Free"
    tier_checked_at: float | None = None
    last_rotated_at: float | None = None
    enabled: bool = True
    group: str = "default"
    notes: str = ""
    proxy_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AccountMeta:
        raw_enabled = data.get("enabled")
        enabled = True if raw_enabled is None else bool(raw_enabled)
        raw_group = str(data.get("group") or "default").strip()
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            email=str(data["email"]) if data.get("email") is not None else None,
            created_at=str(data.get("created_at") or ""),
            last_used=str(data["last_used"])
            if data.get("last_used") is not None
            else None,
            auth_user=str(data.get("auth_user") or "0"),
            cookie_id=str(data["cookie_id"])
            if data.get("cookie_id") is not None
            else None,
            tier=str(data.get("tier") or "Free"),
            tier_checked_at=float(str(data["tier_checked_at"]))
            if data.get("tier_checked_at") is not None
            else None,
            last_rotated_at=float(str(data["last_rotated_at"]))
            if data.get("last_rotated_at") is not None
            else None,
            enabled=enabled,
            group=raw_group or "default",
            notes=str(data.get("notes") or ""),
            proxy_url=str(data["proxy_url"])
            if data.get("proxy_url") is not None
            else None,
        )


@dataclass
class AccountPreset:
    """账号调度预设方案。"""

    id: str
    name: str
    description: str = ""
    action: str = "snapshot"
    account_ids: list[str] = field(default_factory=list)
    target_tiers: list[str] = field(default_factory=list)
    target_groups: list[str] = field(default_factory=list)
    is_system: bool = False
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AccountPreset:
        raw_ids = data.get("account_ids")
        ids_list = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
        raw_tiers = data.get("target_tiers")
        tiers_list = [str(x) for x in raw_tiers] if isinstance(raw_tiers, list) else []
        raw_groups = data.get("target_groups")
        groups_list = (
            [str(x) for x in raw_groups] if isinstance(raw_groups, list) else []
        )
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or "未命名预设"),
            description=str(data.get("description") or ""),
            action=str(data.get("action") or "snapshot"),
            account_ids=ids_list,
            target_tiers=tiers_list,
            target_groups=groups_list,
            is_system=bool(data.get("is_system")),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def _load_system_presets_from_config() -> list[AccountPreset]:
    """从 config.yaml 读取系统预设方案。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_presets,
    )

    raw_list = get_configured_presets()
    if not raw_list:
        return []
    now = datetime.now(UTC).isoformat()
    presets = []
    for item in raw_list:
        p = AccountPreset.from_dict(item)
        p.is_system = True
        p.created_at = p.created_at or now
        p.updated_at = now
        presets.append(p)
    return presets


@dataclass
class Registry:
    """账号注册表。"""

    accounts: dict[str, AccountMeta]
    active_account_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "accounts": {k: v.to_dict() for k, v in self.accounts.items()},
            "active_account_id": self.active_account_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Registry:
        raw_accounts = data.get("accounts")
        accounts_dict: dict[str, object] = (
            raw_accounts if isinstance(raw_accounts, dict) else {}
        )
        accounts = {
            k: AccountMeta.from_dict(v)
            for k, v in accounts_dict.items()
            if isinstance(v, dict)
        }
        raw_active_id = data.get("active_account_id")
        return cls(
            accounts=accounts,
            active_account_id=str(raw_active_id) if raw_active_id is not None else None,
        )


class AccountStore:
    """账号存储管理器（线程安全单例，支持原子文件替换）。"""

    _instances: ClassVar[dict[Path, AccountStore]] = {}
    _singleton_lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls, accounts_dir: Path | None = None) -> AccountStore:
        # absolute() 而非 resolve()：resolve 在 Windows 上逐段做符号链接与
        # 大小写归一化（约 85µs/次），而 AccountStore() 在请求热路径上被
        # 反复构造（单例命中也要付这个开销）。
        resolved_dir = (accounts_dir or _resolve_accounts_dir()).absolute()
        with cls._singleton_lock:
            if resolved_dir not in cls._instances:
                instance = super().__new__(cls)
                cls._instances[resolved_dir] = instance
            return cls._instances[resolved_dir]

    def __init__(self, accounts_dir: Path | None = None) -> None:
        if getattr(self, "_initialized", False):
            return
        self._accounts_dir = (accounts_dir or _resolve_accounts_dir()).absolute()
        self._registry_path = self._accounts_dir / "registry.json"
        self._presets_path = self._accounts_dir / "presets.json"
        self._registry: Registry | None = None
        self._presets: dict[str, AccountPreset] | None = None
        # RLock：update_account_cookies 等复合读改写序列需要全程持锁，
        # 而序列内部还要调用同样加锁的 _load_registry/_save_registry，
        # 同线程重入必须被允许（3.4）。
        self._lock = threading.RLock()
        # 一旦检测到损坏的 registry.json 即置位：隔离后文件不再存在，后续
        # _load_registry 若只靠 exists() 判断就会静默返回空注册表，进而把
        # {"accounts": {}} 写回磁盘。该标志让「拒绝静默清空」在所有后续调用
        # 中持续生效，直到人工恢复文件并重启。
        self._corrupt_registry: bool = False
        self._ensure_dirs()
        self._ensure_default_presets()
        self._initialized = True
        # 清扫上次 SIGKILL/掉电可能残留的原子写临时文件（含完整凭据，3.13）
        with contextlib.suppress(Exception):
            from aistudio_api.infrastructure.utils.common import cleanup_stale_tmp_files

            removed = cleanup_stale_tmp_files(
                self._accounts_dir, pattern="auth.json.tmp.*"
            ) + cleanup_stale_tmp_files(
                self._accounts_dir, pattern="registry.json.tmp.*"
            )
            for acc_dir in self._accounts_dir.iterdir():
                if acc_dir.is_dir():
                    removed += cleanup_stale_tmp_files(acc_dir, pattern="*.tmp.*")
            if removed:
                logging.getLogger("account_store").info(
                    "已清扫 %d 个残留临时凭据文件", removed
                )

    def _ensure_dirs(self) -> None:
        """确保目录存在。"""
        self._accounts_dir.mkdir(parents=True, exist_ok=True)

    def _load_registry(self) -> Registry:
        """加载注册表（线程安全）。"""
        with self._lock:
            if self._registry is not None:
                return self._registry
            if self._corrupt_registry:
                # 坏文件已在先前调用中被隔离，此时文件不存在 ≠ 首次启动。
                # 必须继续失败，否则空注册表会被 _save_registry 写回。
                raise RuntimeError(
                    "账号注册表 registry.json 此前已判定损坏并被隔离；"
                    "请人工恢复备份后重启（拒绝静默清空以免丢失全部账号）"
                )
            if not self._registry_path.exists():
                if self._has_corrupt_marker():
                    # registry.json 被隔离后文件不存在 ≠ 首次启动。目录里残留的
                    # 隔离备份是持久标记，重启后也必须继续拒绝，否则一次
                    # _save_registry 就会把空注册表写回，永久丢掉全部账号。
                    self._corrupt_registry = True
                    raise RuntimeError(
                        "账号注册表 registry.json 已被隔离但尚未人工恢复；"
                        "请恢复备份后重启（拒绝静默清空以免丢失全部账号）"
                    )
                self._registry = Registry(accounts={})
                return self._registry
            try:
                raw_text = self._registry_path.read_text(encoding="utf-8")
                data = json.loads(raw_text)
                registry = Registry.from_dict(data)
            except Exception as e:
                # 损坏/不可解析的注册表绝不静默重置为空：那样下一次
                # _save_registry 会把 {"accounts": {}} 写回，永久丢失全部账号。
                # 隔离坏文件 + 抛错，让启动/写入失败而非静默抹除数据。
                self._corrupt_registry = True
                self._quarantine_corrupt_registry(e)
                raise RuntimeError(
                    "账号注册表 registry.json 损坏，已隔离备份；"
                    "请人工恢复后重启（拒绝静默清空以免丢失全部账号）"
                ) from e

            self._registry = registry
            # 以下为一致的尽力补齐（补齐邮箱/档位/兄弟账号 Cookie）。
            # 失败不应被当成「注册表损坏」——否则会隔离一个好文件并阻断启动。
            try:
                reconciled = False
                for acc_id, acc in self._registry.accounts.items():
                    account_dir = self._accounts_dir / acc_id
                    meta_path = account_dir / "meta.json"
                    if meta_path.is_file():
                        try:
                            mdata = json.loads(meta_path.read_text(encoding="utf-8"))
                            if isinstance(mdata, dict):
                                if not acc.email and mdata.get("email"):
                                    acc.email = str(mdata["email"])
                                    reconciled = True
                                if (
                                    (not acc.tier or acc.tier == "Free")
                                    and mdata.get("tier")
                                    and mdata.get("tier") != "Free"
                                ):
                                    acc.tier = str(mdata["tier"])
                                    reconciled = True
                        except Exception:
                            pass
                if self._sync_sibling_cookies_locked(self._registry):
                    reconciled = True
                if reconciled:
                    _atomic_write_json(self._registry_path, self._registry.to_dict())
            except Exception as e:
                # 补齐失败只记录，不影响本次加载结果（内存态已可用）
                logging.getLogger("account_store").warning(
                    "注册表一致化补齐失败(%s)", type(e).__name__
                )
            return self._registry

    def _has_corrupt_marker(self) -> bool:
        """目录里是否残留此前隔离的损坏注册表备份。

        registry.json 被隔离改名后原文件已不存在，单靠 ``exists()`` 会把它
        误判成首次启动；隔离备份本身就是可以跨重启存活的持久标记。
        """
        try:
            return any(self._accounts_dir.glob("registry.corrupt.*.json"))
        except OSError:
            return False

    def _quarantine_corrupt_registry(self, error: Exception) -> None:
        """把损坏的 registry.json 改名隔离，避免下次 _save_registry 覆盖它。"""
        # 用微秒 + 递增后缀：秒级时间戳在同一秒内二次隔离会撞名，
        # shutil.move 会失败并被忽略，坏文件留在原地继续挡住启动。
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        for suffix in range(16):
            name = (
                f".corrupt.{stamp}.json"
                if suffix == 0
                else f".corrupt.{stamp}.{suffix}.json"
            )
            backup = self._registry_path.with_suffix(name)
            if backup.exists():
                continue
            try:
                shutil.move(str(self._registry_path), str(backup))
            except Exception:
                # 隔离失败也不能掩盖原始错误；仍由调用方抛出 RuntimeError
                return
            logging.getLogger("account_store").error(
                "registry.json 解析失败(%s)，已隔离到 %s", type(error).__name__, backup
            )
            return

    def _save_registry(self, registry: Registry) -> None:
        """保存注册表（线程安全+原子写入）。"""
        with self._lock:
            self._registry = registry
            _atomic_write_json(self._registry_path, registry.to_dict())

    def list_accounts(self) -> list[AccountMeta]:
        """列出所有账号。"""
        registry = self._load_registry()
        return list(registry.accounts.values())

    def _find_registered_account(
        self, registry: Registry, account_id: str
    ) -> AccountMeta | None:
        return registry.accounts.get(account_id)

    def get_account(self, account_id: str) -> AccountMeta | None:
        """获取单个账号。"""
        registry = self._load_registry()
        return self._find_registered_account(registry, account_id)

    def get_active_account(self) -> AccountMeta | None:
        """获取当前活跃账号，若未设置或已被禁用则回退到首个可用账号。"""
        registry = self._load_registry()
        if (
            registry.active_account_id
            and registry.active_account_id in registry.accounts
        ):
            active = registry.accounts[registry.active_account_id]
            if active.enabled:
                return active
        if registry.accounts:
            first_enabled = next(
                (acc for acc in registry.accounts.values() if acc.enabled),
                None,
            )
            if first_enabled is not None:
                registry.active_account_id = first_enabled.id
                self._save_registry(registry)
                return first_enabled
        return None

    def get_active_auth_path(self) -> Path | None:
        """获取当前活跃账号的 auth.json 路径。"""
        account = self.get_active_account()
        if account is None:
            return None
        return self._accounts_dir / account.id / "auth.json"

    def set_active_account(self, account_id: str) -> AccountMeta | None:
        """设置活跃账号，返回账号元数据或 None（如果不存在）。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        changed = registry.active_account_id != acc.id
        registry.active_account_id = acc.id
        now = datetime.now(UTC).isoformat()
        acc.last_used = now
        if changed:
            self._save_registry(registry)
        return acc

    def save_account(
        self,
        name: str,
        email: str | None,
        storage_state: dict[str, object],
        account_id: str | None = None,
        auth_user: str = "0",
        cookie_id: str | None = None,
        tier: str = "Free",
        enabled: bool = True,
        group: str = "default",
        notes: str = "",
        proxy_url: str | None = None,
    ) -> AccountMeta:
        """保存新账号。

        多文件读-改-写序列（auth.json/meta.json/registry/兄弟账号同步）全程
        持锁：与 update_account_cookies 的线程安全契约对齐，否则一旦出现
        线程池调用方（类本身声明线程安全）就会并发回写互相覆盖丢 Cookie。
        self._lock 为 RLock，内部 _load/_save_registry/_sync_sibling_cookies_locked
        可重入不受影响。
        """
        with self._lock:
            return self._save_account_locked(
                name=name,
                email=email,
                storage_state=storage_state,
                account_id=account_id,
                auth_user=auth_user,
                cookie_id=cookie_id,
                tier=tier,
                enabled=enabled,
                group=group,
                notes=notes,
                proxy_url=proxy_url,
            )

    def _save_account_locked(
        self,
        name: str,
        email: str | None,
        storage_state: dict[str, object],
        account_id: str | None = None,
        auth_user: str = "0",
        cookie_id: str | None = None,
        tier: str = "Free",
        enabled: bool = True,
        group: str = "default",
        notes: str = "",
        proxy_url: str | None = None,
    ) -> AccountMeta:
        """save_account 的持锁实现（调用方必须已持有 self._lock）。"""
        registry = self._load_registry()
        now = datetime.now(UTC).isoformat()
        created_at = now
        resolved_tier = tier or "Free"
        resolved_enabled = enabled
        resolved_group = group or "default"
        resolved_notes = notes
        resolved_proxy = proxy_url

        if account_id is None and email:
            for acc in registry.accounts.values():
                if acc.email == email and acc.auth_user == auth_user:
                    account_id = acc.id
                    created_at = acc.created_at
                    if resolved_tier == "Free" and acc.tier and acc.tier != "Free":
                        resolved_tier = acc.tier
                    if resolved_group == "default" and acc.group:
                        resolved_group = acc.group
                    if not resolved_notes and acc.notes:
                        resolved_notes = acc.notes
                    if not acc.enabled and enabled is True:
                        resolved_enabled = acc.enabled
                    if resolved_proxy is None and acc.proxy_url:
                        resolved_proxy = acc.proxy_url
                    break

        if account_id is not None:
            existing = self._find_registered_account(registry, account_id)
            if existing is not None:
                account_id = existing.id
                created_at = existing.created_at
                if (
                    resolved_tier == "Free"
                    and existing.tier
                    and existing.tier != "Free"
                ):
                    resolved_tier = existing.tier
                if resolved_group == "default" and existing.group:
                    resolved_group = existing.group
                if not resolved_notes and existing.notes:
                    resolved_notes = existing.notes
                if not existing.enabled and enabled is True:
                    resolved_enabled = existing.enabled
                if resolved_proxy is None and existing.proxy_url:
                    resolved_proxy = existing.proxy_url
        else:
            account_id = _generate_account_id()

        account_dir = _safe_account_dir(self._accounts_dir, account_id)
        safe_id = account_dir.name
        existing_meta = registry.accounts.get(safe_id)
        # 已探测到的邮箱不应被调用方传 None 抹掉：环境变量注入账号每次启动都会
        # 用 email=None 重建，若直接覆盖会让面板里探活到的邮箱反复消失。
        resolved_email = (
            email
            if email is not None
            else (existing_meta.email if existing_meta else None)
        )
        meta = AccountMeta(
            id=safe_id,
            name=name,
            email=resolved_email,
            created_at=created_at,
            last_used=now,
            auth_user=auth_user,
            cookie_id=cookie_id or (existing_meta.cookie_id if existing_meta else None),
            tier=resolved_tier,
            tier_checked_at=existing_meta.tier_checked_at if existing_meta else None,
            last_rotated_at=existing_meta.last_rotated_at if existing_meta else None,
            enabled=resolved_enabled,
            group=resolved_group,
            notes=resolved_notes,
            proxy_url=resolved_proxy,
        )
        account_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(account_dir / "auth.json", storage_state)
        _atomic_write_json(account_dir / "meta.json", meta.to_dict())
        registry.accounts[safe_id] = meta
        if registry.active_account_id is None:
            registry.active_account_id = safe_id
        self._sync_sibling_cookies_locked(registry)
        self._save_registry(registry)
        return meta

    def delete_account(self, account_id: str) -> bool:
        """删除账号，返回是否成功。

        全程持锁：del registry.accounts[safe_id] 变更字典大小，而工作
        线程（Set-Cookie 持久化经 asyncio.to_thread 进入）在
        _update_account_cookies_locked 里持锁迭代同一字典——无锁删除会
        触发 RuntimeError 被上层 logger.debug 静默吞掉，该次滚动凭据
        持久化随之丢失。RLock 可重入，内部 _load/_save_registry 无碍。
        """
        with self._lock:
            registry = self._load_registry()
            acc = self._find_registered_account(registry, account_id)
            if acc is None:
                return False
            safe_id = acc.id
            # 删除目录
            account_dir = _safe_account_dir(self._accounts_dir, safe_id)
            if account_dir.is_dir():
                shutil.rmtree(account_dir, ignore_errors=True)
            # 从注册表移除
            del registry.accounts[safe_id]
            if registry.active_account_id == safe_id:
                registry.active_account_id = next(iter(registry.accounts), None)
            self._save_registry(registry)
            return True

    def update_account(self, account_id: str, name: str) -> AccountMeta | None:
        """更新账号名称。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        acc.name = name
        # 同步更新 meta.json
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return acc

    def update_account_email(self, account_id: str, email: str) -> bool:
        """更新账号的登录邮箱，并持久化 meta.json 与 registry.json。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None or acc.email == email:
            return False
        acc.email = email
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return True

    def update_account_proxy(self, account_id: str, proxy_url: str | None) -> bool:
        """更新账号的独立出口代理（None 表示回退全局代理），并持久化 meta.json 与 registry.json。"""
        from aistudio_api.infrastructure.account.cookie_parser import (
            normalize_proxy_url,
        )

        clean_proxy = normalize_proxy_url(proxy_url)
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None or acc.proxy_url == clean_proxy:
            return False
        acc.proxy_url = clean_proxy
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return True

    def update_account_tier(
        self, account_id: str, tier: str, checked_at: float | None = None
    ) -> bool:
        """更新账号的订阅权益等级 (Free / Pro / Ultra / Ultra_20X / Plus)，并持久化 meta.json 与 registry.json。"""
        from aistudio_api.infrastructure.account.cookie_parser import (
            normalize_benefit_tier,
        )

        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return False
        clean_tier = normalize_benefit_tier(tier)
        changed = acc.tier != clean_tier
        acc.tier = clean_tier
        if checked_at is not None:
            acc.tier_checked_at = checked_at
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return changed

    def _sync_sibling_cookies_locked(self, registry: Registry) -> bool:
        """同步共享同一浏览器会话 (SAPISID / SID) 的多登兄弟账号 (u/0..u/N) 的滚动 Cookie。"""
        from aistudio_api.infrastructure.account.cookie_parser import (
            ROTATING_SESSION_COOKIE_NAMES,
        )

        groups: dict[str, list[tuple[AccountMeta, Path, dict[str, str]]]] = {}
        for acc_id, acc in registry.accounts.items():
            try:
                auth_path = _safe_account_dir(self._accounts_dir, acc_id) / "auth.json"
            except ValueError:
                continue
            if not auth_path.is_file():
                continue
            # mtime 缓存加载（P9）：批量导入时 save_account 逐个触发同步，
            # 若每次都全量重读所有 auth.json 就是 O(M×N) 次文件读。
            cached_load = _load_auth_cookies_cached(auth_path)
            if cached_load is None:
                continue
            cdict, anchor = cached_load
            if not cdict or not anchor:
                continue
            groups.setdefault(anchor, []).append((acc, auth_path, cdict))

        meta_changed = False
        for group in groups.values():
            if len(group) <= 1:
                continue
            # 按是否存在 __Secure-1PSIDTS 以及最近轮换时间排序（最旧在前，最新在后，后者覆盖前者）。
            # last_rotated_at 为 None 表示「从未后台轮换」——通常是刚导入的新鲜
            # Cookie；归一为 0.0 会把它排在最旧、被库内陈旧轮换值覆盖并固化。
            # None 排在「有轮换时间」之前但晚于同为此前从未轮换的账号没有意义，
            # 用 last_used（ISO 时间串）+ created_at 兜底区分新鲜度。
            def _freshness_key(meta) -> tuple[float, str, str]:
                rotated = meta.last_rotated_at
                # 从未轮换的新导入账号视为"最新"：排在一切已轮换账号之后
                rotated_key = rotated if rotated is not None else float("inf")
                return (rotated_key, meta.last_used or "", meta.created_at or "")

            group.sort(
                key=lambda item: (
                    1
                    if (
                        item[2].get("__Secure-1PSIDTS")
                        or item[2].get("__Secure-3PSIDTS")
                    )
                    else 0,
                    *_freshness_key(item[0]),
                )
            )
            best_cookies: dict[str, str] = {}
            max_rotated = 0.0
            for acc, _, cdict in group:
                for k, v in cdict.items():
                    if v:
                        best_cookies[k] = v
                if acc.last_rotated_at and acc.last_rotated_at > max_rotated:
                    max_rotated = acc.last_rotated_at

            for acc, auth_path, cdict in group:
                merged = dict(cdict)
                updated = False
                for k, v in best_cookies.items():
                    if (
                        k not in merged or k in ROTATING_SESSION_COOKIE_NAMES
                    ) and merged.get(k) != v:
                        merged[k] = v
                        updated = True
                if updated:
                    payload = {
                        "cookies": [
                            {"name": k, "value": v} for k, v in merged.items() if v
                        ]
                    }
                    _atomic_write_json(auth_path, payload)
                if max_rotated > 0 and acc.last_rotated_at != max_rotated:
                    acc.last_rotated_at = max_rotated
                    meta_path = auth_path.parent / "meta.json"
                    if meta_path.exists():
                        _atomic_write_json(meta_path, acc.to_dict())
                    meta_changed = True
        return meta_changed

    def update_account_cookies(
        self,
        account_id: str | None = None,
        *,
        auth_file: str | Path | None = None,
        new_cookies: dict[str, str],
        rotated_at: float | None = None,
    ) -> list[str]:
        """将更新后的 Cookie 写回目标账号及其同源多登兄弟账号 (u/0..u/N) 的 auth.json。

        全程持锁（3.4）：同锚点兄弟账号的读盘-合并-写回序列若不互斥，
        并发回写会互相覆盖导致轮换 Cookie 丢失、后续请求 403。
        """
        if not new_cookies:
            return []

        with self._lock:
            return self._update_account_cookies_locked(
                account_id,
                auth_file=auth_file,
                new_cookies=new_cookies,
                rotated_at=rotated_at,
            )

    def _update_account_cookies_locked(
        self,
        account_id: str | None,
        *,
        auth_file: str | Path | None,
        new_cookies: dict[str, str],
        rotated_at: float | None,
    ) -> list[str]:
        from aistudio_api.infrastructure.account.cookie_parser import (
            compute_session_anchor,
            load_cookies_from_auth_file,
        )

        registry = self._load_registry()
        target_id = account_id
        if not target_id and auth_file:
            try:
                p = Path(auth_file).resolve()
                cand_id = p.parent.name
                if cand_id in registry.accounts:
                    target_id = cand_id
                elif p.is_file():
                    cur = load_cookies_from_auth_file(p)
                    merged = {**cur, **{k: v for k, v in new_cookies.items() if v}}
                    if merged != cur:
                        _atomic_write_json(
                            p,
                            {
                                "cookies": [
                                    {"name": k, "value": v}
                                    for k, v in merged.items()
                                    if v
                                ]
                            },
                        )
                    return []
            except Exception:
                pass

        target_acc = registry.accounts.get(target_id) if target_id else None
        target_cid = target_acc.cookie_id if target_acc else None
        anchor = compute_session_anchor(new_cookies)
        if not anchor and target_id:
            t_path = self.get_auth_path_optional(target_id, require_exists=True)
            if t_path:
                anchor = compute_session_anchor(load_cookies_from_auth_file(t_path))

        updated_ids: list[str] = []
        meta_changed = False

        for acc_id, acc in registry.accounts.items():
            try:
                apath = _safe_account_dir(self._accounts_dir, acc_id) / "auth.json"
            except ValueError:
                continue
            if not apath.is_file():
                continue
            cached_load = _load_auth_cookies_cached(apath)
            if cached_load is None:
                continue
            cur_cookies, cur_anchor = cached_load

            is_target = bool(target_id and acc_id == target_id)
            is_same_cid = bool(target_cid and acc.cookie_id == target_cid)
            is_same_anchor = bool(anchor and cur_anchor == anchor)
            if not (is_target or is_same_cid or is_same_anchor):
                continue

            merged = dict(cur_cookies)
            cookie_changed = False
            for k, v in new_cookies.items():
                if v and merged.get(k) != v:
                    merged[k] = v
                    cookie_changed = True

            if cookie_changed:
                _atomic_write_json(
                    apath,
                    {
                        "cookies": [
                            {"name": k, "value": v} for k, v in merged.items() if v
                        ]
                    },
                )
                updated_ids.append(acc_id)

            if rotated_at is not None and acc.last_rotated_at != rotated_at:
                acc.last_rotated_at = rotated_at
                meta_path = apath.parent / "meta.json"
                if meta_path.exists():
                    _atomic_write_json(meta_path, acc.to_dict())
                meta_changed = True

        if meta_changed:
            self._save_registry(registry)
        return updated_ids

    def get_auth_path(self, account_id: str) -> Path | None:
        """获取指定账号的 auth.json 路径。"""
        return self.get_auth_path_optional(account_id, require_exists=True)

    def get_auth_path_optional(
        self, account_id: str, *, require_exists: bool = False
    ) -> Path | None:
        """获取指定账号的 auth.json 路径，可选是否要求文件已存在。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        path = _safe_account_dir(self._accounts_dir, acc.id) / "auth.json"
        if require_exists and not path.exists():
            return None
        return path

    def toggle_account_enabled(
        self, account_id: str, enabled: bool | None = None
    ) -> AccountMeta | None:
        """切换账号的启用/禁用状态。若未指定 enabled 则取反。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        acc.enabled = not acc.enabled if enabled is None else bool(enabled)
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return acc

    def update_account_group(self, account_id: str, group: str) -> AccountMeta | None:
        """更新账号所属业务分组。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        clean_group = str(group or "default").strip() or "default"
        acc.group = clean_group
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return acc

    def update_account_notes(self, account_id: str, notes: str) -> AccountMeta | None:
        """更新账号运维备注信息。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        acc.notes = str(notes or "")
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return acc

    def list_groups(self) -> list[str]:
        """获取当前所有账号中存在的不重复分组列表。"""
        registry = self._load_registry()
        groups = {acc.group for acc in registry.accounts.values() if acc.group}
        if "default" not in groups:
            groups.add("default")
        return sorted(groups)

    def batch_toggle_group(self, group: str, enabled: bool) -> list[AccountMeta]:
        """按分组批量启用或禁用账号。"""
        registry = self._load_registry()
        modified: list[AccountMeta] = []
        for acc in registry.accounts.values():
            if acc.group == group:
                acc.enabled = enabled
                account_dir = _safe_account_dir(self._accounts_dir, acc.id)
                meta_path = account_dir / "meta.json"
                if meta_path.exists():
                    _atomic_write_json(meta_path, acc.to_dict())
                modified.append(acc)
        if modified:
            self._save_registry(registry)
        return modified

    # ─── 预设方案 (Presets) 管理 ───
    def _ensure_default_presets(self) -> None:
        if self._presets_path.exists():
            return
        defaults = {p.id: p for p in _load_system_presets_from_config()}
        if defaults:
            self._save_presets(defaults)

    def _load_presets(self) -> dict[str, AccountPreset]:
        with self._lock:
            if self._presets is not None:
                return self._presets
            sys_presets = _load_system_presets_from_config()
            if not self._presets_path.exists():
                defaults = {p.id: p for p in sys_presets}
                if defaults:
                    self._save_presets(defaults)
                self._presets = defaults
                return self._presets
            try:
                raw = json.loads(self._presets_path.read_text(encoding="utf-8"))
                presets: dict[str, AccountPreset] = {}
                if isinstance(raw, dict):
                    for pid, pdata in raw.items():
                        if isinstance(pdata, dict):
                            presets[str(pid)] = AccountPreset.from_dict(pdata)
                for sys_p in sys_presets:
                    if sys_p.id not in presets or presets[sys_p.id].is_system:
                        presets[sys_p.id] = sys_p
                self._presets = presets
            except Exception:
                self._presets = {p.id: p for p in sys_presets}
            return self._presets

    def _save_presets(self, presets: dict[str, AccountPreset]) -> None:
        with self._lock:
            self._presets = presets
            payload = {pid: p.to_dict() for pid, p in presets.items()}
            _atomic_write_json(self._presets_path, payload)

    def list_presets(self) -> list[AccountPreset]:
        """列出所有预设方案。"""
        presets = self._load_presets()
        return list(presets.values())

    def get_preset(self, preset_id: str) -> AccountPreset | None:
        """获取单个预设方案。"""
        presets = self._load_presets()
        return presets.get(preset_id)

    def save_preset(self, preset: AccountPreset) -> AccountPreset:
        """创建或更新预设方案。"""
        presets = self._load_presets()
        now = datetime.now(UTC).isoformat()
        if not preset.created_at:
            preset.created_at = now
        preset.updated_at = now
        presets[preset.id] = preset
        self._save_presets(presets)
        return preset

    def delete_preset(self, preset_id: str) -> bool:
        """删除自定义预设方案（内置方案不可删除）。"""
        presets = self._load_presets()
        if preset_id not in presets:
            return False
        if presets[preset_id].is_system:
            raise ValueError("系统内置预设不允许删除")
        del presets[preset_id]
        self._save_presets(presets)
        return True

    def create_snapshot_preset(self, name: str, description: str = "") -> AccountPreset:
        """保存当前所有已启用账号的 ID 列表为预设方案。"""
        import secrets

        registry = self._load_registry()
        active_ids = [acc.id for acc in registry.accounts.values() if acc.enabled]
        now = datetime.now(UTC).isoformat()
        pid = f"preset_{secrets.token_hex(4)}"
        preset = AccountPreset(
            id=pid,
            name=name.strip() or "自定义快照方案",
            description=description.strip(),
            action="snapshot",
            account_ids=active_ids,
            is_system=False,
            created_at=now,
            updated_at=now,
        )
        return self.save_preset(preset)

    def apply_preset(
        self, preset_id: str
    ) -> tuple[AccountPreset, list[str], list[str]]:
        """应用预设：根据规则或快照批量更新账号 enabled 状态。"""
        preset = self.get_preset(preset_id)
        if preset is None:
            raise ValueError(f"预设方案不存在: {preset_id}")

        registry = self._load_registry()
        enabled_ids: list[str] = []
        disabled_ids: list[str] = []

        for acc in registry.accounts.values():
            should_enable = False
            if preset.action == "enable_all":
                should_enable = True
            elif preset.action == "disable_all":
                should_enable = False
            elif preset.action == "snapshot":
                should_enable = acc.id in preset.account_ids
            elif preset.action == "filter_tier":
                should_enable = acc.tier in preset.target_tiers
            elif preset.action == "filter_group":
                should_enable = acc.group in preset.target_groups
            else:
                should_enable = acc.id in preset.account_ids

            acc.enabled = should_enable
            account_dir = _safe_account_dir(self._accounts_dir, acc.id)
            meta_path = account_dir / "meta.json"
            if meta_path.exists():
                _atomic_write_json(meta_path, acc.to_dict())

            if should_enable:
                enabled_ids.append(acc.id)
            else:
                disabled_ids.append(acc.id)

        # 如果当前活跃账号被禁用了，清除活跃标记以便重新择优激活
        if registry.active_account_id in disabled_ids:
            registry.active_account_id = None

        self._save_registry(registry)
        return preset, enabled_ids, disabled_ids
