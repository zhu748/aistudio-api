# aistudio-api 代码审查与修复报告

## 已执行的修复（已提交推送，commit 6679723）

1. **死代码清理**（全部经全仓 grep 验证零引用）：
   - `api_service_gemini.py`：format_sse_event / format_sse_usage / format_sse_error / format_sse_error_text 四个 SSE 包装（约 55 行；生产路径直接用内部 `_event_payload`/`_encode_sse`）
   - `gateway/client.py`：`AIStudioClient.warmup`
   - `gateway/session.py` + `waa/signer.py`：两处 `generate_snapshot_with_headers`（生产走 `generate_snapshot` + `last_signer_headers`）
2. **account_store 样板收敛**：8 个单字段更新方法（update_account/email/proxy/tier、toggle_enabled、group、notes、batch_toggle）中 email/proxy/tier 收敛到 `_update_account_fields` + `_assign_if_changed` 共享骨架；toggle/group/notes 因"恒写盘"语义保留独立实现但注释统一。严格保持三态返回语义（未找到=None / 未变化不写盘=False / 已写盘=True），test_cookie_import 断言原样通过
3. **lifespan 关停缺陷修复**：`await runtime_state._save_task` 在已取消残留任务上抛 CancelledError 中断优雅关停 → 改为只 await 未完成任务
4. 上轮保活移植（outbound_keepalive 模块 + 37 测试）一并入库

验证：ruff 全过 / pyright 0 errors / pytest 1448 passed + 5 skipped。

## 审查发现但本轮未执行的改进（建议后续批次）

### 高优先（重复收敛）
- routes_system.py：config.yaml 读-改-写样板五连抄（update_config_yaml/create_api_key/delete_api_key/update_api_key_name + routes_operations.update_global_proxy）→ 建议在 model_defaults 提供 `update_config_yaml_document(mutator)` 上下文管理器
- routes_accounts.py：删除后顺延切换活跃账号 16 行块×2（346-359 vs 390-403）→ 提取 `_failover_after_delete`；rotator 注册五连抄 → 封装 `register_account`
- compat_service.py：三个流适配器（openai/claude/responses）的 aclose/异常记账/错误帧骨架三重复 → 提取 `_adapter_lifecycle`；TaskGroup 并发下载×3、file/input_file 块×2、`_iter_response_parts`×4
- transport.py：send_request vs send_streaming_request 前奏逐字重复 → 提取 `_open_wire_request`
- api_service_gemini.py：status_map dict 两次逐字重复（345-355 vs 364-374）→ 模块级常量
- model_defaults.py：mtime 缓存样板 6 处、yaml 读改写 5 处
- wire_parser.py 协议槽位常量与 wire_spec.PartIndex 双轨

### 中优先（分类/可读性）
- routes_system.py（1519 行八职责）拆 routes_config/routes_signer/routes_logs；routes_accounts（1038 行）拆导入端点；compat_service（2303 行五关注点）拆 shared/openai/claude/errors；chat_service 前半 schema wire 编码器独立成模块
- 超长函数拆分：request_middleware.__call__（428 行 14 nonlocal → _ResponseObserver 状态对象 + _persist 15 参数改 dataclass）、app.py lifespan（334 行 → 拆 _init_core_services/_auto_refresh_tiers/_cookie_rotation_loop/_graceful_shutdown）、chat_service.normalize_gemini_request（450 行 → 4 个 _normalize_* 辅助）、_build_gemini_streaming_response（389 行双层重试）
- `_claude_stop_reason` 的 full_text/stop_sequences 死参数（两处调用均传空）
- `AccountStats` 四组 requests/success/rate_limited/errors property 别名确认无外部消费者可删（get_all_stats 走 to_dict 的 total_* 键）
- model_defaults `_default_config`（233 行内嵌 dict）与 defaults/config.yaml 已实测漂移 → 改为从模板文件加载

