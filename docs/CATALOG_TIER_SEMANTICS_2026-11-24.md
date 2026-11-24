# ListModels 目录层级语义实测（2026-11-24，同日复核修订）

## 背景

`ModelDiscoveryService` 用上游 ListModels 的 `paid`（protobuf 槽位 [77]）与
`accessModes`（槽位 [82,87]）做付费/订阅等级门控。此前仓库内快照均以单一
Free 账号实测，**两个槽位到底是"模型固有门槛"还是"相对抓取账号的可用性
视图"未做跨账号验证**——2026-11-24 曾据此加过"目录抓取视角与请求者层级
不一致时降级到静态快照"的防御逻辑。

## 实验方法

同一 Cookie 组（4 个 authuser 子账号）经 SOCKS5 代理出网：

| authuser | 探测邮箱 | GetAiStudioBenefitTier |
|---|---|---|
| u/0 | nqoopani@gmail.com | Free |
| u/1 | crbe5vdv@gmail.com | **Pro** |
| u/2 | tonyahfielderz9@gmail.com | Free |
| u/3 | robellapaukstis597@gmail.com | Free |

选 u/0（Free）与 u/1（Pro），各自携带 `X-Goog-AuthUser` 头调用
`MakerSuiteService/ListModels`，对两份目录做逐字段对比。同日复核时又拉取了
**未经解析器的原始响应**（43 条原始条目 × 全部非空槽位）做二次核对。

## 结论

1. **两份目录完全一致**：`paid`/`accessModes`/token 上限/思考档/别名/能力码
   全部字段零差异（复核覆盖全字段，不只 paid/accessModes）。**paid 与
   accessModes 是模型固有门槛，与抓取账号的订阅等级无关**（Free 账号也能
   看到 neon 的 paid=True、accessModes=[1,3]——目录不是可用性视图，是全局
   目录）。
   → 据此移除了"视角降级"防御逻辑（热路径零收益开销）。
   `AccountService` 侧的"切号即失效目录缓存"保留：拉取凭据（Cookie/
   authuser）确实随活跃账号切换。
2. 目录拉取的 tier 头（`X-AIStudio-G1-Tier`）不影响 ListModels 返回内容
   （实验中 Free 头与 Pro 头各拉一次，返回一致）。
3. **原始响应 43 条 vs 解析后 35 条的差额是解析器过滤，不是下架**（当日
   复核修正——初版误判为"6 个 live 模型已下架并删了快照"）：8 条
   `bidiGenerateContent`（Live 双向流）/`createDecision`（决策模型）专用
   模型（gemini-3.8-live 系 6 条 + gemini-decision-1 + genie-4）被
   `_parse_raw_models` 的 generateContent 过滤条件刻意排除——它们仍在线上
   目录里。快照已恢复收录（与线上原始条目一致），仅不出现在 get_models
   列表。
4. 静态快照同步项（`_FALLBACK_MODELS`，冷启动兜底目录）：
   - **gemini-3.1-flash-lite 线上已标 accessModes=[1,3]（Pro+ 门槛）**，
     原快照无门槛标记（冷启动会漏放行 Free 请求）→ 已补（目录同时未标
     paid[77]，快照同步为 paid=False + modes=[1,3]，门控以 modes 为准）。
   - gemini-4-neon 输出上限 32768→65536、默认思考档 2→3。
   - gemini-3-flash-preview 新增别名 gemini-3.5-flash。
   - omni-1.1/1.2 目录 [80] 视频标记清零、3.5-transcribe [81] 音频标记
     清零、3.8-flash 新增 accessModes=[1]（Paid API key 通道标记，无门槛语义）。

## 思考档 5 vs 6：目录显示枚举 vs wire 发送值（2026-11-24 真机定谳）

- 2026-11-24 两次拉取（间隔数小时）：线上目录 `thinkingLevels=[1,2,3,5]`。
- 2026-11-15 Reqable 真机抓包：wire 发 `[1,null,null,6]` → 200（双样本）；
  发 5 → 400 "Thinking level EXTRA_HIGH is not supported"。
