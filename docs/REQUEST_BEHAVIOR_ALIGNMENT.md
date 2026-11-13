# 请求行为与指纹全景对齐 (REQUEST_BEHAVIOR_ALIGNMENT.md)

> 2026-11-13 第二轮实测。基于 redacted-account@example.com（Pro）+ 固定 SOCKS5 出口，
> 以独立 httpx 探针 + 本地 waa-signer（SpiderMonkey 152）交叉验证。
> 本文聚焦三类请求的完整头形状、页面启动编排、传输层协议选择与指纹门控结论，
> 与 `CAPTURE_ALIGNMENT_2026-11.md`（头消融/WAA proof）互补。
> 复现脚本：`/aistudio-research/` 05-08 号实验（见文末索引）。

---

## 1. 三类请求的完整头形状（Go 签名器行为模型 + 活体验证）

### 1.1 导航请求（GET aistudio.google.com 页面）

```
Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8
Upgrade-Insecure-Requests: 1
Sec-Fetch-Dest: document
Sec-Fetch-Mode: navigate
Sec-Fetch-Site: none            # 直接打开；站内跳转为 same-origin
Sec-Fetch-User: ?1
Priority: u=0, i
User-Agent: <浏览器 UA>
Accept-Language: <账号语言>
Accept-Encoding: gzip, deflate, br, zstd   # Firefox 152 / Chrome 141+
Cookie: <aistudio.google.com 域全量 Cookie>
```

要点：
- 页面 HTML 内联 `WIu0Nc` 键即公开 API Key，实测恒为
  `AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs`（与本项目 `DEFAULT_API_KEY` 一致，已活体验证）；
- 页面另下发构建全局：`_F_toggles_default_MakerSuite`（含十六进制字面量，
  解析需 `int(x, 0)`）、`_F_cssRowKey`（boq-makersuite.MakerSuit…）、
  `_F_combinedSignature`（AMOXD…）；仅 WAA VM 环境仿真需要，Python 主链路无关；
- CSP 含 `require-trusted-types-for 'script'`（同样仅影响浏览器侧 WAA 执行环境）。

### 1.2 RPC 请求（POST alkalimakersuite-pa.clients6.google.com）

grpc-web 形状（Go `rpcHeaders` 模型，Python `build_makersuite_headers` 等价对齐）：

```
Accept: */*
Referer: https://aistudio.google.com/
Origin: https://aistudio.google.com
Content-Type: application/json+protobuf
X-Goog-Api-Key: AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs
X-Goog-AuthUser: 0
X-User-Agent: grpc-web-javascript/0.1
X-Goog-Ext-519733851-bin: <见 §2>
X-AIStudio-Visit-Id: v1_<base64(UUID)>        # 页面会话内恒定
X-AIStudio-G1-Tier: TIER1                      # Pro；Free 不发
Authorization: SAPISIDHASH <ts>_<sha1(ts+SAPISID+origin)>
Sec-Fetch-Dest: empty / Sec-Fetch-Mode: cors / Sec-Fetch-Site: same-site
Cookie: <.google.com 域白名单 16 项>
```

### 1.3 图片请求（WAA VM 环境仿真专用）

```
Accept: image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5
Referer: https://aistudio.google.com/
Sec-Fetch-Dest: image / Sec-Fetch-Mode: no-cors / Sec-Fetch-Site: same-origin
Priority: u=5, i
```

## 2. X-Goog-Ext-519733851-bin 的动态真相（已字节级验证）

真实页面每次加载调用 `GetLoggingContext []`，把响应 JSPB 按字段序号编码为
protobuf 再 base64，得到该头。实测（US 地区）：

```
GetLoggingContext 响应: [0,"B",null,null,null,1,4,0,null,4,3,"US",null,0,1,null,null,0,1]
编码结果:            CAASAUIwATgEQABQBFgDYgJVU3AAeAGQAQCYAQE=
本项目静态模板:       CAASAUIwATgEQABQBFgDYgJVU3AAeAGQAQCYAQE=   ← 逐字节一致
```

字段语义（protobuf 序号 = JSPB 下标+1）：f1=0, f2="B"(客户端类型), f6=1, f7=4,
f8=0, f10=4, f11=3, **f12=地区码（AISTUDIO_REGION）**, f14=0, f15=1, f18=0, f19=1。

结论：**本项目静态 `build_ext_bin()` 与官网动态派生在 US 地区完全等价**；
换地区只需替换 f12（`_EXT_BIN_PREFIX/SUFFIX` 拼接逻辑已正确实现）。
Python 主链路无需引入 GetLoggingContext 往返（省一次 RPC）。

## 3. 页面启动编排（浏览器行为模型，waa-signer `pageStartupRPCs` 复刻并实测全 200）

