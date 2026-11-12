# 2026-11 真机抓包对齐与 WAA 行为逆向实录 (CAPTURE_ALIGNMENT_2026-11.md)

本文档记录基于一份 Android Chrome 130 真机抓包（Reqable HAR，235 条 entry，含 46 条
MakerSuiteService RPC、8 条 aistudio.google.com 导航与 1 条 Waa/Create）与 SOCKS5 代理实测
得到的对齐结论与协议行为发现。所有结论均有可复现实验支撑。

---

## 1. 抓包概况

| 维度 | 值 |
|---|---|
| 主机 | `alkalimakersuite-pa.clients6.google.com`（46 条 RPC） |
| 签名服务 | `waa-pa.clients6.google.com`（Waa/Create 1 条） |
| 鉴权形态 | 三重 `SAPISIDHASH / SAPISID1PHASH / SAPISID3PHASH` + Cookie + `X-Goog-Api-Key` |
| API Key 分离 | MakerSuite 用 `AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs`；**Waa/Create 用另一把** `AIzaSyBGb5fGAyC-pRcRU6MUHb__b_vKha71HRE` |
| 模型快照 | `gemini-3.8-flash`、`gemini-3.5-flash-lite`（动态模型命名，与 ListModels 一致） |
| Visit-Id | 同页面会话内恒定复用（`v1_` + base64(UUID)，48 字符），7/7 条 GenerateContent 全带 |

抓包中出现的 RPC：GenerateContent / CountTokens / ListPrompts / CreatePrompt / UpdatePrompt /
ListApplets / ListRecentApplets / CheckUserStatus / GetAppFolder / GenerateTitle。

## 2. 请求头消融矩阵（CountTokens 实测，代理出口）

| 头 | 移除后行为 | 结论 |
|---|---|---|
| `X-Goog-Api-Key` | 403 unregistered callers | **必需** |
| `Authorization` (SAPISIDHASH) | 401 API keys not supported | **必需** |
| `Cookie` | 401 missing credential | **必需**（仅 Auth 不够，双向校验） |
| `Origin` | 401 invalid credentials | **必需**（参与 SAPISIDHASH 计算并被服务端复核） |
| `Content-Type: application/json+protobuf` | 400 HTML 错误页 | **必需** |
| `X-User-Agent` | 200 | 可选 |
| `X-Goog-Ext-519733851-bin` | 200 | 可选（保留以维持指纹一致） |
| `Sec-Fetch-*` / `Accept-Language` / `User-Agent` | 200 | 可选 |
| `X-AIStudio-G1-Tier` | —（Free 不发；Pro→TIER1 与抓包一致） | 按账号层级 |

三重 SAPISIDHASH 中仅 `SAPISIDHASH` 单独出现即可通过（200），1P/3P 哈希为冗余。

## 3. WAA Proof（snapshot 字段）行为——本文档最重要的结论

### 3.1 浏览器级 attestation（真机抓包 token）

- **可长期重放**：抓包 token 在 3 天后重放同 prompt 仍 200；
- **与 contents 的 SHA-256 摘要严格绑定**：同 token 换 prompt / 空 prompt 立即 403
  `The caller does not have permission`；
- 无 snapshot 的 GenerateContent 一律 403（该错误码也是无 proof 的标准回包）。

### 3.2 远程签名服务（spinsnow / aistudio-pg-bridge，NodeVM 类）

- **单次使用现象**：同 proof 第二次请求即 403（EXACT-REPLICA 实验）；
- **mint 质量概率抖动**：新 mint 的 proof 偶发首次即 403，重试（重新 mint）后恢复——
  3×3 交叉实验（visit-id ON/OFF）出现 `ON=[200,403,403]`、`OFF=[200,200,200]`，
  证明 403 与 visit-id / UA 无确定性绑定，而是签名服务状态抖动；
- 因此 WaaSigner 的 proof 复用缓存 **默认关闭**（`AISTUDIO_WAA_PROOF_CACHE_TTL=0`），
  仅本地 SpiderMonkey（浏览器级）签名场景建议实测后开启。

### 3.3 与 proof 无因果关系的头（实验排除项）

