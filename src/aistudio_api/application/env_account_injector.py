"""环境变量账号注入：AISTUDIO_COOKIES / AISTUDIO_COOKIES_<N> / AISTUDIO_COOKIES_FILE。

适用于无持久磁盘的 PaaS / 容器部署：启动时从环境变量读取 Cookie（可携带
每账号独立代理），写入 AccountStore。注入基于会话锚点生成确定性 ID
（env_<hash>），重复启动幂等更新而非重复创建。

单条注入项格式（三种等价，按需选用）：
- ``代理+Cookie``   如 ``socks5://127.0.0.1:1080+SID=xxx; HSID=yyy``
- ``Cookie``        如 ``SID=xxx; HSID=yyy``（走全局 AISTUDIO_PROXY）
- ``Cookie+代理``   如 ``SID=xxx; HSID=yyy+http://proxy:8080``

多条之间用换行分隔（支持字面 ``\\n`` 转义或 JSON 字符串数组），
也可用编号变量 AISTUDIO_COOKIES_1、AISTUDIO_COOKIES_2 ... 逐条注入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aistudio_api.infrastructure.account.account_store import AccountStore

from aistudio_api.infrastructure.account.cookie_parser import (
    EnvCookieEntry,
    collect_env_cookie_raw_values,
    parse_env_cookie_entries,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("env_injector")

ENV_ACCOUNT_ID_PREFIX = "env_"
# env_<hash10> 为主账号，env_<hash10>_u<N> 为探活导入的多登子账号
_ENV_ID_PATTERN = re.compile(r"^env_(?P<digest>[0-9a-f]{10})(?:_u(?P<auth_user>\d+))?$")
ENV_ACCOUNT_NAME_PREFIX = "Env Account"


@dataclass(slots=True)
class EnvInjectionReport:
    """环境变量注入结果报告。"""

    entries: list[EnvCookieEntry] = field(default_factory=list)
    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    pruned_ids: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.entries)


def _env_account_name(index: int) -> str:
    return f"{ENV_ACCOUNT_NAME_PREFIX} {index}"


def inject_env_accounts(store: AccountStore) -> EnvInjectionReport:
    """启动时从环境变量注入账号（幂等：同会话锚点复用同一确定性 ID）。"""
    entries = parse_env_cookie_entries(collect_env_cookie_raw_values())
    report = EnvInjectionReport(entries=entries)
    if not entries:
        return report

    valid_hashes = {e.id_hash for e in entries}
    for e in entries:
        account_id = f"{ENV_ACCOUNT_ID_PREFIX}{e.id_hash}"
        existing = store.get_account(account_id)
        # 保留用户在 UI 中修改过的账号名（仅默认名会被重置回 Env Account N）
        name = _env_account_name(e.index)
        if (
            existing is not None
            and existing.name
            and not existing.name.startswith(ENV_ACCOUNT_NAME_PREFIX)
        ):
            name = existing.name
        store.save_account(
            name=name,
            email=None,
            storage_state=e.storage_state,
            account_id=account_id,
            auth_user="0",
            cookie_id=account_id,
            tier="Free",
        )
        # 环境变量对代理具有最终裁决权：显式设置或清除（None = 回退全局代理）
        store.update_account_proxy(account_id, e.proxy_url)
        if existing is None:
            report.created_ids.append(account_id)
        else:
            report.updated_ids.append(account_id)
        logger.info(
            "环境变量账号注入: id=%s, proxy=%s, cookie=%d 项",
            account_id,
            e.proxy_url or "(全局)",
            len(e.cookies),
        )

    # 环境变量为权威来源时，清理不再存在的注入账号；
    # 仅影响 env_ 前缀账号，且保留同锚点的子账号 (env_<hash>_uN)
    for acc in store.list_accounts():
        m = _ENV_ID_PATTERN.match(acc.id)
        if not m:
            continue
        if m.group("digest") in valid_hashes:
            continue
        if store.delete_account(acc.id):
            report.pruned_ids.append(acc.id)
            logger.info("环境变量账号清理: id=%s (已不在环境变量中)", acc.id)

    return report


async def probe_env_sub_accounts(store: AccountStore, account_ids: list[str]) -> int:
    """为本次新建的环境变量账号探活多登子账号 (u/1..u/N) 并按需导入。

    仅处理新建账号（避免每次启动重复探测、也避免复活用户手动删除的子账号）；
    探测走账号自身绑定的代理。返回新导入的子账号数量。
    """
    if not account_ids:
        return 0

    from aistudio_api.infrastructure.account.cookie_parser import (
        load_cookies_from_auth_file,
        probe_google_accounts_infinite,
    )

    imported = 0
    for account_id in account_ids:
        acc = store.get_account(account_id)
        if acc is None:
            continue
        auth_path = store.get_auth_path(account_id)
        if not auth_path:
            continue
        cookies = load_cookies_from_auth_file(auth_path)
        if not cookies:
            continue
        try:
            probed = await probe_google_accounts_infinite(
                cookies, proxy_url=acc.proxy_url
            )
        except Exception as e:
            logger.debug("环境变量账号 %s 子账号探活失败: %s", account_id, e)
            continue

        for p in probed:
            u_idx = str(p.get("auth_user") or "0")
            if u_idx == "0":
                # u/0 已同步注入，这里只回填探活得到的邮箱与订阅等级
                email_val = str(p["email"]) if p.get("email") else None
                if email_val:
                    store.update_account_email(account_id, email_val)
                tier_val = str(p.get("tier") or "Free")
                if tier_val != "Free":
                    store.update_account_tier(account_id, tier_val)
                continue

            sub_id = f"{account_id}_u{u_idx}"
            email_val = str(p["email"]) if p.get("email") else None
            existing = store.get_account(sub_id)
            if existing is None and email_val:
                # 同邮箱 + 同 auth_user 的账号已存在（如 UI 导入过）则不重复创建
                for other in store.list_accounts():
                    if (
                        other.email
                        and other.email == email_val
                        and other.auth_user == u_idx
                    ):
                        existing = other
                        break
            if existing is not None:
                continue

            store.save_account(
                name=f"{acc.name} (u/{u_idx})",
                email=email_val,
                storage_state={
                    "cookies": [{"name": k, "value": v} for k, v in cookies.items()]
                },
                account_id=sub_id,
                auth_user=u_idx,
                cookie_id=account_id,
                tier=str(p.get("tier") or "Free"),
                proxy_url=acc.proxy_url,
            )
            imported += 1
            logger.info(
                "环境变量子账号导入: id=%s (u/%s, proxy=%s)",
                sub_id,
                u_idx,
                acc.proxy_url or "(全局)",
            )
    return imported


__all__ = [
    "ENV_ACCOUNT_ID_PREFIX",
    "EnvInjectionReport",
    "inject_env_accounts",
    "probe_env_sub_accounts",
]
