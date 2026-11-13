# 2026-11-13 抓包复核与协议回放

本次用户提供的 HAR 共 101 个条目，其中成功的相关 POST RPC 包括两次
GenerateContent、两次 CountTokens、两次 GenerateTitle。仅分析生成与计数报文；
没有重放 CreatePrompt，也没有读取或修改账号内的已保存提示词。

## 报文证据

| 项目 | gemini-3.1-pro-preview | gemini-3.5-flash-lite |
| --- | --- | --- |
| GenerateContent HTTP 状态 | 200 | 200 |
| thinking 配置 `[3][16]` | `[1,null,null,3]`，HIGH | `[1,null,null,4]`，MINIMAL |
| 未知生成开关 `[3][13]` | 1 | null |
| 分片结构 | thinking → text → 空文本 + 签名 + STOP | text → text → 空文本 + 签名 + STOP |
| 最终输入 / 可见输出 / 思考 token | 3 / 11 / 185 | 2 / 15 / 0 |
| 最终总 token | 199 | 17 |
| CountTokens 总数 | 3 | 2 |

两个请求都带 Google Search 工具声明，但这不能证明本次生成实际调用了搜索。
思考配置是这两次请求的选定值，不能据此推断所有模型的默认值或完整能力。
最后一个分片的 `Part[14]` 是不透明 `thoughtSignature`，文本为空且没有
thought 标记；不能把它任意改挂到之前的 thinking 部分。

## 修复与回归

- 修复缓冲生成路径丢失末尾签名：Gemini JSON 响应保留独立空文本签名部分，
  已包含相同签名的图片或函数调用部分不重复追加。流式签名帧补齐模型与响应 ID。
- 修复 wire 解码后清空可选字段仍保留旧值的问题，涉及工具、系统指令、
  evergreen URI、请求标记、会话 token 和请求上下文；未知槽位保持不变。
- 修复混合部分中的文本丢失，覆盖函数调用、函数响应、代码执行与图片载荷。
- 加入脱敏 fixture 和 33 项回归测试：四种 HTTP 协议的流式/非流式文本、
  用量、终结状态；真实客户端 CountTokens 路径；任意小分片、UTF-8 字符、
  XSSI 前缀以及请求往返。网络及签名边界替换为 fixture，其余使用生产代码。

`tests/fixtures/har_2026_11_13_generations.json` 只保留数组结构、模型名和数值。
提示词、回复文本、签名、proof、响应 ID 与时区均替换为测试数据；不含请求头、
Cookie、邮箱、代理凭据或已保存提示词。原始 token 数值只用于测试计量映射，
不表示替换后的测试文本实际分词结果。

```bash
uv run pytest -q tests/unit/test_capture_protocols.py
```

## 验证范围

当前环境允许普通 HTTP(S)，但运行时网络策略的 TCP 出站为禁用状态。
通过用户指定 SOCKS5 代理访问 AI Studio 的连接在默认沙箱及沙箱外均失败，
未进入 Google 身份认证或推理阶段；没有向 Google 或第三方签名服务发送 Cookie。
因此以上是用户成功抓包的离线回放结论，本次没有新增实账号在线验证结果。

现有回归覆盖工具调用、结构化输出、错误和超时等转换行为，但这份 HAR
没有这些功能的成功调用，也没有图像、音频、视频生成的报文，无法据此宣称
它们已通过该账号的线上验证。跨协议的文本/末尾 Gemini 签名没有标准等价字段；
本次保真修复适用于原生 Gemini，函数调用签名仍使用兼容层已有的扩展字段。
进一步实测需先让环境能够连接指定 SOCKS5 代理，再使用本地签名器进行小规模探测。