`X-AIStudio-Visit-Id`（会话级复用值）、`User-Agent`（Chrome/Firefox 均验证）、
`X-Client-Data`、`sec-ch-ua-*` 系列——均非 403 的决定因素。项目保留 visit-id 发送
是**指纹对齐**目的（真实浏览器每次 GenerateContent 都带、且会话内复用），而非鉴权需要。

## 4. CountTokens 端点（本次新增实现）

```
POST /$rpc/.../MakerSuiteService/CountTokens
body:  ["models/gemini-3.5-flash-lite", [[[[null,"嘿"]],"user"]]]
resp:  [2,[],[[[1],1]],null,null,[[1,2]]]
```

- **无需 WAA snapshot**（无 proof 直接 200）；
- 响应首位 `[0]` 即 AI Studio 界面展示的 token 总数（含 1 个固定开销 token）；
- `[5]` 为 `[[1, total]]`、`[2]` 为 `[[[total-1], 1]]`（明细，未用于网关层）；
- 鉴权链与 GenerateContent 完全一致。项目网关已暴露
  `POST /v1beta/{model}:countTokens` 转发该端点（2026-11 新增）。

## 5. 其他端点结构速查（来自抓包，供后续扩展参考）

| RPC | 请求体 | 响应要点 |
|---|---|---|
| `CheckUserStatus` | `[]` | `[1]` |
| `GetAppFolder` | `[]` | `["<Drive folder id>"]` |
| `ListApplets` | `[[1000],[[]],0]` | applet 列表（含 name/描述/时间戳） |
| `ListRecentApplets` | `[]` | 最近 applet |
| `ListPrompts` | `[100]` | 保存的 prompt 列表（Drive prompt id + 元数据） |
| `CreatePrompt` | `[[null,null,null,<prompt 对象>]]` | `["prompts/<id>", ...]` |
| `GenerateTitle` | `[user_prompt, model_response, snapshot]` | `["<标题>"]`（也带 WAA snapshot） |
| `Waa/Create` | `["lmnUSbltwc5ULv48iKLX"]` | `[null,"<签名 attestation base64>"]`（独立 API Key） |

## 6. 2026-11 对齐与优化清单（已实施）

1. **`X-AIStudio-Visit-Id` 会话级复用**（`get_visit_id(scope)`）：真实浏览器一个页面会话
   复用同一 visit-id；原先每次随机生成构成指纹偏离，现在按账号作用域稳定复用，且
   GenerateContent 请求头必带（对齐 7/7 抓包）。
2. **`POST /v1beta/{model}:countTokens` 真实转发**：无需 WAA，走标准鉴权链。
3. **WAA proof 复用缓存基础设施**（默认关闭，403 重签路径自动失效）：仅浏览器级
   attestation 场景按需开启。
4. **403 阶梯重试第 3 跳失效 proof 缓存**：防止重签重试命中已拒绝 proof 的死循环
   兜底（对默认配置同样无害）。
5. **头集合复核**：`build_makersuite_headers` 产物与抓包逐项对齐（唯一差异是 Free
   账号不发 tier 头，与真实客户端行为一致）；消融矩阵确认无缺失必需项。

## 7. 复现实验备忘

- 所有出站请求走 `socks5://<proxy>`（与账号出口一致）；
- 抓包 HAR：`aistudio发送信息请求.txt`（Reqable 3.1.1，JSON）；
- 关键实验脚本逻辑：header 消融（§2）、proof 重放/换 prompt（§3.1）、
  EXACT-REPLICA 对照（§3.2）、3×3 交叉（§3.2）、countTokens 直发（§4）。

---

## 8. 2026-11-12 第二轮深挖：浏览器形态落地与三个新发现

当日以抓包 Cookie + SOCKS5 代理对上游做差分实测（轻量 RPC + 新铸 proof 的
GenerateContent），并对全量 9 条抓包请求体逐槽逆向。已落地第 6 节清单之外
的完整浏览器形态默认值（见 WIRE_SPECIFICATION.md §7.6）与 30 项对齐性回归
测试（tests/unit/test_har_alignment.py）。新增逆向发现：

### 8.1 generation_config[13] 与 ListModels[83] 的严格映射