- **2026-11-24 21:27 Reqable 真机抓包（用户上传，150 条 HAR）**：UI 四档位
  xhigh / high / medium / low 对 hydrogen 实发 wire `[1,null,null,6/3/2/1]`。
  xhigh 的 6 → 503、high 的 3 → 503、medium/low 的 2/1 → 429——全是容量/
  限流类错误（已过参数校验），当日无 200 是上游过载所致，与档位合法性
  无关；若 6 非法会像 11-15 的 5 那样报 400 参数错误。
- **定谳**：目录的 `[1,2,3,5]` 是显示层枚举口径（"5" 对应 UI 的 Extra
  High），UI 实际发送值是 6；wire 层只认 6、拒绝 5。目录拉取本身无误，
  但**不能拿目录 thinkingLevels 直接当 wire 值发**——snap 吸附与请求编码
  以 wire 口径 [1,2,3,6] 为准。
- 槽位 [47]（`[[5,1],[1,6],[5,6],[5,3],[6,3]]` 形状的表）经跨模型对比
  排除"wire↔目录档位翻译表"假设：不同 levels 的模型共享同一张 47 表，
  nano-banana 的 47 表含不在其 levels 里的 7——它是 UI 档位选择器的选项
  矩阵，不是函数映射。
- 处置：快照维持 **[1,2,3,6]**（11-15 的 200/400 双向证据 + 11-24 的 UI
  实发 6 复核）。运行时目录仍以线上数据为准（600s TTL 自动刷新）。复核
  脚本：`/home/z/my-project/scripts/test_thinking_levels.py`（签名服务可用
  时直发 5/6 裁决）、`/home/z/my-project/scripts/parse_capture_1124.py`
  （解析 Reqable HAR 提取 UI 档位→wire 映射）。
- **运行时归一化（2026-11-24 修复）**：cookie 拉取本身无误，但目录值若
  直接入缓存，snap 吸附会把用户请求的 EXTRA_HIGH(6) 降档到 VERY_HIGH(5)
  → wire 400（线上拉取覆盖缓存后必现的活隐患，此前仅静态快照是 wire
  口径）。已在缓存唯一入口 `_merge_extra_models` 统一做「显示枚举 →
  wire 枚举」翻译（thinkingLevels / defaultThinkingLevel 中 5 → 6，仅当
  6 不存在，幂等），线上拉取与静态保底两条路径都经过此处。回归测试：
  `test_merge_translates_catalog_display_levels_to_wire`、
  `test_snap_keeps_extra_high_after_live_catalog_ingest`。

## 复现

```bash
# 探活（邮箱 + GetAiStudioBenefitTier 定层级）
uv run python /home/z/my-project/scripts/probe_tiers.py
# 同 Cookie 分别以 Free / Pro authuser 拉 ListModels 并逐字段对比
uv run python /home/z/my-project/scripts/compare_catalog.py
# 原始响应（不解析）拉取 + 43 条原始槽位 dump + Free/Pro 全字段 diff
uv run python /home/z/my-project/scripts/recheck_catalog.py
```

（脚本读取 `AISTUDIO_PROBE_PROXY` / `AISTUDIO_PROBE_RAW` 环境变量；快照
输出到 `/tmp/aistudio_probe/catalog_{free,pro}.json` 与
`catalog_raw_u{0,1}.json`。）

## 对门控的影响

- 付费门控（`is_paid_model` / `get_required_tiers` / `is_model_allowed_for_tier`）
  的数据源优先级不变：运行时目录（600s TTL）→ 静态快照 → config.yaml 规则。
  实验确认第一步的结论对任意抓取账号都成立，无需视角修正。
- 冷启动窗口（首次拉网前）依赖静态快照，本次同步消除了 flash-lite 的
  门槛漏放行；Live 系快照恢复后与线上原始目录保持一致。
