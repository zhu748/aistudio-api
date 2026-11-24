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

## 未决：hydrogen/neon 思考档 5 vs 6（两套口径冲突）

- 2026-11-24 两次拉取（间隔数小时）：线上目录 `thinkingLevels=[1,2,3,5]`。
- 2026-11-15 Reqable 真机抓包：wire 发 `[1,null,null,6]` → 200（双样本）；
  发 5 → 400 "Thinking level EXTRA_HIGH is not supported"。
- 槽位 [47]（`[[5,1],[1,6],[5,6],[5,3],[6,3]]` 形状的表）经跨模型对比
  排除"wire↔目录档位翻译表"假设：不同 levels 的模型共享同一张 47 表，
  nano-banana 的 47 表含不在其 levels 里的 7——它是 UI 档位选择器的选项
  矩阵，不是函数映射。
- 当日尝试真机裁决（用 RemoteProofSigner 铸 proof 直发 5/6 档）：远程签名
  服务对沙箱不可用（直连 403、经代理 502），本地 Go 签名器需 go 工具链 +
  SpiderMonkey 依赖包（沙箱无 go）。**无法当场重放请求裁定。**
- 处置：快照维持 **[1,2,3,6]**（wire 真机 200/400 双向证据 > 目录元数据，
  且 snap 吸附方向正确：目录值 [1,2,3,5] 会把 EXTRA_HIGH(6) 用户请求降档
  到 5——若上游真不支持 5 则直接 400）。运行时目录仍以线上数据为准
  （600s TTL 自动刷新，上游改口径会自然收敛）。下次真机抓包可用脚本
  `/home/z/my-project/scripts/test_thinking_levels.py` 直接复核。

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