### 低优先（微修复）
- 局部重复 import（routes_accounts secrets×4、app.py httpx、anti_truncation re 等）
- 代理脱敏正则×3 处 → utils/redaction.mask_proxy_url
- `_EXTRA_GUARANTEED_MODELS` 与 fallback 目录条目重复（3 id 交集）
- 404 文案 12 处两种写法统一；logger/log 命名统一

## 代码分类现状结论
- 分层总体清晰（api → application → infrastructure/domain）；主要债务是"文件职责过宽"与"样板复制"两类
- 无浏览器反代逻辑、并发注释密度高是亮点；建议大重构按上述清单分批做、每批全量 pytest 守护

## 路线图执行完成记录（2026-11-17 第二批）

上一轮报告中的全部未执行项已落地，最终验证：ruff All checks passed / pyright 0 errors / pytest 1449 passed + 5 skipped。

- **routes_system.py 拆分**（1519 → 261 行）：config.yaml/抗截断/API Key → `routes_config.py`（570 行）；WAA 签名器管理 → `routes_signer.py`（448 行）；日志与 SSE → `routes_logs.py`（271 行）；`update_global_proxy` 自 routes_operations 迁入 routes_config。URL 与鉴权零变更（ASGITransport 路由测试兜底）。
- **yaml 读改写收敛**：`model_defaults.update_config_yaml_document(mutator, *, private=False)` 持锁读改写助手，收敛内部 5 处 + 路由层 4 处（create/delete/rename API Key、update_global_proxy 保 0600）。
- **chat_service.py 拆分**（1834 → 1162 行）：schema wire 编码器 → 新 `schema_wire_codec.py`（778 行，重导出保持 import 路径）；`normalize_gemini_request` 450 → 55 行编排 + 4 域辅助（contents/system/tools/overrides）+ 2 内部辅助；`_THINKING_OFF_LABELS` 词表常量化。
- **compat_service 收敛**（净减 44 行）：流适配器生命周期 `_stream_adapter_lifecycle` 统一三处；TaskGroup 并发 `_convert_items_concurrently`（MEDIA_CONCURRENCY=4）；`_file_block_to_part`、`_claude_usage_payload`、`_claude_base64_part` 提取；`_claude_stop_reason` 死参数删除；`_close_stream_quietly` 收敛到 anti_truncation。
- **transport 前奏收敛**：`_prepare_wire_exchange` 统一 send_request / send_streaming_request 的鉴权头重建 + 线缆头序 + 代理判定前奏。
- **小批次**：代理脱敏 `redaction.mask_proxy_url()` 三处收敛；account_rotator `_coerce_int/_coerce_float` 提升 + AccountStats 别名删除；status_map 常量化；routes_accounts `_failover_after_delete` / `_register_rotator_account` 收敛 + secrets/logger 规范化。

测试同步调整：test_config_writes / test_log_stream / test_round6_audit_fixes / test_env_api_keys 的 patch 与 import 目标迁移至新模块（test_env_cookie_injection 的 healthz 留 routes_system）。

## 路线图执行完成记录（2026-11-17 第三批）

中优先超长函数拆分与 model_defaults 两项全部落地，微修批部分落地。最终验证：ruff All checks passed / pyright 0 errors / pytest 1449 passed + 5 skipped。