```
GET /prompts/new_chat?model=<模型>            ← 导航（§1.1 头形状）
   ├─ 提取 WIu0Nc API key + 构建全局 + CSP
   └─ 生成新 visit_id（v1_+base64(UUID)，页面会话内恒定）
并发突发（同 visit_id，7+1 路）：
   ├─ GetLoggingContext []        → §2 动态 Ext 头
   ├─ GetUserPreferences []
   ├─ GetAiStudioBenefitTier []   → [1]（Pro）
   ├─ ListModels []               → 163KB 实时目录
   ├─ GenerateAccessToken ["users/me"] ×2   → ya29.a0A… OAuth 令牌（§6）
   ├─ ListIncidentsHistory []     → 20KB 运维事件流
   ├─ ListPromos []
   └─ Waa/Create [challenge]      → BotGuard 挑战（另一把 API Key）
加载后继发（与 VM 自举并发）：
   ├─ ListRecentApplets []
   └─ ListPrompts [100]
最后：VM FillPrompt(bootstrap) → Settle → 就绪；VM 12h 后台刷新
     （刷新时 Waa/Create 携带 [interpreter_hash, snapshot]）
```

实测延迟（同代理出口）：7 路并发 565ms vs 串行 2412ms——**并发编排的收益主要在
页面体感，对 API 服务无鉴权影响**；Python 主链路不做该编排是完全可行的（必需项
仅 5 头 + cookie + proof，见 CAPTURE_ALIGNMENT §2 矩阵）。

## 4. 会话被动刷新链（ServiceLogin passive）

匿名（无 Cookie）导航会被 302 至：

```
accounts.google.com/ServiceLogin?passive=1209600&continue=<原页面>
```

浏览器携带 `.google.com` 域全量 Cookie 时**直接 200 无跳转**，仅轮换 SIDCC 三件套。
`__Secure-1PSIDTS/__Secure-3PSIDTS` 由 accounts.google.com 域在下发（本项目
RPC 端点链路观测不到该两枚，亦不需要）。Go 签名器 `loadPage` 以 5 跳重定向上限
+ accounts 域侦测（→ ErrAuthenticationRequired）实现同语义。

## 5. 指纹门控结论（页面层 + RPC 层双验证）

| 档案 | 页面 | CountTokens RPC |
|---|---|---|
| Firefox 152 Win（本项目主链路） | 200，API key/toggles 一致 | 200 |
| Android Chrome 130 + sec-ch-ua 全家桶（HAR 真机形态） | 200，内容一致 | 200 |
| Windows Chrome 141 + sec-ch-ua 全家桶 | 200，内容一致 | 200 |
| Chrome UA + en-US + Ext-region CN 交叉 | — | 200 |

**上游对 UA / sec-ch-ua-* / Accept-Language / Ext 地区均无门控**（页面与 RPC 双层）。
项目坚持 Firefox 152 全链路一致（uTLS 指纹层 ↔ 头层 ↔ 行为层同 UA）是正确姿态：
一致本身就是最强的指纹；Chrome 头族仅在「同一部署混合真 Chrome 流量」时有边际价值。

传输层：
- 真实浏览器走 SOCKS5 代理时同样回落 HTTP/2（QUIC 需 UDP 直连；Go 签名器
  `browserRoundTripper` 也是直连才按 Alt-Svc 升 H3，代理恒 H2）；
- 本项目 Python httpx H2 经代理与浏览器代理场景**天然行为对齐**；
- H3 仅在直连部署有意义（Go 签名器 `/v1/rpc/send` 已具备该能力）。

实测（同账号同代理）：`/v1/rpc/send`（Firefox152 uTLS 池直发受保护 RPC）与
Python httpx H2 延迟持平（GenerateContent ~3.3s vs ~3.5s，模型生成主导；
CountTokens 440ms；热池 mint 5-8ms/枚）。

## 6. GenerateAccessToken：页面自铸 OAuth 令牌（能力发现）

页面启动时 `GenerateAccessToken ["users/me"]`（发两次，得两枚不同令牌）直接返回
`ya29.a0A…` 形态的 OAuth access token（274B）。这说明 AI Studio 会话可以不经过
OAuth 客户端流程、纯凭 Cookie 铸造账号级 OAuth 令牌。本项目无需使用
（SAPISIDHASH 鉴权链完备），仅作为能力边界记录；该令牌的 scope 与有效期未深挖。

## 7. 已对齐/无需改动的 Python 主链路要点（活体验证汇总）

