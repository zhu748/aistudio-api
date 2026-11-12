# aistudio-api

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/API-Gemini%20v1beta-4285F4?style=flat-square&logo=google" alt="Gemini API"></a>
  <a href="https://hub.docker.com/r/bedrockblock/aistudio-api"><img src="https://img.shields.io/badge/Docker-bedrockblock%2Faistudio--api-2496ED?style=flat-square&logo=docker" alt="Docker Hub"></a>
  <a href="https://deepwiki.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/DeepWiki-Docs-5B68E4?style=flat-square" alt="DeepWiki"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?style=flat-square&logo=vuedotjs" alt="Vue 3"></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/Python-3.11%2B%20%7C%20uv-261230?style=flat-square&logo=python" alt="Python uv"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
</p>

<p align="center">
  Google AI Studio 反向代理服务，将 Web 端 AI Studio 转换为标准 Google Gemini API 协议接口。
</p>

<p align="center">
  <a href="./README_EN.md">English Documentation</a>
</p>

---

## 目录

- [核心特性](#核心特性)
- [技术架构与文档](#技术架构与文档)
- [快速开始](#快速开始)
  - [Android (Termux)](#android-termux)
  - [Windows](#windows)
  - [Linux / macOS](#linux--macos)
  - [Docker 部署](#docker-部署)
- [账号凭据 (Cookie) 获取与导入指引](#账号凭据-cookie-获取与导入指引)
- [第三方客户端配置指引 (NextChat / Cherry Studio 等)](#第三方客户端配置指引-nextchat--cherry-studio--one-api-等)
- [配置与环境变量](#配置与环境变量)
- [API 调用示例](#api-调用示例)
  - [cURL 调用](#curl-调用)
  - [Python SDK (google-genai)](#python-sdk-google-genai)
  - [工具调用 (Function Calling)](#工具调用-function-calling)
- [Web 管理控制台](#web-管理控制台)
- [开发与测试](#开发与测试)
- [借物表与致谢](#借物表与致谢-credits--acknowledgments)
- [开源协议](#开源协议)

---

## 核心特性

| 功能模块 | 说明 |
|---|---|
| **Gemini 协议兼容** | 支持 `/v1beta/...` 接口规范，包括 Thinking 思维链、多模态、Function Calling 工具调用及图片生成 |
| **OpenAI / Claude 协议兼容** | 额外提供 `/v1/chat/completions`（OpenAI Chat Completions）与 `/v1/messages`（Anthropic Claude Messages）兼容端点：流式/非流式、工具调用、多模态图片、思考链（`reasoning_content` / thinking 块）、模型名自动映射（`gpt-*` / `claude-*` → 配置的 Gemini 模型），完整复用多账号轮换与抗截断传输 |
| **动态模型发现** | 自动向上游同步可用模型列表，支持 `gemini-3.7-flash`、`gemini-3.8-flash` 等最新模型 |
| **多账号自动轮换** | 支持多账号管理与故障自动切换，按模型独立记录 429 配额，每日美西午夜自动重置 |
| **运行状态持久化** | 模型调用量、Token 消耗统计 (`stats.json`) 以及账号 429 限额与冷却状态 (`rotator_state.json`) 自动落盘，容器/服务重启不丢失状态 |
| **请求审计与运行日志** | 可检索请求历史、账号/代理尝试与 Token 元数据；结构化日志轮转落盘并按请求 ID 关联。默认不保存请求或响应正文 |
| **下游参数容错与丢弃** | 支持配置 `drop_unsupported_params` 自动过滤下游传入的非标准安全分类（如 `HARM_CATEGORY_CIVIC_INTEGRITY`）与不兼容工具，避免 400 阻断 |
| **批量子账号探活** | 导入单个包含多个 Google 身份的 Cookie 后，自动探测子账号（`u/0`, `u/1`...）并分别建档 |
| **内置搜索与工具** | 支持 Google Search 联网搜索、Google Maps、代码执行沙箱等官方扩展能力 |
| **双轨签名与特征对齐** | 脱离重型浏览器，支持本地 Go 签名服务 (SpiderMonkey 152) 与远程 NodeVM 签名节点，自动绑定匹配的 User-Agent 与请求头特征 |
| **Web 管理面板** | 提供 Web 管理控制台，支持账号管理（文本与 JSON 批量导入）、调用量监控、在线编辑与热重载 `config.yaml` |
> [!NOTE]
> **内存占用参考**：脱离重型浏览器后，服务常驻物理内存仅约 35 - 50 MB，冷启动毫秒级，即使在入门级 Android 手机（Termux）或轻量服务器上亦可稳定高效常驻。
## 技术架构与文档

```
┌────────────────────────────────────────────────────────┐
│               Client (cURL / Python SDK)               │
└───────────────────────────┬────────────────────────────┘
                            │ /v1beta/models/...
┌───────────────────────────▼────────────────────────────┐
│              FastAPI HTTP Gateway (Port 8080)          │
├───────────────────────────┬────────────────────────────┤
│    Account Rotator        │    Chat & Wire Codec       │
│  (Sticky & 429 Isolation) │   (Protobuf-JSON / WAA)    │
└───────────────────────────┼────────────────────────────┘
                            │ Proof Mint & HTTP/2 Stream
┌───────────────────────────▼────────────────────────────┐
│       Browserless WAA Engine (waa-signer / Remote NodeVM)     │
│       Async Connection Pooling (HTTPStreamTransport)   │
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS / RPC
┌───────────────────────────▼────────────────────────────┐
│         Google AI Studio (alkalimakersuite-pa)         │
└────────────────────────────────────────────────────────┘
```

详细的设计与协议分析文档：

- [系统架构设计 (ARCHITECTURE.md)](./docs/ARCHITECTURE.md)：分层架构、请求生命周期、Wire Codec 编解码与并发控制模型。
- [Wire 协议逆向技术规范 (WIRE_SPECIFICATION.md)](./docs/WIRE_SPECIFICATION.md)：Protobuf-JSON 数组映射、字段索引、多模态 Part 结构与 Schema 编码。
- [BotGuard 验证链路机制 (BOTGUARD_VERIFICATION_CHAIN.md)](./docs/BOTGUARD_VERIFICATION_CHAIN.md)：WAA 挑战握手、内容哈希计算、快照签名与服务端校验流程。
- [WAA 虚拟机内部架构与签名协议规范 (WAA_VM_ARCHITECTURE.md)](./docs/WAA_VM_ARCHITECTURE.md)：多态指令集 (ISA)、五阶段 ARX 流密钥派生、环境探针与 `!proof` TLV 字典。
- [开发与协作规范 (AGENT.md)](./AGENT.md)：类型系统要求、代码质量检查与开发指南。

---

## 快速开始

> [!NOTE]
> **开箱即用提示**：服务默认内置远程 WAA 证明签名节点，无需额外编译，安装依赖后**直接启动即可开箱即用**！若追求极致的毫秒级本地性能与离线化运行，可在运行后随时按需编译本地原生引擎（各平台步骤见下方进阶可选说明）。
### Android (Termux)

> [!TIP]
> **下载提示**：请务必从 [F-Droid](https://f-droid.org/packages/com.termux/) 或 [GitHub Releases](https://github.com/termux/termux-app/releases) 安装 Termux。**切勿使用 Google Play 商店版本**（Play 商店版本已停止维护，更新环境会报错）。

打开 Termux，依次复制执行以下命令：

```bash
# 步骤 1：更新 Termux 基础环境并安装 git
pkg update -y && pkg install -y git

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# 步骤 3：初始化依赖环境并同步安装
bash scripts/setup-env.sh
uv sync

# 步骤 4：直接启动服务（开箱即用）
uv run python main.py server --port 8080
```

<details>
<summary><b>【进阶可选】编译本地原生高性能签名服务 (waa-signer)</b></summary>

若需脱离外部网络节点、获得 10~30 毫秒本地极速签名体验：
```bash
pkg install -y golang zig
bash waa-signer/build.sh
```
编译完成后重启服务，系统将自动识别并切换至本地原生引擎。
</details>

启动成功后：
1. 打开手机自带浏览器，访问 **`http://localhost:8080`** 即可进入 Web 管理控制台；
2. 参考下方 [账号凭据获取指引](#账号凭据-cookie-获取与导入指引) 导入 Google Cookie 即可开始调用；
3. **后台保活提示**：下拉手机通知栏，点击 Termux 通知项上的 `Acquire Wakelock`，并在手机系统设置中为 Termux 开启“无限制后台运行/电池优化白名单”，防止锁屏休眠被系统杀后台。
---

### Windows

推荐前置：电脑已安装 [Git](https://git-scm.com/)。Python 环境可由 `uv` 全自动管理。

打开 PowerShell 或终端，依次执行：

```bat
# 步骤 1：安装 uv 包管理工具
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# 步骤 3：同步安装项目依赖（uv 会自动安装所需 Python 环境）
uv sync

# 步骤 4：直接启动服务（开箱即用）
uv run python main.py server --port 8080
```

<details>
<summary><b>【进阶可选】一键编译本地高性能签名服务 (waa-signer.exe)</b></summary>

若需启用毫秒级本地原生签名引擎：
```powershell
# 推荐安装编译器（脚本亦支持自动检测与提示安装）：
winget install GoLang.Go zig.zig

# 执行一键构建脚本
.\waa-signer\build.bat
```
编译完成后重启服务即可生效。
</details>

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。

---

### Linux / macOS

打开终端，依次执行：

```bash
# 步骤 1：安装 uv 包管理工具
curl -LsSf https://astral.sh/uv/install.sh | sh

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# 步骤 3：同步安装项目依赖
uv sync

# 步骤 4：直接启动服务（开箱即用）
uv run python3 main.py server --port 8080
```

<details>
<summary><b>【进阶可选】编译本地高性能签名服务</b></summary>

前置要求：已安装 Go 1.22+ 与 zig（或 clang / gcc）：
```bash
bash waa-signer/build.sh
```
</details>

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。
---

### Docker 部署

> [!NOTE]
> 本项目 `Dockerfile` 采用多阶段构建，在镜像构建时会**全自动下载依赖并编译** `waa-signer` 二进制服务内置于容器内，容器启动时自动拉起本地原生签名引擎，开箱即用，无需手动构建。

```bash
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 8080:8080 \
  -v aistudio-api-data:/app/data \
  ghcr.io/deconstructedcube/aistudio-api:latest
```
使用 Docker Compose 启动：

```bash
docker compose up -d
```
启动后访问 **`http://localhost:8080`**（或服务器 IP 端口）进入管理控制台。

### Render 一键部署

> [!NOTE]
> 仓库内置 [`render.yaml`](./render.yaml) Blueprint，推送至 GitHub/GitLab 后在
> Render 控制台 **New → Blueprint** 选择仓库即可自动完成构建与部署，无需服务器。

**部署步骤：**

1. 将本仓库 Fork / 推送到你的 GitHub 仓库；
2. 在 [Render 控制台](https://dashboard.render.com) 选择 **New → Blueprint**，选中该仓库；
3. 首次部署会提示填写密钥环境变量：
   - `AISTUDIO_WEB_PASSWORD`（**必填**）：Web 管理面板登录密码，公网部署务必设置；
   - `AISTUDIO_COOKIES` / `AISTUDIO_COOKIES_2`（**推荐**）：环境变量注入账号 Cookie，重启后自动恢复，格式见[环境变量注入账号](#环境变量注入账号无磁盘部署)；更多账号可在面板继续添加 `AISTUDIO_COOKIES_3`、`AISTUDIO_COOKIES_4` ...；
   - `AISTUDIO_API_KEYS`（**推荐**）：API 调用鉴权 Key（逗号分隔多个，如 `sk-aaa,sk-bbb`），重启后自动恢复，防止 API 接口裸奔；
   - `AISTUDIO_PROXY`（可选）：出站代理，如 `socks5://user:pass@host:port`，Google 对数据中心 IP 风控严格时配置；
4. 等待构建完成（多阶段构建会自动编译本地 WAA 签名器，约 5~10 分钟）；
5. 访问 Render 分配的 `https://<service>.onrender.com` 进入管理控制台，导入 Cookie 账号即可使用。

**关键行为说明：**

| 项目 | 说明 |
| --- | --- |
| 端口 | Render 注入 `PORT` 环境变量，entrypoint 自动映射为监听端口，无需手动配置 |
| 健康检查 | `/healthz`（`/health` 等价别名），轻量无鉴权，零账号冷启动也可通过探活 |
| 数据持久化 | **Free 计划无磁盘**：`/app/data` 随容器重启 / 重新部署清空 —— 账号用 `AISTUDIO_COOKIES` 环境变量注入，每次启动自动重建；调用统计等易失数据随重启归零 |
| 配置初始化 | 每次启动自动将镜像内置的 `defaults/config.yaml` 模板初始化到 `/app/data`；Free 计划面板修改不持久，需固化的配置请用环境变量下发 |
| 签名引擎 | 默认拉起容器内置的本地 Go 签名器（无需浏览器）；Free 计划建议设 `AISTUDIO_WAA_SERVER=remote_spinsnow` 改走远程签名器（更省内存）。环境变量仅指定**启动默认**节点，面板手动切换对当前进程即时生效，重启后自动恢复默认 |

**计划选择建议：**

- **Free（512MB，Blueprint 默认）**：内存足够部署 —— 本引擎不需要浏览器（纯 Go + SpiderMonkey 本地签名）。无磁盘：账号用 `AISTUDIO_COOKIES` 环境变量注入，随重启自动恢复；15 分钟无流量自动休眠（唤醒约有几十秒冷启动）。建议设置 `AISTUDIO_WAA_SERVER=remote_spinsnow` 作为长期默认（重启后自动回到该节点，同时跳过本地引擎进程更省内存）；临时排查时仍可在面板手动切换本地/远程节点，对当前进程即时生效、无需重启，面板也提供一键“恢复默认节点”；
- **Starter（$7/月）**：支持持久磁盘 —— 将 `render.yaml` 中注释掉的 `disk` 段恢复并把 `plan` 改为 `starter` 即可；服务常驻不休眠，面板修改与 UI 导入账号持久生效；
- **Standard 及以上**：本地签名器与 Python 服务同容器运行更宽裕，适合多账号高频调用。

---

## 账号凭据 (Cookie) 获取与导入指引

本服务不会保存你的 Google 密码，仅通过 Web 端 Session 凭据进行 API 调用，且内置自动化 Cookie 续期轮换机制。

### 步骤 1：获取 AI Studio Cookie

1. 在电脑或手机浏览器中访问并登录 [Google AI Studio](https://aistudio.google.com/)，确保能够正常进入对话工作台；
2. 安装浏览器扩展 **Cookie-Editor**（可在 Chrome 应用商店、Edge 扩展中心或 Firefox 附加组件中心免费安装）；
3. 在 `aistudio.google.com` 标签页下，点击浏览器的 Cookie-Editor 图标；
4. 点击右下角 **Export** 按钮，选择 **Export as JSON**（亦可选择 Export as Header）；数据将自动复制到剪贴板。

### 步骤 2：导入至管理控制台

1. 访问本服务 Web 控制台（默认 `http://localhost:8080`）；
2. 点击左侧导航栏的 **「账号管理」**；
3. 点击右上角 **「+ 导入凭据」** 按钮；
4. 将剪贴板中的 Cookie 数据直接粘贴到输入框中，点击 **「确认导入」**；
5. 系统将自动完成连通性校验，并自动递归探测多登子账号（`u/0`, `u/1`...）完成多身份入库！

### 方式三：环境变量注入（无磁盘 PaaS / 容器部署）

无持久磁盘的平台（Render Free、Railway 重启、K8s 无状态 Pod 等）重新部署后账号会丢失，可通过环境变量在启动时自动注入 Cookie，与 Web 导入完全等价（同样享受自动轮换、探活与多登子账号发现）：

```bash
# docker-compose.yml 示例：多账号 + 每账号独立代理
environment:
  - AISTUDIO_COOKIES=|
      socks5://user:pass@proxy-a.example.com:1080+SID=xxx; HSID=yyy; __Secure-3PSID=zzz
      SID=aaa; HSID=bbb; __Secure-3PSID=ccc
      SID=ddd; HSID=eee+http://proxy-b.example.com:8080
```

**单条格式（三种等价，`+` 为分隔符）：**

| 格式 | 示例 | 说明 |
| --- | --- | --- |
| `代理+Cookie` | `socks5://127.0.0.1:1080+SID=xxx; HSID=yyy` | 代理在前 |
| `Cookie` | `SID=xxx; HSID=yyy` | 不带代理，走全局 `AISTUDIO_PROXY` |
| `Cookie+代理` | `SID=xxx; HSID=yyy+http://10.0.0.1:7890` | 代理在后 |

- 代理支持 `http://` / `https://` / `socks5://` 完整地址，也支持 `127.0.0.1:7890` 简写（自动补全为 HTTP 代理）；
- Cookie 内容支持任意常见格式：请求头 KV、JSON 数组（Cookie-Editor 导出）、Netscape 文本；
- 绑定了独立代理的账号，其请求、Cookie 轮换、探活全部走该代理出口；
- 同一账号重启后重复注入不会产生副本（按会话锚点确定性去重），从环境变量中移除则自动清理；
- 多行环境变量不便时可改用编号变量（`AISTUDIO_COOKIES_1`、`AISTUDIO_COOKIES_2` ...）或 `AISTUDIO_COOKIES_FILE` 文件注入；单行内多条亦可用字面 `\n` 分隔。

---

## 第三方客户端配置指引 (NextChat / Cherry Studio / One API 等)

本服务同时提供三种协议入口，任选其一：

| 协议 | 接口 | 适用客户端 |
|---|---|---|
| **Google Gemini（原生）** | `http://localhost:8080`（需版本号时填 `http://localhost:8080/v1beta`） | google-genai SDK 及所有 Gemini 协议客户端 |
| **OpenAI 兼容** | `http://localhost:8080/v1` | NextChat / Cherry Studio / one-api / LobeChat 等选择「OpenAI」提供商的客户端 |
| **Anthropic Claude 兼容** | `http://localhost:8080`（Claude 客户端的 Base URL 无需 `/v1` 后缀，SDK 会自动拼接） | Claude Code / Anthropic SDK / Claude 协议客户端 |

| 配置项目 | 填写内容 | 说明 |
|---|---|---|
| **模型提供商 (Provider)** | Google Gemini / OpenAI / Claude 任选 | 三种协议共享同一账号池与模型目录 |
| **API Base URL / 代理地址** | 见上表 | — |
| **API Key / 密钥** | 任意字符串 (如 `aistudio-api`) | 若未在服务端 `config.yaml` 中配置 `api_keys`，客户端填写任意非空字符即可；OpenAI 客户端用 `Authorization: Bearer`，Claude 客户端用 `x-api-key`，均受同一套 Key 鉴权 |
| **支持的模型名称** | OpenAI/Claude 渠道直接填 Gemini 模型名（如 `gemini-3.7-flash`）；填 `gpt-*` / `o1*` / `claude-*` 等家族名时会自动映射到 `compat` 配置的默认模型 | 支持 `GET /v1/models` 查询（OpenAI 与 Claude 双格式自动识别） |

## 配置与环境变量

服务启动后可访问 `http://localhost:8080` 进入 Web 控制台完成可视化管理。

### 环境变量支持

| 变量名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | API 服务监听端口 |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | Web 控制台访问密码（亦可设置 `AISTUDIO_ADMIN_PASSWORD`）。仅配置 API Key 未设密码时，面板改由任意有效 API Key 登录且 Key 明文脱敏展示；两者均未配置时启动会自动生成随机管理密码并打印到日志 |
| `AISTUDIO_API_KEYS` | string | `""` | API 调用鉴权 Key，多个用逗号/换行分隔或 JSON 数组（如 `sk-aaa,sk-bbb`）；亦可单数 `AISTUDIO_API_KEY` 注入单个。与 config.yaml 中配置的 Key 合并生效，无磁盘部署重启后自动恢复 |
| `AISTUDIO_ALLOW_NO_AUTH` | bool | `false` | 显式声明在可信内网完全开放（免鉴权）。未配置任何凭据时默认自动生成随机管理密码保护面板；确需裸奔模式必须显式设置此变量（1=开启） |
| `AISTUDIO_PROXY` | string | `""` | 出口代理地址，支持 `http://`、`https://` 或 `socks5://` |
| `AISTUDIO_COOKIES` | string | `""` | 启动时注入的账号 Cookie，多条用换行分隔，单条支持 `代理+Cookie` / `Cookie` / `Cookie+代理` 三种格式（详见下方[环境变量注入账号](#环境变量注入账号无磁盘部署)） |
| `AISTUDIO_COOKIES_<N>` | string | `""` | 编号变量注入（`AISTUDIO_COOKIES_1`、`AISTUDIO_COOKIES_2` ...），适合无法传入多行环境变量的平台，与 `AISTUDIO_COOKIES` 可同时使用 |
| `AISTUDIO_COOKIES_FILE` | string | `""` | Cookie 注入文件路径（内容格式与 `AISTUDIO_COOKIES` 相同，支持多行文本或 JSON 字符串数组），适合超出环境变量长度限制的批量凭据 |
| `AISTUDIO_DATA_DIR` | string | `data/` | 主数据持久化根目录（Docker 下为 `/app/data`） |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | 账号持久化凭据目录 |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | 模型调用与 Token 使用量统计持久化文件 |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | 账号调度与 429 冷却状态持久化文件 |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | 模型规则与默认工具配置文件路径 |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | 是否持久化模型调用与 Token 统计（1=开启，0=关闭） |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | 是否持久化账号 429 冷却与调度状态（1=开启，0=关闭） |
| `AISTUDIO_PROOF_SERVER_URL` | string | `https://waa.spinsnow.fun` | 签名服务器地址覆盖项；推荐在 `config.yaml` 的 `waa.servers` 中统一配置各节点及其特征 User-Agent 并手动切换 |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.7-flash` | 默认文本模型 |
| `AISTUDIO_TIMEZONE` | string | 自动探测 | Wire 协议交互时区（默认 `America/Los_Angeles`） |
| `AISTUDIO_ANTI_TRUNCATION` | bool | `false` | 强制覆盖 `config.yaml` 中 `anti_truncation.enabled` 全局开关（1=开启，0=关闭，留空跟随配置文件） |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | 是否在终端转储每次 API 交互报文详情（支持 `DEBUG=1` 触发） |
| `AISTUDIO_DUMP_FILE` | bool | `false` | 是否将请求与响应报文详情落盘保存 |
| `AISTUDIO_DUMP_DIR` | string | `data/dumps/` | 报文转储保存目录 |
| `AISTUDIO_REQUEST_HISTORY` | bool | `true` | 是否持久化请求元数据（不保存正文） |
| `AISTUDIO_HISTORY_DAYS` / `AISTUDIO_HISTORY_MAX_ROWS` | int | `14` / `100000` | 请求历史保留天数和最大记录数 |
| `AISTUDIO_MAX_REQUEST_BYTES` | int | `33554432` | 单个模型请求正文上限（字节） |
| `AISTUDIO_MAX_CONCURRENT_REQUESTS` | int | `50` | 同时处理的模型请求上限，超出时返回 HTTP 429 |
| `AISTUDIO_REQUEST_TIMEOUT` | int | `600` | 模型请求总超时（秒）；流式响应从请求开始计时 |
| `AISTUDIO_DUMP_MAX_BYTES` | int | `1048576` | 详细转储时单个请求体或响应体的采集上限（字节） |
| `AISTUDIO_DUMP_MAX_FILES` | int | `1000` | 详细转储文件最多保留数量 |
| `AISTUDIO_DUMP_MAX_AGE_DAYS` | int | `7` | 详细转储文件最多保留天数 |
| `AISTUDIO_DUMP_MAX_TOTAL_BYTES` | int | `268435456` | 详细转储目录最大总容量（默认 256 MiB） |
| `AISTUDIO_SIGNER_SECRET` / `AISTUDIO_SIGNER_SECRET_URL` | string | `""` | 为对应签名节点配置 Bearer 共享密钥；仅发给匹配 URL 的节点 |

#### 本地 WAA 签名器（waa-signer）环境变量

| 变量名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `SIGNER_SHARED_SECRET` | string | `""` | 签名器 `/v1/*` 端点的 Bearer 共享密钥（常量时间比较）。跨网络部署签名器时**必须**设置；检测到 `PORT` 注入（绑定 0.0.0.0）而未设置时签名器将拒绝启动 |
| `LISTEN_ADDR` | string | `127.0.0.1:8081` | 签名器监听地址；未设置且容器平台注入 `PORT` 时会绑定 `0.0.0.0`（此时强制要求 `SIGNER_SHARED_SECRET`） |
| `WAA_MAX_WORKERS` | int | `32` | 签名器同时保活的最大 Worker 数（每个 Worker 持有一个 SpiderMonkey VM 与连接池）；满额时优先回收空闲 Worker，全部忙碌则拒绝新请求 |
| `WAA_IDLE_TTL_MINUTES` | int | `30` | Worker 空闲回收阈值（分钟），后台每分钟扫描一次，超时 Worker 被关闭释放内存 |

> 安全说明：`/v1/rpc/send` 的目标 URL 已限制为 Google 域白名单（`*.google.com` / `*.googleapis.com`），防止签名器被用作 SSRF 中继；`/health` 保持免鉴权（仅暴露 Worker 计数）供探活。
---

### 抗截断传输 (Anti-Truncation Transport)

针对上游正文文本通道偶发的吞字 / 流式截断问题，提供“工具调用参数运货”传输层：请求侧自动追加一个仅用于运货的合成函数声明（默认 `router_emit`），并指示模型把全部正文写入该函数调用的 `content` 字符串参数（上游对工具调用参数不做流式截断）；响应侧实时把参数内容还原为普通正文，客户端无感知。

```yaml
# config.yaml
anti_truncation:
  enabled: true          # 全局开关（默认关闭）
  tool_name: router_emit # 合成工具名，与客户端工具名冲突时自动改用随机名
  model_variants:        # 模型变体模式（也可在控制台「系统设置 → 抗截断传输」卡片配置）
    enabled: true        # 开启后为选中模型暴露两个后缀变体
    models:              # 选中的基础模型列表
      - gemini-3.7-flash
```

- **按请求覆盖**：请求头 `X-Anti-Truncation: 1` 强制开启、`0` 强制关闭，优先级高于全局配置与模型变体。
- **全局开启**：`enabled: true` 时所有模型请求默认启用抗截断。
- **模型变体（按模型选择）**：`model_variants.enabled: true` 后，选中模型在 `GET /v1beta/models` 中额外暴露两个变体，选择变体模型即启用抗截断，原模型行为不变：
  - `<model>-at-stream` —— 流式抗截断：SSE 端点走流式增量还原，客户端逐段看到正文；
  - `<model>-at` —— 非流式抗截断：任意端点改走缓冲（非流式）上游，整体还原后单条完整响应下发（SSE 端点打包为一条事件，更抗截断但无逐段体验）。
- **优雅降级**：模型未按指令走工具通道时，纯文本原样透传，不破坏请求。
- **自动旁路**（与上游实现一致的安全条件）：工具历史轮次、结构化输出（`response_schema` / JSON `response_mime_type`）、停止序列、图片输出模型、内置服务端工具（如 Google Search）、强制 `tool_choice`（ANY）或 NONE 模式，一律跳过注入。
- **流式兼容**：分片到达的参数字符串由增量 JSON 识别器实时解码为 `body` 增量事件，客户端仍可逐段看到正文；同时自适应“全量重发 / 增量追加”两种上游行为，避免重复正文。

> 机制移植自 [ken050210/vertex-streaming-anti-truncation](https://github.com/ken050210/vertex-streaming-anti-truncation)，并适配本项目 AI Studio JSPB 线路协议（MakerSuite 协议无 `tool_choice` 强制字段，故依赖指令遵循 + 优雅降级）。

### OpenAI / Claude 协议兼容层 (compat)

`/v1/chat/completions` 与 `/v1/messages` 两个兼容端点的模型名映射配置（支持 `config.yaml` 热重载）：

```yaml
# config.yaml
compat:
  openai:
    default_model: gemini-3.7-flash # 客户端填 gpt-* / o1* / chatgpt-* 等家族名时回退到的模型
    model_mapping: {}               # 自定义映射，支持精确与最长前缀匹配，例: gpt-4o: gemini-3.7-flash
  claude:
    default_model: gemini-3.7-flash # 客户端填 claude-* 家族名时回退到的模型
    model_mapping: {}               # 例: claude-opus: gemini-4-argon（可命中 claude-opus-4-1）
```

- 直接填 Gemini 模型名（含 `-at` / `-at-stream` 抗截断变体）时原样透传，不经过映射；
- 兼容层与原生 `/v1beta` 共享同一账号池、模型目录、鉴权 Key 与付费模型门控；
- 多模态图片 / 音频 / 文档：远程 URL 由服务端代下载，内联 base64 与远程下载统一受 20MB 原始字节上限（超限返回 400）；`count_tokens` 估算不计入 base64 媒体数据；
- 工具调用只转换自定义函数工具（OpenAI `type: function` / Claude 无 type 或 `type: custom`），`web_search` 等服务端工具类型会被跳过而非误声明为函数。

## API 调用示例

支持以下鉴权方式：
- Query 参数：`?key=YOUR_API_KEY`
- 请求头：`x-goog-api-key: YOUR_API_KEY`、`x-api-key: YOUR_API_KEY` 或 `Authorization: Bearer YOUR_API_KEY`

### cURL 调用

<details>
<summary><b>1. 查询模型列表</b></summary>

```bash
curl http://localhost:8080/v1beta/models \
  -H "x-goog-api-key: your-api-key"
```
</details>

<details>
<summary><b>2. 文本生成 (Non-streaming)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:generateContent \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "用简短的一句话介绍量子计算。"}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>3. 流式生成 (Server-Sent Events)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "介绍一下 Python 协程的工作原理。"}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>4. OpenAI 兼容格式（/v1/chat/completions）</b></summary>

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "用简短的一句话介绍量子计算。"}],
    "stream": true
  }'
```

- `model` 直接填 Gemini 模型名；填 `gpt-4o` 等家族名会自动映射到 `config.yaml` 中 `compat.openai.default_model`；
- 流式返回标准 `chat.completion.chunk` 序列（含 `data: [DONE]`），思考链走 `reasoning_content` 增量字段；
- 工具调用（`tools` / `tool_choice`）、`response_format`（JSON 模式）、`reasoning_effort`、图片输入（data URL 或远程 URL）均已支持。
</details>

<details>
<summary><b>5. Claude 兼容格式（/v1/messages）</b></summary>

```bash
curl http://localhost:8080/v1/messages \
  -H "x-api-key: your-api-key" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "用简短的一句话介绍量子计算。"}],
    "stream": true
  }'
```

- `model` 填 `claude-*` 家族名会自动映射到 `config.yaml` 中 `compat.claude.default_model`（也可直接填 Gemini 模型名）；
- 流式返回完整 Claude 事件序列（`message_start` / `content_block_delta` / `message_delta` / `message_stop`），思考链走 thinking 块；
- 工具（`tools` / `tool_choice`）、`thinking.budget_tokens`（自动换算为 Gemini 思考等级）、图片 / PDF 块、`stop_sequences` 均已支持；
- `POST /v1/messages/count_tokens` 提供粗略 token 估算，`GET /v1/models` 在携带 `anthropic-version` 头时返回 Claude 格式。
</details>

---

### Python SDK (google-genai)

使用 Google 官方 `google-genai` SDK 直连本代理服务：

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 1. 流式响应
response = client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="解释一下什么是反应式编程？",
)
for chunk in response:
    print(chunk.text, end="", flush=True)

# 2. 非流式响应
result = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="用 Python 写一个快速排序算法。",
)
print(result.text)
```

---

### 工具调用 (Function Calling)

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 定义工具声明
weather_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_current_weather",
            description="获取指定地点的实时天气状况",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "location": types.Schema(
                        type="STRING",
                        description="城市或地区名称，例如 'Beijing' 或 'Tokyo'",
                    ),
                    "unit": types.Schema(
                        type="STRING",
                        enum=["celsius", "fahrenheit"],
                        description="温度单位",
                    ),
                },
                required=["location"],
            ),
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="今天杭州天气怎么样？",
    config=types.GenerateContentConfig(tools=[weather_tool]),
)

if response.function_calls:
    for call in response.function_calls:
        print(f"触发工具调用: {call.name}")
        print(f"调用参数: {call.args}")
```

---

## Web 管理控制台

系统内置静态打包的 Web 前端管理控制台（访问 `http://localhost:8080`）：

- **仪表盘 (Dashboard)**：按最终调用结果统计成功率，支持时间范围、趋势、模型/账号/密钥分布、重试率及首包/总耗时 P50/P95。
- **账号管理 (Accounts)**：支持 Cookie 导入、多账号探测、批量启停与探测、账号备注和详情、模型用量、当前冷却状态及最近调用查询。
- **运行日志 (Logs)**：默认展示模型请求及关联签名、切号和网络异常，可切换系统日志。SSE 实时推送支持级别、模型、账号和 Request ID 筛选；实时缓冲保留最近 2000 条。
- **请求历史 (Requests)**：展示执行中的调用及每次调用的最终记录；支持失败、慢请求、有重试等筛选。详情包含请求与实际思考配置、首包/首次思考/首段回复/总耗时、各类 Token、时间线、上游尝试和关联日志，并支持脱敏正文查看、详情链接及 JSON 导出。
- **代理诊断 (Proxies)**：显示全局/账号级代理生效来源，可测试当前或草稿代理到 Google、AI Studio 与出口 IP；可在线更新全局代理。请求历史只显示脱敏代理地址。
- **历史日志**：JSONL 后台线程轮转写入 `data/logs/`，最多约 30 MiB，按请求 ID、级别和关键字查询，并保留实时日志页面。
- **服务保护**：请求正文默认限制 32 MiB、最大并发 50、总超时 600 秒。阈值均可通过环境变量调整。
- **系统设置 (Settings)**：在线编辑 `config.yaml`（支持配置 profiles 与 models 默认规则），保存后服务端即时热重载。
- **访问鉴权**：管理 API 与控制台受管理密码或有效 API Key 保护。未配置凭据时服务会生成随机管理密码；如仅在可信内网开放，可显式设置 `AISTUDIO_ALLOW_NO_AUTH=1`。

统计口径、时间定义与日志接口兼容说明见 [调用统计与请求排查](docs/OBSERVABILITY.md)。

### 失败排查：请求失败 / 空回看哪里

请求失败或"空回"时**错误原因会直接透传到客户端回复里**（形如 `[aistudio-api 请求失败 502 UNAVAILABLE] HTTP request failed: ConnectError ...`），无需登录服务器即可定位：

- `[aistudio-api 请求失败 ...]` —— 上游/签名/代理失败的具体原因（502 = 传输或签名失败、403 = 账号风控、429 = 配额耗尽）；
- `[aistudio-api 空回诊断] ...` —— 上游返回 200 但零内容（多为账号风控拦截）。

更完整的上下文在 **Web 控制台 → 运行日志 (Logs)**：传输与签名失败日志会注明所用代理地址与签名服务地址（凭据自动脱敏）；PaaS 部署也可查看平台日志流（stdout 同步输出）。

常见根因速查：

| 现象 | 典型原因 |
| --- | --- |
| `ConnectError / ConnectTimeout`（502） | `AISTUDIO_PROXY` 指向了部署环境无法访问的代理（如把本地 SOCKS5 代理变量带上了 PaaS）；删除该环境变量改直连即可 |
| `Remote proof minting failed (server=...)` | WAA 签名服务不可达或拒绝：在 设置 → 签名服务 切换节点（本地/远程面板切换即时生效） |
| 403 PERMISSION_DENIED | 账号级风控：需在 Google 账号完成手机绑定/年龄验证后恢复 |
| 429 RESOURCE_EXHAUSTED | 账号当日配额耗尽，多账号轮换或等待 00:00 PST 重置 |

---

## 开发与测试

前端源码位于 `web/`，采用 Vue 3 + TypeScript + Vite + Tailwind CSS：

```bash
# 启动前端开发调试服务
cd web
bun install
bun run dev          # 启动开发服务（localhost:3000，代理至 8080）
bun run type-check   # 执行 TypeScript 类型校验
bun run lint         # 执行 ESLint 代码规范检查
bun run build        # 生产构建，产物输出至 src/aistudio_api/static
```

Python 端质量检查与测试：

```bash
# 执行代码风格与 Lint 检查
uv run ruff check .
# 执行静态类型检查
bun x pyright

# 执行单元测试套件
uv run pytest
```

---

## 借物表与致谢 (Credits & Acknowledgments)

本项目在逆向工程、签名算法与协议实现过程中，参考并借鉴了以下优秀的开源项目与社区成果，在此致以诚挚的感谢：

- **[Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API)**：提供了嵌入式 SpiderMonkey 152 运行时与 Firefox 152 DOM / WebIDL 宿主现场实现，以及基于 uTLS 的网络栈设计，`waa-signer` 服务基于其核心逻辑适配抽离。
- **[Lianues/aistudio-pg-bridge](https://github.com/Lianues/aistudio-pg-bridge)**：开创了脱离本地浏览器的轻量化 WAA 证明签名服务思路，并确立了 `/v1/proof/mint` 接口规范。
- **[ken050210/vertex-streaming-anti-truncation](https://github.com/ken050210/vertex-streaming-anti-truncation)**：抗截断传输（工具调用参数运货）的核心机制与流式增量 JSON 识别器来源，本项目将其适配至 AI Studio JSPB 线路协议。
- **[spinsnow](https://waa.spinsnow.fun)**：提供了公开稳定的 Node.js VM WAA 签名中继服务与环境参考。
- **[Camoufox](https://github.com/daijro/camoufox)** 与 **[go-browserforge](https://github.com/brainplusplus/go-browserforge)**：提供了真实浏览器环境指纹数据与反指纹追踪生成算法。

---

## 开源协议

本项目基于 [MIT License](./LICENSE) 许可协议开源。