- **request_middleware.py**：`__call__` 428 → 407 行，新增 `_ResponseObserver` dataclass（16 字段）清零 14 个 nonlocal；`_persist` 18 → 9 参数（observer 内字段不再直传）。AST 级比对确认编排逻辑逐 token 等价。
- **app.py**：lifespan 334 → 64 行编排，拆出 `_init_core_services` / `_auto_refresh_tiers` / `_cookie_rotation_loop` / `_env_subaccount_probe` / `_graceful_shutdown`；关停 14 个动作顺序脚本化比对严格一致（含 6679723 的 save_task CancelledError 兜底）。
- **api_service_gemini.py**：`stream_response` ~380 → 140 行编排 + 14 个模块级辅助（`_StreamAttemptState`、`_stream_attempt_frames`、`_retry_stream_attempt` 等）；yield 顺序与双层重试守卫零变化。
- **model_defaults.py**：mtime 样板 7 处收敛为 `_cached_mtime` / `_cache_fresh`；`_default_config` 233 行内嵌 dict 改为模板加载 `defaults/config.yaml`（懒加载 + deepcopy + 缺文件兜底），按"内嵌 dict 为真相源"修复 6 处实测漂移（video_models.match、paid_model_rules 两处、tier_quotas 三处 carbon、waa.servers 文案），yaml 注释全保留。
- **微修批**：app.py 函数内重复 `import httpx` 清理；`_iter_response_parts` 两份经归一化比对语义不同（签名缓存行为差异），保留各自实现；404 文案、`_EXTRA_GUARANTEED_MODELS` 去重两项经核实（测试断言措辞/嵌套作用域风险）不值得改，维持现状。

剩余未执行（均为低优先微修，收益低于风险）：routes_accounts 拆导入端点、compat_service 五关注点文件级拆分、wire_parser PartIndex 双轨统一、logger/log 命名统一。至此路线图高/中优先项全部清零。

## 路线图执行完成记录（2026-11-17 第四批，收尾批）

上批遗留的低优先项全部落地或核销。最终验证（CI 同位口径）：ruff check . / ruff format --check . 全过、pyright src tests 0 errors、全量 pytest 1449 passed + 5 skipped。

- **CI 红灯修复**：上批 c9174f6 将拆分脚本入库但带 21 个 lint 错误（本地验证口径是 `src` 而 CI 是全仓 `.`，盲区）→ ruff --fix + format 修复，本地补齐 CI 同位四连校验。
- **routes_accounts.py 拆分**（1038 → 575 行）：Cookie 导入端点族（import-cookies / probe-import / import-bundle 三端点 + 7 个请求/响应模型）拆出 `routes_account_imports.py`（493 行）；app.py 同款双挂载，URL 零变化；`log` → `logger` 命名统一。
- **compat_service.py 五关注点拆分**（2311 → 100 行聚合门面）：共享层 `compat_shared.py`（486 行：媒体/签名/并发/SSE 帧解析 + resolve_compat_model）、`compat_errors.py`（214 行：双侧错误类型映射 + 错误 payload 构造）、`compat_openai.py`（826 行：请求转换 + completion 构建 + 流适配器）、`compat_claude.py`（855 行：Claude 全族）；门面单向 re-export，routes/responses_service/测试 import 路径零变化；openai/claude 互不依赖。
- **wire_parser PartIndex 双轨统一**：13 个 `WIRE_PART_*_INDEX` 模块级常量改为 `wire_spec.PartIndex` 取值别名（单一真相源，wire_codec import 面零改动）。
- **logger/log 命名统一**：session.py 收尾（`log = get_logger("session")` → `logger`）。
- **维持现状**（上批已注明理由）：404 文案统一（测试断言既有措辞）、`_EXTRA_GUARANTEED_MODELS` 去重（嵌套作用域风险大于收益）。

至此审查报告路线图全部条目执行完毕（执行 / 核销均有记录）。

## 深审执行记录（2026-11-23 第六轮：六代理全模块深审 + 全面优化）

六个并行审查代理覆盖 Gemini 管线 / 兼容层 / 网关传输 / API 层与状态 / Go waa-signer / proxybridge+前端，发现 40+ 项，逐项人工验证后实施（误报与设计分歧项核销）。最终验证：ruff 全过、pyright 0 errors、pytest 1481 passed + 5 skipped（新增 14 项回归）、前端 lint/type-check/build 全过。