| 要素 | 项目现状 | 实测验证 |
|---|---|---|
| API Key | `DEFAULT_API_KEY` | = 页面 WIu0Nc，一致 |
| Ext-bin | 静态 `build_ext_bin(region)` | = GetLoggingContext 动态派生（US 字节级一致） |
| Visit-Id | 会话级复用 `v1_+base64(UUID)` | 与 Go `newVisitID`/HAR 形态一致 |
| 头集合 | `build_makersuite_headers` + 动态 merge | 三层架构已覆盖全部必需+指纹头 |
| 传输 | httpx H2 + SOCKS5 | 浏览器代理场景同为 H2，行为对齐 |
| Sec-Fetch/Accept-Language/UA | 可选指纹头 | 页面+RPC 双层无门控 |
| Cookie 白名单 + Set-Cookie 回写 | 16 项 + 滚动持久化 | 与页面/RPC 实测轮换规律吻合 |

---

## 8. 线缆级保真审计与对齐落地（第三轮，2026-11-13）

> 本轮聚焦「项目发出的字节 vs 真实浏览器发出的字节」——不问上游允不允许，
> 只问**是否一致**。方法：本地 TLS+ALPN(h2) 捕获服务器（h2 server 模式）
> 逐帧捕获项目 httpx 栈的实际 HEADERS 帧，与 waa-signer Go 浏览器模型
> （firefoxRequestHeaderOrder / rpcHeaders / ensurePreflight，逐头逆向
> 自真实 Firefox 152 流量）逐项 diff。脚本：`aistudio-research/09_wire_fidelity.py`。

### 8.1 审计发现（修复前）

| 维度 | 项目实际线缆形态 | 真实 Firefox 152 |
|---|---|---|
| 头顺序 | dict 插入序（content-type 打头） | 固定浏览器序（user-agent 打头） |
| `te: trailers` | **缺失**（httpx 不自带） | 每请求必带 |
| `accept-encoding` | `gzip, deflate, br`（缺 zstd） | `gzip, deflate, br, zstd` |
| `content-length` | httpx 追加到尾部 | 头块中段（x-user-agent 后） |
| CORS 预检 | **从不发 OPTIONS** | 每个新 URL 先匿名预检（Max-Age 缓存） |
| 会话令牌 | wire 槽 11 恒空 | 每轮回传上轮响应槽 7 令牌 |
| 页面 CountTokens 补发 | 无 | GenerateContent 后 +2.2s 一次 |
| 每主机连接上限 | 20 keepalive | Firefox = 6 |

### 8.2 已落地修复（transport.py / session.py / streaming.py / client.py / wire_codec.py）

1. **Firefox 线缆头序**（`_firefox_wire_header_list`）：出站头按
   `firefoxRequestHeaderOrder` 重建——user-agent, accept, accept-language,
   accept-encoding, referer, [tier], visit-id, ext-bin, api-key, authuser,
   authorization, content-type, x-user-agent, content-length, origin,
   cookie, sec-fetch-dest/mode/site, te；顺序表外残头按字母序殿后
   （Go orderedHeaderNames 语义）。头以**有序列表**传 httpx（dict 会丢失序）。
2. **`te: trailers`**：每 RPC 显式携带（httpx 允许透传，已实证）。
3. **`accept-encoding: gzip, deflate, br, zstd`**：显式 Firefox 值；
   新增 brotli/zstandard 依赖保证 httpx 能解码上游压缩响应
   （缺解码器时按能力降级，避免收到解不开的字节流）。
4. **显式 content-length**：按请求体长度写入 Firefox 序位（避免 httpx 追加尾部）。
5. **CORS 预检复刻**（`_ensure_cors_preflight`）：对 `*.clients6.google.com`
   的 RPC 先发匿名 OPTIONS（无 Cookie/Authorization），头序按 Firefox 预检
   形态（含 `priority: u=4`、ACR headers 按字母序）；按
   `Access-Control-Max-Age`（实测 3600s，Firefox 上限 24h）缓存；失败仅告警
   不阻断。**真实上游活体 200 验证通过。**
6. **会话令牌传播**（`session_token.py` + `attach_session_token`）：
   响应 chunk 槽 7 的 `v1_...` 令牌按账号作用域缓存，下一轮 GenerateContent
   请求注入 wire 槽 11（活体两轮 200 验证：Turn2 slot11 = Turn1 令牌）；
   在已签名 body 上注入不影响 proof（04 号实验：proof 只绑定 contents 摘要）。
7. **页面 CountTokens 补发**（`schedule_browser_counttokens_followup`）：
   GenerateContent 发出 2.2s 后对当前用户轮次补发一次 CountTokens
   （body `[model,[[[null,"用户文本"]],"user"]]`，与 Go countTokensBody 逐层
   一致）；fire-and-forget，`AISTUDIO_BROWSER_COUNTTOKENS_FOLLOWUP=0` 可关。
