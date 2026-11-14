# 2026-11-14 浏览器侧 403 根因判别与混合拦截捕获方案

本轮逆向目标：在真实浏览器（Playwright + Chromium）里打开
`aistudio.google.com`，观察页面自然构造的 GenerateContent 请求头与
工具调用线缆结构（thoughtSignature 往返）。过程中浏览器直发请求被
上游 403 拒绝，本文记录 403 的根因判别过程、最终可行的解决方案
（混合拦截架构），以及该方案下捕获到的最新 wire 对齐证据。

## 一、403 现象

- 浏览器内页面加载、`CountTokens`、`ListModels` 等不带 WAA proof 的
  RPC 全部 200；仅携带 proof 的 `GenerateContent` 被拒绝。
- 403 响应体（截断的 JSPB 数组）：

  ```text
  [,[7,"The caller does not have permission"]]
  ```

- 403 与既有 400 问题正交：400 是 RPC 语义层（thought_signature 缺失），
  403 是 WAA/BotGuard 验证层。两者的判据、修复路径互不影响。

## 二、逐步排除的错误假设

| 假设 | 实验 | 结果 | 结论 |
| --- | --- | --- | --- |
| 地区拦截 | 直连访问 aistudio.google.com | 302 跳转 available-regions 文档页 | 经代理解决，与 403 无关 |
| 代理 IP 被标记 | 同代理 + 同 Cookie 走项目签名栈发 GenerateContent（`discriminate_test.py`） | **200** | IP/账号/Cookie 均无辜 |
| 账号权限 | 同上 200；浏览器内其他 RPC 均 200 | 正常 | 账号无辜 |
| UA 伪装导致 proof 失配 | 去掉 Firefox UA 伪装后重试 | 仍 403 | UA 伪装非唯一因素 |
| headless 特征 | xvfb 有头模式（`Xvfb :99` + `DISPLAY=:99`）重试 | 仍 403 | 有头不解决 |
| 浏览器 TLS/H2 指纹 | CDP 完整覆盖 UA + Client Hints 为 Chrome/151 Windows 后 `route.continue` 放行（hybrid3） | 仍 403 | 浏览器网络栈发出的请求被拒 |

## 三、判别实验的核心证据

同一账号、同一 SOCKS5 代理、同一模型（`models/gemini-4-hydrogen`）、
同一端点，仅改变"请求由谁构造、由谁发出、proof 由谁计算"：

| 路径 | proof 来源 | 发送栈 | 结果 |
| --- | --- | --- | --- |
| 项目完整签名栈（`AIStudioClient` + spinsnow） | spinsnow 远程签名 | 项目 httpx HTTP/2 | **200** |
| 真实 Chromium（无伪装 / UA 伪装 / 有头） | 页面内 BotGuard JS | Chromium 网络栈 | 403 |
| hybrid3：浏览器构造 + spinsnow proof + CDP 指纹覆盖 | spinsnow | Chromium 网络栈（`route.continue`） | 403 |
| hybrid4：浏览器构造 + spinsnow proof + httpx 代发 | spinsnow | 项目 httpx HTTP/2 | **200** |

结论——403 的否决点有三个，全部位于"浏览器自身"：

1. **proof 声明的运行环境**：页面内 BotGuard 在自动化环境（Playwright
   注入的 CDP、`navigator.webdriver`、缺失的插件/指纹面）下计算的
   proof，其加密声明与真实桌面 Chrome 不符，上游直接拒绝。
2. **HeadlessChrome UA / `sec-ch-ua` 头**：headless Chromium 自带
   `"HeadlessChrome";v="143"` 等客户端提示头，是 403 否决点。
3. **浏览器 TLS/H2 指纹**：Chromium 网络栈的 JA3/JA4 与
   HTTP/2 SETTINGS 帧特征与真实 Chrome 桌面栈存在差异；hybrid3 证明
   即使换 proof + 覆盖 UA 头，只要仍由浏览器发出即 403。

三者只要全避开（proof 换 spinsnow 铸、请求改由 httpx 代发、头集合
用项目规范头），同一代理与 Cookie 即 200。

## 四、最终方案：混合拦截架构（hybrid4）

思路：**让页面全速自然运转**（Angular 前端自己维护会话状态、构造
完全对齐的请求体），只在网络边界做三件事——观察、换 proof、代发回填。