抓包两类模型呈现 flag13 二值性（3.8-flash 恒 1 / 3.5-flash-lite 恒 null），
与线上 ListModels 目录槽位 `[83]` 交叉比对后确认**严格对应**（1 ↔ 1，
null ↔ null；carbon / argon / 新代图像与 TTS 模型均为 1）。项目据此把
flag13 从「硬编码 3.8-flash 预设」升级为**目录元数据自动推导**
（model_discovery 解析 [83] → `unknownFlag13`，wire 构造时注入）。

### 8.2 浏览器默认思考档位 = 模型元数据默认档

抓包 3.5-flash-lite 默认档为 4（MINIMAL）而 3.8-flash 为 2（MEDIUM），
恰好等于各自 ListModels `[71][5]` 的默认档位 —— 浏览器并非统一 MEDIUM，
而是**跟随模型元数据默认档**。落地为 `browser_default_thinking_wire`。

### 8.3 403「caller does not have permission」的第三种语义：账号级生成限流

当日探测序列中，前 5 次新铸 proof 的 GenerateContent 全部 200，此后
（累计约 24 次生成尝试后）**所有**请求 403 —— 包括此前实测可长期重放的
**原版抓包 proof 完整重放**（同 prompt 同 body），而同期 CountTokens /
CheckUserStatus / GetAiStudioBenefitTier 依然 200。结论：

- 403 PERMISSION_DENIED 不只代表 proof 失效/摘要失配，还可能是
  **账号级生成限流/风控拦截**（会话与鉴权完全健康）；
- 该状态下重签无效（新铸 proof 同样 403），阶梯重试应尽快走向**切换账号**；
- 观测约 15 分钟未自愈；限流窗口长度待后续实测（未继续消耗配额验证）。

对项目的含义：现有 403 阶梯（2 重放 + 2 重签 + 1 切号）最终会切号故行为
正确，但对账号级封锁场景中间两次重签是无效消耗 —— 已在
Auth403RetryManager 注释中记录该语义；proof 复用缓存默认关闭（TTL=0）
的策略也因该发现得到进一步佐证。

### 8.4 当日实测矩阵（SOCKS5 代理 + spinsnow 新铸 proof）

| 实验 | 结果 |
|---|---|
| CountTokens · 现行项目头集（含 Fetch Metadata/visit-id 会话化） | 200 |
| CountTokens · Cookie 白名单扩充（+__Secure-STRP/+SEARCH_SAMESITE） | 200 |
| GenerateContent · 3.5-lite 浏览器默认形态（65536/1/0.95/64 + thinking 4） | 2/3 轮 200（第 3 轮 403 为 proof 抖动） |
| GenerateContent · 3.8-flash 精确形态（tools + 单元素 request_context + flag13=1） | 2/3 轮 200 |
| GenerateContent · request_context 旧三元素形态 | 200（上游宽容确认） |
| GenerateContent · gemini-4-carbon 浏览器形态 | 403（落入账号级限流窗口，形态本身经目录元数据交叉验证） |
| 原版抓包 proof 重放（账号限流前后对照） | 限流前 200 / 限流后 403（§8.3 证据链） |
| CountTokens / CheckUserStatus / GetAiStudioBenefitTier（限流期间） | 200/200/200（账号健康性证据） |

### 8.5 优化后全链路验证（无网络 spy + 服务 E2E）

- **生产路径 spy**（镜像 Gemini 原生调用链 `normalize → prepare_request`，
  假快照不出网）：请求体与抓包 [185] 参照体**逐槽一致** —— len=14、
  tools@6 模板、request_context@13 单元素、gen_config
  `[null,null,null,65536,1,0.95,64,null×6,1,null,null,[1,null,null,2]]`，
  头集含 Fetch Metadata / 会话化 visit-id、无 X-Browser-*；
- **服务 E2E**（完整 FastAPI 服务 + 抓包账号 + SOCKS5 代理）：
  `/v1/models` 200（37 个线上模型，含 carbon/argon 收录）、
  `POST /v1beta/:countTokens` 200（`{"totalTokens":2}`）、
  `:generateContent` 与 OpenAI 兼容端点 403 —— 账号级生成限流仍在
  窗口内（§8.3），请求形态已由 spy 独立验证，非实现问题。
