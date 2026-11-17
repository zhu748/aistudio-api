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
