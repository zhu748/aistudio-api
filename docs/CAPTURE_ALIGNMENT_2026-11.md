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

---

## 9. 2026-11-12 第三轮深挖（新抓包换新 cookie）：403 第四种语义与签名器熔断

用户以手机浏览器重新验证账号可用（新 HAR：`最新抓包.txt`，6 条 MakerSuite RPC 全 200，
含 GenerateContent ×2），推翻「账号被限流」的单一解释。以该新鲜 Cookie + 同 SOCKS5
代理重做对照矩阵，锁定真正的根因。

### 9.1 新鲜 Cookie 对照矩阵（同代理同 UA 同 visit-id，逐字节重放）

| 实验 | 结果 | 结论 |
|---|---|---|
| CountTokens（合并轮换 cookie） | 200 | 会话/鉴权/代理链路健康 |
| GenerateContent · 抓包 body 逐字节重放（原版浏览器 proof + 原版 cookie） | **200** | 账号健康，昨日 403 窗口已过期 |
| GenerateContent · 同 body 换合并轮换 cookie | **200** | cookie 轮换（SIDCC/PSIDCC set-cookie）不影响 |
| GenerateContent · spinsnow 新铸 proof #1（浏览器 5 槽形态） | **403** | mint 质量问题浮出 |
| GenerateContent · 新铸 proof #2 / #3（含不带 proxy 参数变体） | **403 / 403** | 3/3 全拒 —— 非概率抖动 |
| 同分钟原版 proof 对照重放 | **200** | 决定性：账号与请求形态无辜，**被拒的只有 mint proof** |

### 9.2 项目构造路径端到端验证（「这个是不是对的」的最终答案）

以项目自身调用链 `normalize_gemini_request → AIStudioClient.prepare_request`
构造请求体（快照注入抓包原版浏览器 proof），与抓包 body 逐槽比对后经代理发送：
**200**（差异槽仅为浏览器会话续接 token 等可选字段，语义无关）。项目构造的
请求形态正确性自此与 proof 质量解耦验证完毕。

### 9.3 spinsnow 服务级挑战过期（TLV 解码铁证）

按 §7.1 规范解码 `!proof` 外层 TLV 信封对比（浏览器 3 枚 / mint 5 枚）：

| 字段 | 浏览器 proof（200） | spinsnow mint proof（403） |
|---|---|---|
| challenge_version（Tag66） | `3153048796` | `3109903533`（**过期挑战构建**） |
| challenge_build_ts（Tag66） | `3705056239` | `2404368952` |
| 加密环境载荷（Tag153） | 1352~1436 B | **1015 B**（薄 ~340B，缺交互/DOM 信号） |
| 冷启动耗时（Tag2） | 62 ms | 282 ms |
| 版本 / 魔数 / baseEnd | v3 / 0xA5 / 100 | 一致（协议层未变） |

5 次 mint（跨 3.5-lite / 3.8-flash / 4-carbon 三模型、随机 prompt）全部携带
**完全相同**的 `challenge_version=3109903533 / build_ts=2404368952` ——
spinsnow 全节点共用同一过期挑战，属服务级降级而非节点抖动。浏览器当日挑战
（3 枚 proof 一致为 3153048796）与旧抓包（上一轮会话时重放 200）完全一致，
证明 Google 正常轮换挑战而 spinsnow 的 VM 池未跟进。

### 9.4 403 第四种语义：签名器 attestation 质量降级

**「The caller does not have permission」在会话与账号完全健康、请求形态逐字节
正确的前提下，可因签名器产出的 proof 质量不足（过期挑战 / 薄环境声明）被系统性
拒绝。** 该语义与 §8.3 账号级限流在单请求内表现完全相同（403 + 轻量端点 200，
CountTokens 无法区分二者）；唯一可观测差异在跨账号维度：

- §8.3 账号级限流：仅受影响账号 403，切号即恢复；
- 本语义：**所有账号**的新铸 proof 全 403（签名器是公共依赖），切号只会
  无差别烧穿账号池并给无辜账号记 auth_error 惩罚。

### 9.5 落地：SignerHealthTracker 熔断（`infrastructure/waa/health.py`）

- **判据**：滑动窗口（默认 300s）内 ≥3 次「403 记账」且涉及 ≥2 个不同账号
  → 判签名器降级，进入冷却（默认 60s）。单账号信号永不触发（无法与 §8.3
  区分，保守放行完整阶梯）。阈值支持 `AISTUDIO_SIGNER_CB_*` 环境变量调节。
- **降级期间**：403 阶梯在 count≥3（重签/切号阶段）直接短路，返回
  `type=signer_degraded` 的可定位错误；首个 403 不再对账号记 auth_error
  （账号无辜）；前两跳免费重放保留（覆盖可自愈语义 1/2）。
- **半开语义**：任何一次成功生成（proof 被上游接受，含流式已产出数据后
  途断的 403）立即解除熔断并清窗。
- **可观测**：新增 `GET /config/signer-health` 管理端点（窗口计数、涉及
  账号数、冷却剩余、最近一次触发详情）。
- 回归测试：`tests/unit/test_signer_health.py`（9 项：判据、清窗、剪枝、
  快照字段、阶梯完整性保持、短路行为、端到端风暴聚合）。

### 9.6 当日第三轮实测矩阵汇总

| 实验 | 结果 |
|---|---|
| CountTokens · 新鲜合并 cookie | 200 |
| GenerateContent · 原版 proof 逐字节重放（×2 变体） | 200 / 200 |
| GenerateContent · 新铸 proof（×3，含无 proxy 变体） | 403 ×3 |
| GenerateContent · 项目构造路径 + 原版 proof | **200** |
| mint ×5（3 模型 / 随机 prompt）TLV 解码 | 全部同一过期挑战（3109903533） |

### 9.7 优化后服务 E2E（完整 FastAPI + 抓包账号 + SOCKS5 代理 + 劣化 spinsnow）

| 端点 | 结果 | 备注 |
|---|---|---|
| `GET /health` | 200 | |
| `POST /v1beta/:countTokens` | **200** `{"totalTokens":2}` | attempts=1，1.5s |
| `POST /v1beta/:generateContent`（E2E-R4 prompt） | **200** 返回 `E2E-R4` | **attempts=3，4.5s**：第 1 次发送（新铸 proof）403 → 阶梯重放第 2 次 403 → **第 3 次重放 200** |
| `GET /config/signer-health` | 200 | 成功后窗口清零（半开语义生效），未误触发熔断 |

两条由 E2E 修正的既有结论：

1. **「mint proof 单次使用」非绝对教条**：同枚 proof 第 3 次发送即 200 ——
   上游对 VM 级 proof 的拒绝存在概率性（与 §3.2 观测的「新 mint 偶发 403」
   同源）。403 阶梯保留的前两跳免费重放在劣化场景下具有真实恢复价值
   （本次实战恢复），熔断将 count≥3 才短路的设计与之自洽。
2. **SignerHealthTracker 的半开语义实战验证**：阶梯恢复成功即调用
   record_proof_accepted 清窗，避免一次抖动触发熔断误杀后续请求。