```text
Chromium (Playwright, socks5://127.0.0.1:11080 桥接)
  │  页面 JS 自然构造 GenerateContent（工具历史+签名+会话 token 全保真）
  ▼
page.route(/$rpc.*GenerateContent/)          ← 拦截点
  ├─ 1. 落盘 req_NN_natural_headers.json     ← 观察目标 1：真实请求头
  ├─ 2. 落盘 req_NN_natural_body.json        ← 观察目标 2：真实请求体
  ├─ 3. POST 127.0.0.1:8765/mint             ← spinsnow 按 wireContents 铸 proof
  │      body[4]（SNAPSHOT 槽位）= 新 proof
  ├─ 4. POST 127.0.0.1:8765/send             ← httpx 代发
  │      · URL 用页面原始 URL
  │      · 头集合完全重建：build_makersuite_headers() + Windows Chrome UA
  │        （忽略浏览器头——HeadlessChrome UA/sec-ch-ua 是 403 否决点）
  │      · 403 时自动重签重试（模拟 Auth403RetryManager，最多 3 次）
  └─ 5. route.fulfill(响应)                   ← 回填页面，UI 正常渲染
                                            （页面不感知，多轮会话可继续）
```

要点：

- **只对 `GenerateContent` 类 RPC 做 proof 替换与代发**；CountTokens
  等其他 `$rpc` 原样放行（它们不带 proof，浏览器直发即 200）。
- `/mint` 的 digest 计算必须与页面一致：只取 TEXT/INLINE_DATA/FILE_DATA
  槽位拼 prompt（`extract_content_digest` 同款逻辑），functionCall 等
  部分计空串——因此**改写工具历史不会破坏 proof 摘要**。
- `/send` 必须用项目规范头集合（`build_makersuite_headers` + 三段式
  `Authorization: SAPISIDHASH ... SAPISID1PHASH ... SAPISID3PHASH ...`
  + `X-Goog-Api-Key` + `X-Goog-Ext-519733851-bin` + `X-AIStudio-G1-Tier`
  + `X-AIStudio-Visit-Id`），UA 用真实 Windows Chrome 串。
- 提交输入用 `Ctrl+Enter`（AI Studio 的 Run 快捷键），Enter 不发送。
- 认证 SOCKS5 代理给 Chromium 用需本地桥接（RFC1929 用户名密码认证
  Chromium 不支持）：桥接器监听 `127.0.0.1:11080` 免认证，向上游完成
  认证握手后透明 relay。
- Cookie 装载：域 `.google.com`、secure；`__Secure-1PAPISID` /
  `__Secure-3PAPISID` / `SEARCH_SAMESITE` 需排除 httpOnly 才能注入。

实测结果：3 轮对话（含 Google Search 工具 + 思考摘要 + 多轮签名链）
全部 200，页面 UI 正常渲染模型回复，`batchexecute` 会话持久化正常。

## 五、本轮捕获的 wire 对齐新证据

以下均为 hybrid4 方案下页面**自然构造**的请求（模型
`models/gemini-4-hydrogen`，账号 Pro / TIER1）：

### 5.1 顶层槽位（与 `wire_spec.py` TopLevelIndex 对照）

| 槽位 | 项目定义 | 实测值 |
| --- | --- | --- |
| [0] MODEL | ✓ | `models/gemini-4-hydrogen` |
| [1] CONTENTS | ✓ | 多轮历史 |
| [2] SAFETY_SETTINGS | ✓ | `[[null,null,7,5],[null,null,8,5],[null,null,9,5],[null,null,10,5]]`（4 类阈值全 5） |
| [3] GENERATION_CONFIG | ✓ | 见 5.2 |
| [4] SNAPSHOT | ✓ | proof（mint 替换槽位） |
| [6] TOOLS | ✓ | Google Search = `[null,null,null,[null,[[]]]]`，与 TOOLS_TEMPLATES 完全一致 |
| [11] SESSION_TOKEN | ✓ | `v1_...`（46 字符）；**仅第 2 轮起出现**，值 = 上一轮响应尾部的会话 token |
| [13] REQUEST_CONTEXT | ✓ | `[[null,null,"UTC"]]` |

### 5.2 generationConfig 实测（46 槽位）

