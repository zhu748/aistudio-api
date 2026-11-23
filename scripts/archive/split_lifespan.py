"""Task 2: app.py lifespan 拆分（_init_core_services/_auto_refresh_tiers/_cookie_rotation_loop/_graceful_shutdown）。

锚点精确匹配替换；失败时复位重试。用法：
    uv run python /home/z/my-project/aistudio-api/scripts/split_lifespan.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path("/home/z/my-project/aistudio-api/src/aistudio_api/api/app.py")

text = SRC.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Anchor 1: TYPE_CHECKING imports for helper signatures
# ---------------------------------------------------------------------------
A1_OLD = """if TYPE_CHECKING:
    from aistudio_api.infrastructure.account.account_store import AccountMeta

_log_cfg = get_configured_logging_settings()
"""
A1_NEW = """if TYPE_CHECKING:
    from aistudio_api.application.account_rotator import AccountRotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.application.env_account_injector import EnvInjectionReport
    from aistudio_api.infrastructure.account.account_store import (
        AccountMeta,
        AccountStore,
    )

_log_cfg = get_configured_logging_settings()
"""
assert text.count(A1_OLD) == 1, "anchor1 TYPE_CHECKING"
text = text.replace(A1_OLD, A1_NEW, 1)

# ---------------------------------------------------------------------------
# Anchor 2: replace whole lifespan with orchestrator + module-level helpers
# ---------------------------------------------------------------------------
A2_OLD = '''@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aistudio_api.infrastructure.waa.daemon import (
        ensure_signer_ready,
        stop_signer_daemon,
    )

    ensure_auth_configured()
    from aistudio_api.infrastructure.utils.log_files import (
        start_file_logging,
        stop_file_logging,
    )

    start_file_logging()
    if settings.request_history:
        from aistudio_api.infrastructure.request_history import request_history

        await asyncio.to_thread(request_history().recover_interrupted)
    # 启动时自动检查并拉起本地 WAA 签名服务（若配置为本地节点）。
    # 内含阻塞 I/O，下放线程池避免卡住启动期事件循环。
    await asyncio.to_thread(ensure_signer_ready)

    from aistudio_api.application.account_rotator import init_rotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.infrastructure.account.account_store import AccountStore

    client = AIStudioClient()
    runtime_state.client = client

    # 初始化账号管理服务
    account_store = AccountStore()
    account_service = AccountService(account_store)
    runtime_state.account_service = account_service

    # 环境变量 Cookie 注入（AISTUDIO_COOKIES / _<N> / _FILE，支持 代理+Cookie / Cookie / Cookie+代理）
    from aistudio_api.application.env_account_injector import (
        inject_env_accounts,
        probe_env_sub_accounts,
    )

    env_report = inject_env_accounts(account_store)
    if env_report.total:
        logger.info(
            "环境变量账号注入完成: 共 %d 条（新建=%d, 更新=%d, 清理=%d）",
            env_report.total,
            len(env_report.created_ids),
            len(env_report.updated_ids),
            len(env_report.pruned_ids),
        )

    # 初始化黏性账号调度器
    rotator = init_rotator(account_store)
    runtime_state.rotator = rotator

    active_auth_path = account_store.get_active_auth_path()
    if active_auth_path is not None:
        await client.switch_auth(str(active_auth_path))
    logger.info(
        "服务启动完成: port=%d, 账号数=%d",
        settings.port,
        len(account_store.list_accounts()),
    )
    logger.debug(
        "账号列表: %s",
        [
            f"{a.id}(u/{a.auth_user},tier={a.tier},enabled={a.enabled})"
            for a in account_store.list_accounts()
        ],
    )

    async def _auto_refresh_tiers() -> None:

        from aistudio_api.infrastructure.account.cookie_parser import (
            load_cookies_from_auth_file,
            probe_account_benefit_tier,
            probe_account_email,
        )

        now_ts = time.time()
        accounts_to_check = [
            a
            for a in account_store.list_accounts()
            if not a.email
            or (a.tier_checked_at is None)
            or (now_ts - (a.tier_checked_at or 0) > 86400 * 3)
        ]
        if not accounts_to_check:
            return

        logger.info("校验账号邮箱与订阅等级: 待检测=%d", len(accounts_to_check))
        upgraded_count = 0
        checked_count = 0
        sem = asyncio.Semaphore(4)

        # 按代理分池的探活客户端：绑定独立代理的账号各走各的出口。
        # turn/turns/sstp 先解析为本地回环 SOCKS5（桥接子进程冷启动放
        # 线程池），解析失败抛错 → _check_one 捕获后跳过该账号（fail-
        # closed，绝不静默改直连）。池化经 _pooled_probe_client：并发
        # 首调只建一个客户端（历史上检查-构造-赋值间隙的 await 会泄漏）。
        probe_clients: dict[str, httpx.AsyncClient] = {}

        async def _get_probe_client(proxy_url: str | None) -> httpx.AsyncClient:
            return await _pooled_probe_client(probe_clients, proxy_url)

        try:

            async def _check_one(a: AccountMeta) -> None:
                nonlocal checked_count, upgraded_count
                apath = account_store.get_auth_path(a.id)
                if not apath:
                    return
                async with sem:
                    try:
                        cdict = load_cookies_from_auth_file(apath)
                        if not cdict:
                            return
                        checked_count += 1
                        probe_client = await _get_probe_client(
                            getattr(a, "proxy_url", None)
                        )
                        if not a.email:
                            em = await probe_account_email(
                                cdict,
                                auth_user=a.auth_user or "0",
                                client=probe_client,
                            )
                            if em:
                                account_store.update_account_email(a.id, em)
                        t = await probe_account_benefit_tier(
                            cdict,
                            auth_user=a.auth_user or "0",
                            client=probe_client,
                        )
                        resolved_tier = t or "Free"
                        account_store.update_account_tier(
                            a.id, resolved_tier, checked_at=now_ts
                        )
                        if resolved_tier != "Free" and a.tier == "Free":
                            upgraded_count += 1
                            logger.info(
                                "账号订阅等级更新: id=%s (u/%s) -> %s",
                                a.id,
                                a.auth_user,
                                resolved_tier,
                            )
                    except Exception as e:
                        logger.debug("账号 %s 订阅检测失败: %s", a.id, e)

            await asyncio.gather(*(_check_one(a) for a in accounts_to_check))
        finally:
            for probe_client in probe_clients.values():
                with suppress(Exception):
                    await probe_client.aclose()

        logger.info(
            "账号校验完成: 已检测=%d, 付费升级=%d",
            checked_count,
            upgraded_count,
        )

    async def _cookie_rotation_loop() -> None:
        """后台定时轮换账号滚动 Cookie (__Secure-1PSIDTS / SIDCC)，间隔 7 分钟。"""
        import httpx

        from aistudio_api.infrastructure.account.cookie_parser import (
            compute_session_anchor,
            load_cookies_from_auth_file,
            probe_aistudio_session,
        )

        await asyncio.sleep(5.0)
        while True:
            try:
                now_ts = time.time()
                seen_anchors: set[str] = set()
                accounts = account_store.list_accounts()
                # 按代理分池的轮换客户端：账号绑定独立代理时走各自出口
                rot_clients: dict[str, httpx.AsyncClient] = {}
                try:
                    for acc in accounts:
                        if not acc.enabled:
                            continue
                        st = rotator.get_account_stats(acc.id)
                        if st and st.session_expired:
                            continue
                        last_rot = acc.last_rotated_at or 0.0
                        if last_rot and (now_ts - last_rot < 420.0):
                            continue
                        apath = account_store.get_auth_path(acc.id)
                        if not apath:
                            continue
                        cdict = load_cookies_from_auth_file(apath)
                        if not cdict:
                            continue
                        anchor = compute_session_anchor(cdict)
                        if anchor:
                            if anchor in seen_anchors:
                                continue
                            seen_anchors.add(anchor)

                        acc_proxy = getattr(acc, "proxy_url", None)
                        client_key = acc_proxy or ""
                        rot_client = rot_clients.get(client_key)
                        if rot_client is None:
                            from aistudio_api.infrastructure.proxy_bridge import (
                                resolve_effective_proxy_async,
                            )

                            rot_client = httpx.AsyncClient(
                                timeout=15.0,
                                proxy=await resolve_effective_proxy_async(acc_proxy),
                                follow_redirects=False,
                            )
                            rot_clients[client_key] = rot_client

                        before_ts = cdict.get("__Secure-1PSIDTS", "")
                        rot_res = await account_service.rotate_account_cookies(
                            acc.id, client.session, client=rot_client
                        )
                        status_code = rot_res.get("status", 0)
                        if status_code == 401:
                            alive, _ = await probe_aistudio_session(
                                cdict, client=rot_client
                            )
                            if not alive:
                                rotator.record_session_expired(acc.id)
                            continue
                        new_cdict = rot_res.get("cookies")
                        after_ts = (
                            new_cdict.get("__Secure-1PSIDTS", "")
                            if isinstance(new_cdict, dict)
                            else ""
                        )
                        if (
                            status_code == 200
                            and before_ts
                            and after_ts
                            and before_ts == after_ts
                        ):
                            alive, _ = await probe_aistudio_session(
                                new_cdict if isinstance(new_cdict, dict) else cdict,
                                client=rot_client,
                            )
                            if not alive:
                                rotator.record_session_expired(acc.id)
                finally:
                    for rot_client in rot_clients.values():
                        with suppress(Exception):
                            await rot_client.aclose()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("后台 Cookie 轮换巡检异常: %s", e)
            await asyncio.sleep(60.0)

    tier_task = asyncio.create_task(_auto_refresh_tiers())
    rotate_task = asyncio.create_task(_cookie_rotation_loop())

    # 模型目录（含思考档位/付费状态元数据）后台周期刷新：启动即预热，
    # 此后每 60s 唤醒一次（真实拉网频率由 get_models 内部 600s TTL 控制，
    # 失败按内部退避 ≤60s 在下一 tick 重试）。此前刷新是"被动周期"——
    # 仅当 /v1beta/models、/v1/models 或管理端模型列表被请求时触发，
    # 从不访问模型列表端点的客户端（模型名写死）会让聊天路径长期读到
    # 静态保底快照，目录档位迁移（如 2026-11-15 hydrogen 5→6）随之漏过。
    model_discovery.start_background_refresh()

    # 新注入的环境变量账号：后台探活多登子账号 (u/1..u/N)
    env_probe_task = None
    if env_report.created_ids:

        async def _env_subaccount_probe() -> None:
            try:
                imported = await probe_env_sub_accounts(
                    account_store, list(env_report.created_ids)
                )
                if imported:
                    logger.info("环境变量子账号探活导入完成: 新增 %d 个", imported)
            except Exception as e:
                logger.debug("环境变量子账号探活失败: %s", e)

        env_probe_task = asyncio.create_task(_env_subaccount_probe())

    # 出站保活（AISTUDIO_KEEPALIVE_URL，未配置时不启动、零开销，
    # 见 infrastructure/outbound_keepalive.py）。
    from aistudio_api.infrastructure.outbound_keepalive import (
        outbound_keepalive_service,
    )

    try:
        await outbound_keepalive_service.start()
    except Exception as e:
        logger.warning("保活服务启动失败（不影响主服务）: %s - %s", type(e).__name__, e)

    yield
    logger.info("正在关闭服务")
    bg_tasks = [t for t in (tier_task, rotate_task, env_probe_task) if t]
    for bg_task in bg_tasks:
        if not bg_task.done():
            bg_task.cancel()
    # 等待取消真正落地，避免残留 pending task 在事件循环关闭时告警
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    # 停止出站保活任务（幂等；未启动时为空操作）
    await outbound_keepalive_service.stop()
    # 停止模型目录后台刷新（跨循环调用时仅清理句柄，内部已防挂死）
    await model_discovery.stop_background_refresh()
    if client:
        try:
            await client.close()
        except Exception as e:
            logger.debug("关闭客户端异常: %s", e)
    await asyncio.to_thread(stop_signer_daemon)
    # 终止全部 TURN/SSTP 桥接子进程（fail-closed 组件随主进程同生命周期）
    from aistudio_api.infrastructure.proxy_bridge import shutdown_bridge_processes

    await asyncio.to_thread(shutdown_bridge_processes)
    # 统计与调度状态都改成了防抖写入（record 后 2s 才落盘）；若进程在最后一次
    # 记账后 2s 内退出，最后一次增量会丢失。关闭时显式刷新一次内存态。
    try:
        pending_save = runtime_state._save_task
        # 只 await 仍在运行的防抖任务：已取消/已完成的残留任务（如测试
        # 事件循环销毁时被取消的旧任务）会把 CancelledError 抛进优雅关停
        # 流程；无论如何后面都会同步补一次快照，这里跳过即可。
        if pending_save is not None and not pending_save.done():
            await pending_save
        await asyncio.to_thread(runtime_state.save_stats)
        pending_rotator = runtime_state.rotator
        if pending_rotator is not None:
            await asyncio.to_thread(pending_rotator.save_state)
    except Exception as e:
        logger.debug("关闭时刷新统计/调度状态失败: %s", e)
    await asyncio.to_thread(stop_file_logging)
    runtime_state.client = None
    runtime_state.account_service = None
    runtime_state.rotator = None
'''
assert text.count(A2_OLD) == 1, "anchor2 lifespan body"

A2_NEW = '''async def _init_core_services(
    client: AIStudioClient,
) -> tuple[AccountStore, AccountService, AccountRotator, EnvInjectionReport]:
    """初始化核心服务（账号存储/服务/调度器 + 环境变量 Cookie 注入）。

    返回 (account_store, account_service, rotator, env_report)；调用方
    负责 runtime_state 赋值与后台任务派生。
    """
    from aistudio_api.application.account_rotator import init_rotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.application.env_account_injector import inject_env_accounts
    from aistudio_api.infrastructure.account.account_store import AccountStore

    # 初始化账号管理服务
    account_store = AccountStore()
    account_service = AccountService(account_store)

    # 环境变量 Cookie 注入（AISTUDIO_COOKIES / _<N> / _FILE，支持 代理+Cookie / Cookie / Cookie+代理）
    env_report = inject_env_accounts(account_store)
    if env_report.total:
        logger.info(
            "环境变量账号注入完成: 共 %d 条（新建=%d, 更新=%d, 清理=%d）",
            env_report.total,
            len(env_report.created_ids),
            len(env_report.updated_ids),
            len(env_report.pruned_ids),
        )

    # 初始化黏性账号调度器
    rotator = init_rotator(account_store)

    active_auth_path = account_store.get_active_auth_path()
    if active_auth_path is not None:
        await client.switch_auth(str(active_auth_path))
    logger.info(
        "服务启动完成: port=%d, 账号数=%d",
        settings.port,
        len(account_store.list_accounts()),
    )
    logger.debug(
        "账号列表: %s",
        [
            f"{a.id}(u/{a.auth_user},tier={a.tier},enabled={a.enabled})"
            for a in account_store.list_accounts()
        ],
    )
    return account_store, account_service, rotator, env_report


async def _auto_refresh_tiers(
    account_store: AccountStore,
) -> None:
    """启动时校验账号邮箱与订阅等级（并发 4 路、按代理分池探活）。"""

    from aistudio_api.infrastructure.account.cookie_parser import (
        load_cookies_from_auth_file,
        probe_account_benefit_tier,
        probe_account_email,
    )

    now_ts = time.time()
    accounts_to_check = [
        a
        for a in account_store.list_accounts()
        if not a.email
        or (a.tier_checked_at is None)
        or (now_ts - (a.tier_checked_at or 0) > 86400 * 3)
    ]
    if not accounts_to_check:
        return

    logger.info("校验账号邮箱与订阅等级: 待检测=%d", len(accounts_to_check))
    upgraded_count = 0
    checked_count = 0
    sem = asyncio.Semaphore(4)

    # 按代理分池的探活客户端：绑定独立代理的账号各走各的出口。
    # turn/turns/sstp 先解析为本地回环 SOCKS5（桥接子进程冷启动放
    # 线程池），解析失败抛错 → _check_one 捕获后跳过该账号（fail-
    # closed，绝不静默改直连）。池化经 _pooled_probe_client：并发
    # 首调只建一个客户端（历史上检查-构造-赋值间隙的 await 会泄漏）。
    probe_clients: dict[str, httpx.AsyncClient] = {}

    async def _get_probe_client(proxy_url: str | None) -> httpx.AsyncClient:
        return await _pooled_probe_client(probe_clients, proxy_url)

    try:

        async def _check_one(a: AccountMeta) -> None:
            nonlocal checked_count, upgraded_count
            apath = account_store.get_auth_path(a.id)
            if not apath:
                return
            async with sem:
                try:
                    cdict = load_cookies_from_auth_file(apath)
                    if not cdict:
                        return
                    checked_count += 1
                    probe_client = await _get_probe_client(
                        getattr(a, "proxy_url", None)
                    )
                    if not a.email:
                        em = await probe_account_email(
                            cdict,
                            auth_user=a.auth_user or "0",
                            client=probe_client,
                        )
                        if em:
                            account_store.update_account_email(a.id, em)
                    t = await probe_account_benefit_tier(
                        cdict,
                        auth_user=a.auth_user or "0",
                        client=probe_client,
                    )
                    resolved_tier = t or "Free"
                    account_store.update_account_tier(
                        a.id, resolved_tier, checked_at=now_ts
                    )
                    if resolved_tier != "Free" and a.tier == "Free":
                        upgraded_count += 1
                        logger.info(
                            "账号订阅等级更新: id=%s (u/%s) -> %s",
                            a.id,
                            a.auth_user,
                            resolved_tier,
                        )
                except Exception as e:
                    logger.debug("账号 %s 订阅检测失败: %s", a.id, e)

        await asyncio.gather(*(_check_one(a) for a in accounts_to_check))
    finally:
        for probe_client in probe_clients.values():
            with suppress(Exception):
                await probe_client.aclose()

    logger.info(
        "账号校验完成: 已检测=%d, 付费升级=%d",
        checked_count,
        upgraded_count,
    )


async def _cookie_rotation_loop(
    account_store: AccountStore,
    account_service: AccountService,
    rotator: AccountRotator,
    client: AIStudioClient,
) -> None:
    """后台定时轮换账号滚动 Cookie (__Secure-1PSIDTS / SIDCC)，间隔 7 分钟。"""
    import httpx

    from aistudio_api.infrastructure.account.cookie_parser import (
        compute_session_anchor,
        load_cookies_from_auth_file,
        probe_aistudio_session,
    )

    await asyncio.sleep(5.0)
    while True:
        try:
            now_ts = time.time()
            seen_anchors: set[str] = set()
            accounts = account_store.list_accounts()
            # 按代理分池的轮换客户端：账号绑定独立代理时走各自出口
            rot_clients: dict[str, httpx.AsyncClient] = {}
            try:
                for acc in accounts:
                    if not acc.enabled:
                        continue
                    st = rotator.get_account_stats(acc.id)
                    if st and st.session_expired:
                        continue
                    last_rot = acc.last_rotated_at or 0.0
                    if last_rot and (now_ts - last_rot < 420.0):
                        continue
                    apath = account_store.get_auth_path(acc.id)
                    if not apath:
                        continue
                    cdict = load_cookies_from_auth_file(apath)
                    if not cdict:
                        continue
                    anchor = compute_session_anchor(cdict)
                    if anchor:
                        if anchor in seen_anchors:
                            continue
                        seen_anchors.add(anchor)

                    acc_proxy = getattr(acc, "proxy_url", None)
                    client_key = acc_proxy or ""
                    rot_client = rot_clients.get(client_key)
                    if rot_client is None:
                        from aistudio_api.infrastructure.proxy_bridge import (
                            resolve_effective_proxy_async,
                        )

                        rot_client = httpx.AsyncClient(
                            timeout=15.0,
                            proxy=await resolve_effective_proxy_async(acc_proxy),
                            follow_redirects=False,
                        )
                        rot_clients[client_key] = rot_client

                    before_ts = cdict.get("__Secure-1PSIDTS", "")
                    rot_res = await account_service.rotate_account_cookies(
                        acc.id, client.session, client=rot_client
                    )
                    status_code = rot_res.get("status", 0)
                    if status_code == 401:
                        alive, _ = await probe_aistudio_session(
                            cdict, client=rot_client
                        )
                        if not alive:
                            rotator.record_session_expired(acc.id)
                        continue
                    new_cdict = rot_res.get("cookies")
                    after_ts = (
                        new_cdict.get("__Secure-1PSIDTS", "")
                        if isinstance(new_cdict, dict)
                        else ""
                    )
                    if (
                        status_code == 200
                        and before_ts
                        and after_ts
                        and before_ts == after_ts
                    ):
                        alive, _ = await probe_aistudio_session(
                            new_cdict if isinstance(new_cdict, dict) else cdict,
                            client=rot_client,
                        )
                        if not alive:
                            rotator.record_session_expired(acc.id)
            finally:
                for rot_client in rot_clients.values():
                    with suppress(Exception):
                        await rot_client.aclose()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug("后台 Cookie 轮换巡检异常: %s", e)
        await asyncio.sleep(60.0)


async def _env_subaccount_probe(
    account_store: AccountStore, created_ids: list[str]
) -> None:
    """新注入的环境变量账号：后台探活多登子账号 (u/1..u/N)。"""
    from aistudio_api.application.env_account_injector import probe_env_sub_accounts

    try:
        imported = await probe_env_sub_accounts(account_store, created_ids)
        if imported:
            logger.info("环境变量子账号探活导入完成: 新增 %d 个", imported)
    except Exception as e:
        logger.debug("环境变量子账号探活失败: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aistudio_api.infrastructure.waa.daemon import ensure_signer_ready

    ensure_auth_configured()
    from aistudio_api.infrastructure.utils.log_files import start_file_logging

    start_file_logging()
    if settings.request_history:
        from aistudio_api.infrastructure.request_history import request_history

        await asyncio.to_thread(request_history().recover_interrupted)
    # 启动时自动检查并拉起本地 WAA 签名服务（若配置为本地节点）。
    # 内含阻塞 I/O，下放线程池避免卡住启动期事件循环。
    await asyncio.to_thread(ensure_signer_ready)

    client = AIStudioClient()
    runtime_state.client = client

    account_store, account_service, rotator, env_report = await _init_core_services(
        client
    )
    runtime_state.account_service = account_service
    runtime_state.rotator = rotator

    tier_task = asyncio.create_task(_auto_refresh_tiers(account_store))
    rotate_task = asyncio.create_task(
        _cookie_rotation_loop(account_store, account_service, rotator, client)
    )

    # 模型目录（含思考档位/付费状态元数据）后台周期刷新：启动即预热，
    # 此后每 60s 唤醒一次（真实拉网频率由 get_models 内部 600s TTL 控制，
    # 失败按内部退避 ≤60s 在下一 tick 重试）。此前刷新是"被动周期"——
    # 仅当 /v1beta/models、/v1/models 或管理端模型列表被请求时触发，
    # 从不访问模型列表端点的客户端（模型名写死）会让聊天路径长期读到
    # 静态保底快照，目录档位迁移（如 2026-11-15 hydrogen 5→6）随之漏过。
    model_discovery.start_background_refresh()

    # 新注入的环境变量账号：后台探活多登子账号 (u/1..u/N)
    env_probe_task = None
    if env_report.created_ids:
        env_probe_task = asyncio.create_task(
            _env_subaccount_probe(account_store, list(env_report.created_ids))
        )

    # 出站保活（AISTUDIO_KEEPALIVE_URL，未配置时不启动、零开销，
    # 见 infrastructure/outbound_keepalive.py）。
    from aistudio_api.infrastructure.outbound_keepalive import (
        outbound_keepalive_service,
    )

    try:
        await outbound_keepalive_service.start()
    except Exception as e:
        logger.warning("保活服务启动失败（不影响主服务）: %s - %s", type(e).__name__, e)

    try:
        yield
    finally:
        await _graceful_shutdown(
            tier_task,
            rotate_task,
            env_probe_task,
            client,
        )


async def _graceful_shutdown(
    tier_task: asyncio.Task[None],
    rotate_task: asyncio.Task[None],
    env_probe_task: asyncio.Task[None] | None,
    client: AIStudioClient,
) -> None:
    logger.info("正在关闭服务")
    bg_tasks = [t for t in (tier_task, rotate_task, env_probe_task) if t]
    for bg_task in bg_tasks:
        if not bg_task.done():
            bg_task.cancel()
    # 等待取消真正落地，避免残留 pending task 在事件循环关闭时告警
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    # 停止出站保活任务（幂等；未启动时为空操作）
    from aistudio_api.infrastructure.outbound_keepalive import (
        outbound_keepalive_service,
    )

    await outbound_keepalive_service.stop()
    # 停止模型目录后台刷新（跨循环调用时仅清理句柄，内部已防挂死）
    await model_discovery.stop_background_refresh()
    if client:
        try:
            await client.close()
        except Exception as e:
            logger.debug("关闭客户端异常: %s", e)
    from aistudio_api.infrastructure.waa.daemon import stop_signer_daemon

    await asyncio.to_thread(stop_signer_daemon)
    # 终止全部 TURN/SSTP 桥接子进程（fail-closed 组件随主进程同生命周期）
    from aistudio_api.infrastructure.proxy_bridge import shutdown_bridge_processes

    await asyncio.to_thread(shutdown_bridge_processes)
    # 统计与调度状态都改成了防抖写入（record 后 2s 才落盘）；若进程在最后一次
    # 记账后 2s 内退出，最后一次增量会丢失。关闭时显式刷新一次内存态。
    try:
        pending_save = runtime_state._save_task
        # 只 await 仍在运行的防抖任务：已取消/已完成的残留任务（如测试
        # 事件循环销毁时被取消的旧任务）会把 CancelledError 抛进优雅关停
        # 流程；无论如何后面都会同步补一次快照，这里跳过即可。
        if pending_save is not None and not pending_save.done():
            await pending_save
        await asyncio.to_thread(runtime_state.save_stats)
        pending_rotator = runtime_state.rotator
        if pending_rotator is not None:
            await asyncio.to_thread(pending_rotator.save_state)
    except Exception as e:
        logger.debug("关闭时刷新统计/调度状态失败: %s", e)
    from aistudio_api.infrastructure.utils.log_files import stop_file_logging

    await asyncio.to_thread(stop_file_logging)
    runtime_state.client = None
    runtime_state.account_service = None
    runtime_state.rotator = None
'''

text = text.replace(A2_OLD, A2_NEW, 1)

SRC.write_text(text, encoding="utf-8")
print("split_lifespan.py: all anchors replaced OK")
sys.exit(0)
