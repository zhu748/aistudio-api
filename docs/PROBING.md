# 本机协议探测

`scripts/probe_aistudio.py` 对运行中的 aistudio-api 发起真实模型请求，支持
Python 3.11+。脚本内有 uv 依赖声明，可复制为单文件运行，不依赖项目内部模块。
它测试网关协议，不直接重放 Google 内部 RPC，也不会导入或修改账号。

## 准备

1. 在能连接 Google 出口代理的本机或服务器上启动项目：

   ```bash
   uv sync
   uv run python main.py server --port 8080
   ```

2. 在 `http://127.0.0.1:8080` 管理界面导入自己的 Cookie，配置该账号的
   SOCKS5 出口，并确认启用的账号就是准备测试的账号。多账号轮换可能使
   不同请求使用不同账号，报告不采集账号标识。
3. 在网关设置中选择已经安装、可运行的本地签名器 `local_spidermonkey`，
   可参考 `waa-signer/README.md`。仓库默认选择远程签名器；Cookie 是否发给
   签名服务取决于网关配置，脚本本身不读取 Google Cookie。
4. 如果网关设置了 API key，在本机环境变量 `AISTUDIO_API_KEY` 中设置它。
   没有配置 API key 时，脚本使用非空测试值。不要把 Google Cookie 填到此变量。

## 运行

先针对一个协议执行两个生成请求：

```bash
uv run scripts/probe_aistudio.py --model gemini-3.5-flash-lite --protocols gemini --out probe-gemini.json
```

完整运行四种协议：

```bash
uv run scripts/probe_aistudio.py --model gemini-3.5-flash-lite --full --out probe-report.json
```

将模型名换成本机账号实际可用的 ID。`--base-url` 默认为
`http://127.0.0.1:8080`，填网关根地址，不带 `/v1`，不是 SOCKS5 地址。
脚本到网关的请求不使用系统代理；Google 出口由网关的账号代理设置控制。

| 模式 | 生成请求数上限（四种协议） | 覆盖 |
| --- | --- | --- |
| 默认 | 8 | 普通文本、SSE 流式、结束状态及 token 用量 |
| `--full` | 24 | 默认内容，加 thinking、JSON、函数调用及工具结果回传 |

两种模式都会额外请求一次模型目录。脚本逐个请求、不自行重试；网关内部的
重试和账号轮换可能使上游请求数高于上述数字。鉴权拒绝、限流和部分网关错误
会跳过剩余探测；功能级 400 会保留失败结果并继续检查其他功能。
工具探测只回传已知加法 `2+5=7`，不执行模型提出的任意函数或代码。

## 查看结果

报告包含检查结果、HTTP 状态、响应体首批字节/完整请求耗时、token 用量和
内容长度，不保存原始内容、签名、响应 ID、请求头、Cookie、邮箱或代理地址。
把 `probe-report.json` 发回即可继续分析。退出码 0 表示全部执行的检查通过，
1 表示至少有一项失败；连接或鉴权失败也会生成报告。

- `first_data_ms` 是首批响应体字节耗时，可能只是 `response.created` 等元数据，
  不代表模型首字时间，非流式结果也不能用于测量模型首字。
- thinking 请求检查参数路径和回答结果，同时记录思考文本长度；长度为零
  不能单凭一次探测证明模型没有进行思考。
- Claude 的 JSON 探测是提示词约束；当前网关未提供 Claude 原生结构化输出
  参数，不能将通过解释为服务端强制 Schema。
- 模型目录可能来自缓存，通过目录请求不能证明 Google 登录态健康。
- 本脚本不覆盖图像/音视频生成、Live/WebSocket 或原始 WAA 报文；这些需要
  单独的能力探测或浏览器抓包。