| 槽位 | 项目定义 | 实测值 |
| --- | --- | --- |
| [3] MAX_TOKENS | ✓ | 32768 |
| [4] TEMPERATURE | ✓ | 1 |
| [5] TOP_P | ✓ | 0.95 |
| [6] TOP_K | ✓ | 64 |
| [13] UNKNOWN_FLAG_13 | ✓ | 1（与目录 [83]=1 的新代模型规则一致） |
| [16] THINKING_CONFIG | ✓ | `[1,null,null,3]`（mode=1, level=3 HIGH） |
| **[45]** | **未定义（项目缺口）** | **1** —— 新未知开关，语义待逆向，记录备透传 |

### 5.3 工具调用与签名的往返形态

- **思考摘要**：历史回传时是独立 part——`[null, 文本, ..., 12:1]`
  （slot 12 = THOUGHT_ALT_FLAG = 1），text 槽位放摘要正文。
- **thoughtSignature 载体**：每个 model content 的**最后一个 part** 是
  `[null, "", ..., 14:签名]`——空文本 + slot 14 签名（本轮实测
  7892B / 6660B base64）。签名 part 不带 thought 标记、不依附于任何
  业务 part，是独立尾 part。
- 响应侧：最终分片携带同形态的空文本签名 part，其后是
  usage（`[输入, 输出, 思考, null, 分片, null, 分片, ...]`）与
  会话 token（回传下一轮 slot 11）。
- **Google Search（grounding 类服务端工具）不产生 functionCall part**：
  搜索在服务端执行，历史中只有思考摘要与文本，无任何
  `default_api:` 命名空间调用痕迹。本账号（记忆/Saved Info 未启用）
  的全部捕获中均未出现 `default_api:context`——与
  commit `46f5139` 的防御性剥离逻辑不冲突：该修复仅在客户端回传
  无签名服务端工具调用时生效，正常路径零影响。

### 5.4 请求头实测（与项目规范头集合对照：全部一致）

- `Authorization`：三段式 `SAPISIDHASH ts_hash SAPISID1PHASH ts_hash
  SAPISID3PHASH ts_hash`（三段同 ts 同 hash 值，源自 SAPISID 与
  `__Secure-1PAPISID`/`__Secure-3PAPISID` 同值）
- `Content-Type: application/json+protobuf`、
  `X-User-Agent: grpc-web-javascript/0.1`
- `X-Goog-Api-Key: AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs`
- `X-Goog-Ext-519733851-Bin: CAASAUIwATgEQABQBFgDYgJVU3AAeAGQAQCYAQE=`
- `X-AIStudio-G1-Tier: TIER1`、`X-AIStudio-Visit-Id: v1_<base64(UUID)>`
- `Accept-Language: zh-CN`（页面 locale 跟随）

## 六、复现资产（脱敏归档）

`scripts/browser-hybrid/`（随本文档归档，凭据均已参数化）：

| 文件 | 作用 |
| --- | --- |
| `socks5_bridge.mjs` | 认证 SOCKS5 本地桥接（上游地址/账号密码走环境变量） |
| `mint_server.py` | 本地 mint+send 服务：spinsnow 铸 proof、httpx 代发、403 重签 |
| `hybrid_capture.mjs` | Playwright 主脚本：Cookie 装载、route 拦截、观察落盘、代发回填 |

运行前置：Playwright Chromium 已安装；`data/accounts/*/auth.json`
存在可用账号；`config.yaml` 的 `waa_servers` 配置了可用远程签名服务
（或本地编译 `waa-signer`）；出口 IP 可达 aistudio.google.com（或配代理）。

## 七、验证范围与限制

- 本轮全部实验严格限定 `aistudio.google.com` 域；未访问其他 Google 服务。
- 实验账号一个（jshsosjs77@gmail.com，Pro/TIER1，记忆未启用）；
  免费层账号的 tier 头与限流路径未在本轮覆盖。
- 浏览器侧只验证了 `GenerateContent`；`GenerateTitle`、
  `CreatePrompt` 等其他 proof-bearing RPC 理论上同架构可复用，未实测。
- hybrid4 的目的是**观察**真实线缆结构，不是生产通路；生产通路仍是
  项目自身的签名栈（其 200 已由判别实验证明）。
- 本轮捕获未含客户端自定义函数（function calling）的调用往返——
  仅覆盖 grounding 类服务端工具；函数工具往返形态与对齐结论见
  `FUNCTION_CALL_ROUNDTRIP_2026-11-14.md`（hybrid6 实验）。