8. **连接池上限 6/6**：对齐 Firefox `max-persistent-connections-per-server`
   （H2 多路复用下单连接承载 ~100 并发流，6 连接足够）。

### 8.3 修复后线缆形态（本地 H2 捕获逐帧验证，PASS）

RPC POST HEADERS 帧头序（HPACK 解码顺序=线缆顺序）：

```
:method, :authority, :scheme, :path     ← httpcore 硬编码（见 §8.4）
user-agent → accept → accept-language → accept-encoding(gzip, deflate, br, zstd)
→ referer → x-aistudio-g1-tier → x-aistudio-visit-id → x-goog-ext-…-bin
→ x-goog-api-key → x-goog-authuser → authorization → content-type
→ x-user-agent → content-length → origin → cookie* → sec-fetch-dest
→ sec-fetch-mode → sec-fetch-site → te(trailers)        ← 19/19 头序一致
```

预检 OPTIONS（首请求前实际捕获）：

```
user-agent → accept(*/*) → accept-language → accept-encoding
→ access-control-request-method(POST) → access-control-request-headers
  (authorization,content-type,x-aistudio-g1-tier,x-aistudio-visit-id,
   x-goog-api-key,x-goog-authuser,x-goog-ext-…-bin,x-user-agent)
→ referer → origin → sec-fetch-dest/mode/site → priority(u=4) → te(trailers)
```

活体验证（SOCKS5 出口 + 真实 Google 上游，`11_live_e2e.py` /
`12_behavior_verify.py`）：预检 OPTIONS **200 + Max-Age 3600**；CountTokens/
CheckUserStatus 全 200；服务级 E2E generateContent×4 全部 attempts=1 首签
即 200（本地 SpiderMonkey 签名器）；两轮对话会话令牌正确传播、补发
CountTokens 精确 2.2s 到达且 200。

### 8.4 残余硬偏差（httpx/httpcore/h2 栈级，Python 主链路不可修）

| 偏差 | 项目实际 | 真实 Firefox 152 | 说明 |
|---|---|---|---|
| 伪头顺序 | `:method,:authority,:scheme,:path` | `:method,:path,:authority,:scheme` | httpcore `_send_request_headers` 硬编码 |
| cookie 位置 | 强制置尾 | 位于 sec-fetch-* 前 | h2 库 RFC 7540 cookie 合并行为 |
| H2 SETTINGS | 6 项（h2 缺省集） | 4 项 Firefox 值（表 65536/push 0/窗口 131072/帧 16384） | httpcore 构造时固定 |
| 连接 WINDOW_UPDATE | 3×16MiB 自动 | 1×12517377（12MiB） | h2 流控自动管理 |
| HEADERS priority | 无 | 权重 41 | httpcore 不支持 |
| TLS ClientHello | Python ssl | Firefox 152 uTLS | Go 签名器通道已对齐 |

**结论**：HTTP 语义层（头集合/值/序/行为）已 100% 对齐；帧级/TLS 级偏差
属 Python httpx 栈硬限制，浏览器级完整传输形态由 waa-signer
`POST /v1/rpc/send`（Firefox 152 uTLS + H2/H3 连接池）承载，Python 主链路
为其下游默认通道。行为缺口清单更新：
- ~~会话 token 未传播~~（已落地，§8.2-6）；
- ~~页面 CountTokens 补发~~（已落地，§8.2-7）；
- 页面启动编排（§3）仍未复刻（可选行为伪装，代价 ~0.5s 启动 RPC）。

## 9. 复现脚本索引（/aistudio-research/）

| 脚本 | 覆盖 |
|---|---|
| `05_behavior_choreography.py` | §1/§2/§3 页面启动编排 + Ext 动态派生验证 |
| `06_passive_refresh.py` | §4 被动刷新链（匿名 302 vs 带 Cookie 200） |
| `07_rpc_send_h3.py` + `run-h3.sh` | §5 传输对照（签名器 uTLS 池 vs Python H2） |
| `08_chrome_fingerprint.py` | §5 指纹门控矩阵（三档案 × 页面/RPC） |
| `09_wire_fidelity.py` | §8 线缆保真审计（修复前基线捕获 + diff） |
| `10_wire_verify.py` | §8.3 修复后传输层逐帧验证（本地 H2 捕获 PASS） |
| `11_live_e2e.py` | §8.3 活体线缆验证（真实 Google 预检 200 + RPC 200） |
| `12_behavior_verify.py` | §8.2-6/7 令牌传播 + 2.2s 补发活体验证（PASS） |
| `choreography_results.json` / `passive_refresh_results.json` / `h3_results.json` / `chrome_fingerprint_results.json` / `wire_fidelity_results.json` / `wire_verify_results.json` / `live_e2e_results.json` / `behavior_verify_results.json` | 原始数据 |