- **安全（P2×2）**：GET /config 掩码 `network.proxy_url` 内嵌凭据（原先任意 API Key 持有者可读全局代理账密）；API Key 回退会话写保护清单扩宽（`/api/proxies`、`/api/logs`、`/api/requests`、`/accounts`、`/rotation` 写操作 403，SSE 票据与 detect 探测例外放行）；routes_signer switch/reset 响应与 GET 同口径掩码。
- **正确性**：countTokens 全空轮 ValueError→400（原先穿透 500）+ systemInstruction 计入统计；pending_signature 不再覆盖 functionCall/图片 part 自带签名（流式签名回传一致性）；204 异常归一 502（RFC 9110 禁 body）；schema shape 降级链二次兜底（深嵌套超限降级为 object wire + description 附原文，不再 400）；stream_parser 闭合 chunk 非法 JSON 回退修复路径（中间层注入裸控制符不再整流失败）；Claude stop_sequences 空串过滤（与 OpenAI 侧一致）；OpenAI/Claude 适配器过滤上游错误兜底文本（不再混入 assistant 正文）；Responses 错误事件携带语义化 code（429→rate_limit_error 等，原硬编码 server_error）；日志历史缺 ts_ms 行排序兜底。
- **性能（P1×1 + P3×5）**：签名缓存 remember 落盘去抖（2s 窗口 + 脏标记 + atexit 兜底 + compact 序列化，原先每个 tool_calls 帧全量重写 MB 级文件阻塞事件循环）；SSE 五处 json.dumps 补 separators（Gemini/OpenAI/Claude/Responses 每帧省 2-4% 字节）；StopSequenceBuffer.feed 单次 find；快照落盘门禁前置 should_save_exchange（辅助 RPC 成功不再空跑 to_thread）；request_history query/options 只读去写锁（大搜索不再阻塞 insert）；session 无凭据降级态 5s 负缓存（消除每请求全量重转 + INFO 刷屏）。
- **健壮性**：流式帧层包装生成器确定性 aclose；Cookie 后台持久化任务异常兜底日志；AccountStore 单例 __init__ 纳入锁（并发首次构造竞态）；import 白名单补 AISTUDIO_DATA_DIR；三条导入/探活路径统一传账号绑定代理。
- **Go（waa-signer 7 项 + proxybridge 2 项，无 Go 工具链环境，字节级补丁 + 逐文件静态自查）**：指纹文件原子写（atomicwrite 共享包）；handleOf 错误路径补 release（根表句柄泄漏）；Close 补 CloseIdleConnections；mozjs 非法 callee 前置守卫（UB）；QUIC 会话缓存 nil 防御 + 票据长度下界；trailer QPACK 解码改请求 ctx（永久挂起）；池淘汰排除 WorkerBusy；SOCKS 拒绝路径方法选择 0xFF；firstByte 回收窗口 10min→3min。
- **前端（6 项）**：TokenSettingsModal 改造为会话管理（原手改令牌输入会自锁面板）；LogsView SSE 批量 flush + 级别防抖 + 后台冻结；VisualConfigEditor computed 去副作用；fetchRotation 序列守卫；AppLayout 首帧重复请求消除；api/accounts.ts 路径参数编码补齐。
- **核销（误报/有意设计）**：非流式 MAX_TOKENS 丢弃工具调用——有专门回归测试守护的保守设计（截断的并行调用集合可能不完整，有意与流式不对称），撤回改动并补注释；`except* ValueError` 吞异常——语义上非 ValueError 子组会继续传播，非吞；routes_accounts 探活未传代理——实际已传（仅 import 路径缺失）；signature 帧缺 text:""——风险大于收益，维持现状。
- **建议后续（需实测环境）**：Go 侧在有工具链环境跑 `go build ./... && go vet ./... && go test ./...`；waa-signer 池锁 HOL（冷启动持 entry.mu 5-8s）与 HTTP/3 0-RTT 双 accept 循环两项需真实多账号环境验证后重构。
