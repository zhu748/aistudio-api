# ListModels 目录层级语义实测（2026-11-24）

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
`MakerSuiteService/ListModels`，对两份目录做逐字段对比（35 个模型 × 全部解析槽位）。

## 结论

1. **两份目录完全一致**：35 个模型 id 集合无差、`paid`/`accessModes`/
   token 上限/思考档/别名/能力码全部相同。**paid 与 accessModes 是模型固有
   门槛，与抓取账号的订阅等级无关**（Free 账号也能看到 neon 的 paid=True、
   accessModes=[1,3]——目录不是可用性视图，是全局目录）。
   → 据此移除了"视角降级"防御逻辑（热路径零收益开销）。
   `AccountService` 侧的"切号即失效目录缓存"保留：拉取凭据（Cookie/
   authuser）确实随活跃账号切换。
2. 目录拉取的 tier 头（`X-AIStudio-G1-Tier`）不影响 ListModels 返回内容
   （实验中 Free 头与 Pro 头各拉一次，返回一致）。
3. 顺带核对的静态快照漂移（`_FALLBACK_MODELS`，冷启动兜底目录）：
   - 6 个 live 模型已从线上目录下架：gemini-3.8-live、
     gemini-3.8-live-extended-thinking、gemini-3.1-flash-live-preview、
     gemini-3.5-transcribe-live、gemini-3.5-live-translate-preview、
     gemini-robotics-er-2-streaming-preview → 快照同步删除。
   - **gemini-3.1-flash-lite 线上已标 accessModes=[1,3]（Pro+ 门槛）**，
     原快照无门槛标记（冷启动会漏放行 Free 请求）→ 已补（目录同时未标
     paid[77]，快照同步为 paid=False + modes=[1,3]，门控以 modes 为准）。
   - gemini-4-neon 输出上限 32768→65536、默认思考档 2→3；思考档目录回报
     [1,2,3,5]，与 hydrogen 系 wire 真机实测的最高档 6 冲突——**以 wire
     实测为准**（发 6 → 200、发 5 → 400 "EXTRA_HIGH is not supported"，
     目录口径滞后），neon/hydrogen 快照均用 [1,2,3,6]。
   - gemini-3-flash-preview 新增别名 gemini-3.5-flash。
   - omni-1.1/1.2 目录 [80] 视频标记清零、3.5-transcribe [81] 音频标记
     清零、3.8-flash 新增 accessModes=[1]（Paid API key 通道标记，无门槛语义）。

## 复现

```bash
# 探活（邮箱 + GetAiStudioBenefitTier 定层级）
uv run python /home/z/my-project/scripts/probe_tiers.py
# 同 Cookie 分别以 Free / Pro authuser 拉 ListModels 并逐字段对比
uv run python /home/z/my-project/scripts/compare_catalog.py
```

（脚本读取 `AISTUDIO_PROBE_PROXY` / `AISTUDIO_PROBE_RAW` 环境变量；快照
输出到 `/tmp/aistudio_probe/catalog_{free,pro}.json`。）

## 对门控的影响

- 付费门控（`is_paid_model` / `get_required_tiers` / `is_model_allowed_for_tier`）
  的数据源优先级不变：运行时目录（600s TTL）→ 静态快照 → config.yaml 规则。
  实验确认第一步的结论对任意抓取账号都成立，无需视角修正。
- 冷启动窗口（首次拉网前）依赖静态快照，本次同步消除了 flash-lite 的
  门槛漏放行与下架模型残留。
