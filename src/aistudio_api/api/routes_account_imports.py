"""账号导入路由（Cookie / 探测批量 / 凭据 Bundle）。

拆分说明：本文件端点族原位于 ``routes_accounts.py``，按审查要求拆出
（import cookies / probe-and-import / import bundle 及其请求响应模型）。
挂载方式与 ``routes_accounts`` 完全一致：``prefix="/accounts"``，由
app.py 以 ``/api`` 前缀 + 无前缀两种方式挂载，URL 保持不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from aistudio_api.api.dependencies import get_account_service, get_runtime_state
from aistudio_api.api.routes_accounts import AccountResponse, to_account_response
from aistudio_api.infrastructure.account.cookie_parser import (
    parse_cookie_string,
    probe_account_benefit_tier,
    probe_account_email,
)
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.redaction import redact_text

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState
    from aistudio_api.application.account_service import AccountService

logger = get_logger("routes.account_imports")

router = APIRouter(prefix="/accounts")


def _masked_proxy_for_request(
    request: Request | None, proxy_url: str | None
) -> str | None:
    """API Key 回退会话掩码代理凭据（与账号列表同口径，见 routes_accounts）。"""
    if proxy_url and request is not None:
        from aistudio_api.api.dependencies import is_admin_password_session

        if not is_admin_password_session(request):
            return redact_text(proxy_url)
    return proxy_url


class ImportCookiesRequest(BaseModel):
    cookies: str
    name: str | None = None
    email: str | None = None
    account_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_\-\.@]+$")
    auth_user: str = "0"
    tier: str | None = None
    # 可选：导入时直接绑定出口代理；留空则尝试从 cookies 文本中自动识别
    proxy_url: str | None = None


class ImportCookiesResponse(BaseModel):
    account_id: str
    name: str
    cookie_count: int
    auth_user: str = "0"
    tier: str = "Free"
    proxy_url: str | None = None


class ProbeAndImportRequest(BaseModel):
    cookies: str
    name_prefix: str | None = None
    # 可选：探测与后续请求共用该代理；留空则从 cookies 文本中自动识别
    proxy_url: str | None = None


class ProbeAndImportResponse(BaseModel):
    imported_count: int
    accounts: list[AccountResponse]


class BundleAccountItem(BaseModel):
    id: str | None = None
    name: str | None = None
    email: str | None = None
    cookies: str
    auth_user: str = "0"
    tier: str | None = None
    proxy_url: str | None = None


class ImportBundleRequest(BaseModel):
    file_path: str | None = None
    content: str | None = None
    accounts: list[BundleAccountItem] | None = None


class ImportBundleResponse(BaseModel):
    imported_count: int
    accounts: list[AccountResponse]


@router.post("/import-cookies", response_model=ImportCookiesResponse)
async def import_cookies(
    req: ImportCookiesRequest,
    request: Request,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ImportCookiesResponse:
    """从 Cookie 导入账号（支持 JSON 数组、Netscape、KV 或 JSON 对象）。

    Cookie 文本可同时携带出口代理：``代理 + Cookie`` 或 ``Cookie + 代理``，
    分隔符支持 ``+``、换行、``|``、逗号、空格；也可用 ``proxy_url`` 显式
    指定（显式参数优先）。识别到的代理会随账号一起入库。
    """
    from aistudio_api.infrastructure.account.cookie_parser import (
        normalize_proxy_url,
        split_proxy_from_paste,
    )

    auto_proxy, cookie_text = split_proxy_from_paste(req.cookies)
    req.cookies = cookie_text
    effective_proxy = auto_proxy
    if req.proxy_url:
        effective_proxy = normalize_proxy_url(req.proxy_url)
        if effective_proxy is None:
            raise HTTPException(status_code=400, detail="代理地址格式无法识别")
    raw_text = req.cookies.strip()
    if raw_text.startswith(("{", "[")):
        with contextlib.suppress(Exception):
            parsed_json = json.loads(raw_text)
            # Check if this is a multi-account bundle
            bundle_list: list[object] | None = None
            if (
                isinstance(parsed_json, dict)
                and "accounts" in parsed_json
                and isinstance(parsed_json["accounts"], list)
            ):
                bundle_list = parsed_json["accounts"]
            elif (
                isinstance(parsed_json, list)
                and len(parsed_json) > 0
                and isinstance(parsed_json[0], dict)
                and "cookies" in parsed_json[0]
            ):
                bundle_list = parsed_json

            if bundle_list:
                bundle_req = ImportBundleRequest(
                    accounts=[
                        BundleAccountItem(
                            name=str(a.get("name") or "Google Account"),
                            email=a.get("email")
                            if isinstance(a.get("email"), str)
                            else None,
                            cookies=str(a.get("cookies") or ""),
                            auth_user=str(
                                a.get("auth_user") or a.get("authUser") or "0"
                            ),
                            # g1Tier 为导出器原始枚举（TIER0/1/2）：与下方单账号
                            # 分支同款翻译，否则 "TIER1" 直落 registry 会被
                            # normalize_benefit_tier 按未知键降级为 Free。
                            tier=(
                                str(a.get("tier") or "")
                                or {
                                    "TIER1": "Pro",
                                    "TIER2": "Ultra",
                                    "TIER0": "Free",
                                }.get(str(a.get("g1Tier") or "").upper(), "")
                                or str(a.get("g1Tier") or "Free")
                            ),
                            proxy_url=str(a.get("proxy_url") or effective_proxy or "")
                            or None,
                        )
                        for a in bundle_list
                        if isinstance(a, dict) and a.get("cookies")
                    ]
                )
                bundle_res = await import_bundle(
                    bundle_req, request, account_service, runtime_state
                )
                first_id = (
                    bundle_res.accounts[0].id if bundle_res.accounts else "bundle"
                )
                return ImportCookiesResponse(
                    account_id=first_id,
                    name=f"已成功批量导入 {bundle_res.imported_count} 个账号",
                    cookie_count=bundle_res.imported_count,
                    auth_user="0",
                    proxy_url=_masked_proxy_for_request(request, effective_proxy),
                )

            # Single account JSON export
            if isinstance(parsed_json, dict):
                if "authUser" in parsed_json and req.auth_user == "0":
                    req.auth_user = str(parsed_json["authUser"])
                if "g1Tier" in parsed_json and not req.tier:
                    tm_tier = str(parsed_json["g1Tier"]).upper()
                    if tm_tier == "TIER1":
                        req.tier = "Pro"
                    elif tm_tier == "TIER2":
                        req.tier = "Ultra"
                    elif tm_tier == "TIER0":
                        req.tier = "Free"
                if "email" in parsed_json and not req.email:
                    req.email = str(parsed_json["email"])
    had_active_account = account_service.get_active_account() is not None
    storage_state = parse_cookie_string(req.cookies)
    raw_cookies = storage_state.get("cookies")
    cookie_list: list[dict[str, object]] = (
        raw_cookies if isinstance(raw_cookies, list) else []
    )
    cookie_count = len(cookie_list)

    if cookie_count == 0:
        raise HTTPException(status_code=400, detail="未解析到有效 cookie")

    name = req.name or (
        f"Google Account (u/{req.auth_user})" if req.auth_user != "0" else "导入的账号"
    )

    import secrets

    cid = f"cookie_{secrets.token_hex(4)}"
    detected_email = req.email
    detected_tier = req.tier
    if not detected_email or not detected_tier:
        raw_cookies_list = storage_state.get("cookies")
        if isinstance(raw_cookies_list, list):
            cookie_dict = {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in raw_cookies_list
                if isinstance(c, dict) and c.get("name")
            }
            if cookie_dict:
                if not detected_email:
                    with contextlib.suppress(Exception):
                        detected_email = await probe_account_email(
                            cookie_dict,
                            auth_user=req.auth_user or "0",
                            # 探活走账号绑定出口，避免粘贴「代理+Cookie」
                            # 导入时探活走全局出口触发风控（与批量导入一致）。
                            proxy_url=effective_proxy,
                        )
                if not detected_tier:
                    with contextlib.suppress(Exception):
                        probed = await probe_account_benefit_tier(
                            cookie_dict,
                            auth_user=req.auth_user or "0",
                            proxy_url=effective_proxy,
                        )
                        if probed and probed != "Free":
                            detected_tier = probed
    detected_tier = detected_tier or "Free"
    # save_account 是多文件原子写序列（auth/meta/registry + 兄弟账号 Cookie
    # 同步会重读全部账号文件），直接在事件循环执行会拖停所有在途 SSE 流；
    # AccountStore 自身线程安全（全程持锁设计），下放线程池执行。
    account = await asyncio.to_thread(
        account_service.save_account_from_cookies,
        name=name,
        email=detected_email,
        storage_state=storage_state,
        account_id=req.account_id,
        auth_user=req.auth_user or "0",
        cookie_id=cid,
        tier=detected_tier,
        proxy_url=effective_proxy,
    )
    if runtime_state.rotator:
        runtime_state.rotator.add_account(account.id)
        runtime_state.rotator.clear_cooldown(account.id)
    gateway_session = runtime_state.client.session if runtime_state.client else None
    active_acc = account_service.get_active_account()
    if gateway_session is not None and (
        not had_active_account or (active_acc and active_acc.id == account.id)
    ):
        # 激活即触发目录缓存失效（收敛在 AccountService.activate_account）。
        await account_service.activate_account(account.id, gateway_session)
    return ImportCookiesResponse(
        account_id=account.id,
        name=account.name,
        cookie_count=cookie_count,
        auth_user=account.auth_user,
        tier=account.tier,
        proxy_url=_masked_proxy_for_request(request, account.proxy_url),
    )


@router.post("/probe-import", response_model=ProbeAndImportResponse)
async def probe_and_import(
    req: ProbeAndImportRequest,
    request: Request,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ProbeAndImportResponse:
    """探测 Cookie 中的多个已登录账号并批量导入。"""
    from aistudio_api.infrastructure.account.cookie_parser import (
        normalize_proxy_url,
        parse_cookie_string,
        probe_google_accounts_infinite,
        split_proxy_from_paste,
    )

    auto_proxy, cookie_text = split_proxy_from_paste(req.cookies)
    req.cookies = cookie_text
    effective_proxy = auto_proxy
    if req.proxy_url:
        effective_proxy = normalize_proxy_url(req.proxy_url)
        if effective_proxy is None:
            raise HTTPException(status_code=400, detail="代理地址格式无法识别")
    had_active_account = account_service.get_active_account() is not None
    probed = await probe_google_accounts_infinite(
        req.cookies, proxy_url=effective_proxy
    )
    if not probed:
        raise HTTPException(status_code=400, detail="未探测到有效已登录 Google 账号")

    storage_state = parse_cookie_string(req.cookies)
    prefix = req.name_prefix.strip() if req.name_prefix else "Google Account"
    import secrets

    cid = f"cookie_{secrets.token_hex(4)}"
    # 批量导入逐账号的多文件写序列（save_account 是多文件原子写 + 兄弟
    # Cookie 同步重读全部账号文件）下放线程池：M 个账号 ≈ M×(5-15ms)
    # 同步 IO，留在事件循环会拖停所有在途 SSE 流（与另两条导入路径一致）。
    metas = await asyncio.to_thread(
        account_service.batch_import_accounts,
        probed_list=probed,
        storage_state=storage_state,
        prefix=prefix,
        cookie_id=cid,
        proxy_url=effective_proxy,
    )
    rotator = getattr(runtime_state, "rotator", None)
    if rotator:
        for meta in metas:
            rotator.add_account(meta.id)
            rotator.clear_cooldown(meta.id)

    imported_accounts = [
        to_account_response(account, request=request) for account in metas
    ]

    client_obj = getattr(runtime_state, "client", None)
    if not had_active_account and metas and client_obj is not None:
        gateway_session = client_obj.session
        if gateway_session is not None:
            # 激活即触发目录缓存失效（收敛在 AccountService.activate_account）。
            await account_service.activate_account(metas[0].id, gateway_session)
    logger.info("Cookie 探活完成: 导入账号数=%d", len(imported_accounts))
    return ProbeAndImportResponse(
        imported_count=len(imported_accounts),
        accounts=imported_accounts,
    )


@router.post("/import-bundle", response_model=ImportBundleResponse)
async def import_bundle(
    req: ImportBundleRequest,
    request: Request,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ImportBundleResponse:
    """批量导入多个账号的凭据 Bundle（支持传入 JSON 文本、直接解析列表或服务器本地文件路径）。"""
    import json
    import secrets

    from aistudio_api.infrastructure.account.cookie_parser import (
        normalize_proxy_url,
        parse_cookie_string,
        split_proxy_from_paste,
    )

    items_to_import: list[dict[str, object]] = []

    if req.accounts:
        for a in req.accounts:
            items_to_import.append(
                {
                    "name": a.name or "Google Account",
                    "email": a.email,
                    "cookies": a.cookies,
                    "auth_user": a.auth_user or "0",
                    "tier": a.tier or "Free",
                    "proxy_url": a.proxy_url,
                }
            )
    elif req.content:
        try:
            raw_data = json.loads(req.content)
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"JSON 内容解析失败: {e}"
            ) from e
        if isinstance(raw_data, list):
            raw_list = raw_data
        elif isinstance(raw_data, dict):
            raw_list = raw_data.get("accounts") or raw_data.get("profiles") or []
        else:
            raw_list = []
        for item in raw_list:
            if isinstance(item, dict) and item.get("cookies"):
                items_to_import.append(item)
    elif req.file_path:
        # 安全：file_path 只允许读取受信任的导入根目录下的 JSON 文件。
        # 此前对任意绝对路径 read_text 并回显 str(e)/绝对路径，等价于
        # 认证后的任意文件读 + 存在性探测。根目录默认取 cwd 与项目根下
        # 的 data/ 目录，可通过 AISTUDIO_IMPORT_ROOTS（os.pathsep 分隔）
        # 追加；前端示例里的 /sdcard/Download/... 需由部署者显式登记。
        # 本文件位于 <repo>/src/aistudio_api/api/，因此 parents[3] 才是仓库根
        # （parents[4] 会越界到盘符根，把 /data 之类放进白名单，同时也让
        # 非仓库 cwd 启动时项目自身的 data/ 反而不在白名单内）。
        project_root = Path(__file__).resolve().parents[3]
        allowed_roots: list[Path] = [
            (Path.cwd() / "data").resolve(),
            (project_root / "data").resolve(),
        ]
        # 自定义数据目录部署（AISTUDIO_DATA_DIR，PaaS/测试常见）下，用户
        # 把导出 JSON 放在真实数据目录内也应可导入。
        with contextlib.suppress(Exception):
            from aistudio_api.config import resolve_data_dir

            allowed_roots.append(resolve_data_dir().resolve())
        env_roots = os.getenv("AISTUDIO_IMPORT_ROOTS", "")
        for raw_root in env_roots.split(os.pathsep):
            if raw_root.strip():
                allowed_roots.append(Path(raw_root.strip()).expanduser().resolve())
        fpath = Path(req.file_path).expanduser().resolve()
        if (
            not any(fpath.is_relative_to(root) for root in allowed_roots)
            or fpath.suffix.lower() != ".json"
        ):
            raise HTTPException(status_code=400, detail="文件路径不在允许的导入目录内")
        if not fpath.is_file():
            raise HTTPException(
                status_code=400, detail="文件不存在或不是有效 JSON 文件"
            )
        try:
            raw_text = fpath.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
        except Exception as e:
            # 不回显 str(e)（可能含绝对路径/内容片段），仅给固定文案
            logger.warning("导入文件读取/解析失败: %s", type(e).__name__)
            raise HTTPException(status_code=400, detail="读取或解析导入文件失败") from e
        if isinstance(raw_data, list):
            raw_list = raw_data
        elif isinstance(raw_data, dict):
            raw_list = raw_data.get("accounts") or raw_data.get("profiles") or []
        else:
            raw_list = []
        for item in raw_list:
            if isinstance(item, dict) and item.get("cookies"):
                items_to_import.append(item)
    else:
        raise HTTPException(
            status_code=400,
            detail="请提供 accounts 列表、content JSON 文本或 file_path 本地文件路径",
        )

    if not items_to_import:
        raise HTTPException(
            status_code=400, detail="未解析到任何包含有效 cookies 的账号条目"
        )

    had_active_account = account_service.get_active_account() is not None
    imported_accounts: list[AccountResponse] = []
    for idx, item in enumerate(items_to_import):
        raw_cookie_str = str(item.get("cookies") or "")
        if not raw_cookie_str:
            continue
        auto_proxy, cookie_text = split_proxy_from_paste(raw_cookie_str)
        item_proxy = item.get("proxy_url")
        effective_proxy = auto_proxy
        if isinstance(item_proxy, str) and item_proxy.strip():
            effective_proxy = normalize_proxy_url(item_proxy)
            if effective_proxy is None:
                raise HTTPException(status_code=400, detail="代理地址格式无法识别")
        storage_state = parse_cookie_string(cookie_text)
        if not storage_state.get("cookies"):
            continue

        acc_name = str(item.get("name") or f"Account {idx + 1}")
        acc_email = str(item["email"]) if item.get("email") else None
        acc_auth_user = str(item.get("auth_user") or "0")
        acc_tier = str(item.get("tier") or "")
        if not acc_email or not acc_tier:
            raw_cookies_list = storage_state.get("cookies")
            if isinstance(raw_cookies_list, list):
                cookie_dict = {
                    str(c.get("name") or ""): str(c.get("value") or "")
                    for c in raw_cookies_list
                    if isinstance(c, dict) and c.get("name")
                }
                if cookie_dict:
                    if not acc_email:
                        with contextlib.suppress(Exception):
                            acc_email = await probe_account_email(
                                cookie_dict,
                                auth_user=acc_auth_user,
                                # 探活走账号绑定出口（与面板单号探活一致），
                                # 粘贴「代理+Cookie」导入时避免探活走全局
                                # 出口触发风控。
                                proxy_url=effective_proxy,
                            )
                    if not acc_tier:
                        with contextlib.suppress(Exception):
                            probed = await probe_account_benefit_tier(
                                cookie_dict,
                                auth_user=acc_auth_user,
                                proxy_url=effective_proxy,
                            )
                            if probed and probed != "Free":
                                acc_tier = probed
        acc_tier = acc_tier or "Free"
        cid = f"cookie_{secrets.token_hex(4)}"

        # 同上：批量导入逐账号的多文件写序列下放线程池，避免循环串行
        # 阻塞整个导入期间的事件循环。
        account = await asyncio.to_thread(
            account_service.save_account_from_cookies,
            name=acc_name,
            email=acc_email,
            storage_state=storage_state,
            auth_user=acc_auth_user,
            cookie_id=cid,
            tier=acc_tier,
            proxy_url=effective_proxy,
        )
        rotator = getattr(runtime_state, "rotator", None)
        if rotator:
            rotator.add_account(account.id)
            rotator.clear_cooldown(account.id)
        imported_accounts.append(to_account_response(account, request=request))
    if not had_active_account and imported_accounts:
        # set_active_account 亦触发目录缓存失效（收敛在 AccountService）。
        account_service.set_active_account(imported_accounts[0].id)
        client_obj = getattr(runtime_state, "client", None)
        if client_obj is not None and client_obj.session is not None:
            await account_service.activate_account(
                imported_accounts[0].id, client_obj.session
            )
    logger.info("批量导入完成: 导入账号数=%d", len(imported_accounts))
    return ImportBundleResponse(
        imported_count=len(imported_accounts),
        accounts=imported_accounts,
    )
