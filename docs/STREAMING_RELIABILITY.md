# 长请求与断流处理

## 这次修复解决什么

流式开关开启后，工具参数、签名、抗截断缓冲结果仍可能长时间没有任何正文。旧实现仅在 OpenAI 等适配器开始时发送初始化事件，此后等待上游时没有心跳。反向代理或客户端可能因持续没有字节而断开连接。

OpenAI / Claude 流式响应现在立即发送 `: keepalive\n\n`，随后每 15 秒发送一次。原生 Gemini 使用空 SSE 块 `\n\n`，兼容不识别注释行的 google-genai SDK。心跳位于协议转换之后的 HTTP 输出层，因此签名、403 恢复、等待完整工具参数、上游缓冲、重试退避期间都生效。心跳不包含 `data:`，不会成为模型正文、工具参数或 Token 用量；工具参数仍完整交付，不会提前执行残缺参数。

非流式生成也默认保活。前 15 秒保留正常响应行为；超过 15 秒后发送 `application/json` 响应和合法 JSON 空白，此后每 15 秒发送空白，最终附加一个完整 JSON 对象。标准 JSON 解析器会忽略这些空白。覆盖 `/v1/chat/completions`、`/v1/responses`、`/v1/messages` 和 Gemini `:generateContent`；不改变 countTokens 和管理接口。

**HTTP 协议限制**：非流式一旦发送空白，HTTP 200 已经提交；迟到的 403、502、504 将通过对应协议的 `error` JSON 返回，调用方需要检查错误体。鉴权、校验和在首个保活前完成的错误保留原始 HTTP 错误状态。确实依赖 HTTP 错误状态的特殊客户端可设置 `AISTUDIO_NONSTREAM_KEEPALIVE=0`，但默认保持开启。

## 网络故障恢复与资源释放

- 未输出正文/思考/工具调用时，传输故障及上游 HTTP 502/503/504 最多额外尝试两次，基础退避 0.5 秒、1 秒并附带 ±20% 随机抖动；也受账号调度的 `max_retries` 和整请求总超时限制。优先使用可用备用账号，没有备用账号时仍可重试当前账号。重新准备请求，不继续复用失败尝试的签名快照。重试耗尽时保留上游 502/503/504 状态，不把权限、参数、配额错误混入这类重试。
- 已向下游输出生成内容后不重放生成，避免重复正文、重复工具调用和前后内容拼接不一致。失败按原协议返回结构化错误。
- 心跳不会取消正在进行的上游读取；不会建立无界响应队列。下行发送维持背压，单次写入超时默认 30 秒。
- 客户端/代理实际断开后停止生成并关闭迭代器，非流式后台生成任务也会被取消和回收。日志明确记录下游断开，而不是仅显示不明来源的取消堆栈。
- 403 继续使用既有重放、重新签名、切号与签名健康检查逻辑。保活不能解决 Cookie 权限、账号风控或 WAA 签名拒绝。

兼容接口的远程图片/文档下载与请求转换也纳入保活：准备阶段超过 15 秒便开始发送对应协议心跳。快速校验错误仍返回原 HTTP 状态；开始 SSE 保活后的校验失败或超时通过协议错误事件返回。正常生成开始后继续使用原适配器的完成状态与错误处理。

响应写入和收尾分别记录：完整 JSON 或 OpenAI / Claude 兼容接口的终止事件已经交给下游发送后，收尾超时仅结束 HTTP 响应，不追加第二个错误 JSON，也不会在 `[DONE]`、完成或失败事件之后再追加超时错误。错误回调没有剩余事件时也会结束 HTTP 响应，避免客户端继续等待。若总时限恰好打断一条正在写入的帧，则关闭连接并记录取消，避免把错误拼接到可能只送出一半的 JSON/SSE 中。ASGI 2.4 的心跳写入失败同样记为下游断开。

缓冲与流式上游响应都由传输层显式持有并关闭，取消时的关闭操作有最多 5 秒保护时间。保护同时覆盖 HTTPX 读到 EOF 时的自动关闭，避免响应已标记关闭、实际连接清理却被取消的竞态；父任务重复取消时仍等待受时限约束的清理任务结束，再传递取消。非流式等待完整上游响应时取消，也不会因尚未拿到响应对象而跳过清理。非流式晚到的 429 保留限流统计分类。

## Render + Cloudflare + 酒馆

重新部署包含本次代码的镜像。默认值已经生效，Render Blueprint 同步声明以下变量；已有 Render 服务也可以在 Environment 核对：

```dotenv
AISTUDIO_STREAM_HEARTBEAT_INTERVAL=15
AISTUDIO_STREAM_SEND_TIMEOUT=30
AISTUDIO_NONSTREAM_KEEPALIVE=1
AISTUDIO_UPSTREAM_READ_TIMEOUT=300
AISTUDIO_REQUEST_TIMEOUT=600
```

酒馆保持开启流式传输。即便工具参数只在最后一次返回，等待期间也会收到心跳。酒馆、转发插件、Cloudflare Worker 或其他中转若另外设置了固定总时限，需要同步调整；心跳只能缓解空闲读超时，无法延长第三方强制总时限。

`AISTUDIO_UPSTREAM_READ_TIMEOUT` 控制网关到 Google/TURN 代理的连续无数据等待，原来固定为 120 秒，现在默认 300 秒；连接超时仍为 15 秒，上传为 120 秒，连接池排队为 30 秒。`AISTUDIO_REQUEST_TIMEOUT` 则限制包含签名、所有重试、生成在内的整个请求，默认 600 秒。总超时到期会返回对应协议的错误；不会因心跳无限占用资源。

响应带 `Cache-Control: no-cache, no-transform` 和 `X-Accel-Buffering: no`。自定义 Cloudflare Worker、缓存规则、Nginx 或酒馆中转应逐块转发，不要先 `response.text()` / `response.json()` 读完整响应后再转发。若使用 Nginx，可在对应 API location 配置：

```nginx
proxy_buffering off;
proxy_cache off;
gzip off;
proxy_read_timeout 650s;
proxy_send_timeout 650s;
```

## 如何定位仍然出现的中断

1. 对同一个长工具请求，分别使用 Cloudflare 域名和 Render 原始 `*.onrender.com` 地址测试。仅 Cloudflare 路径失败时重点检查该层的缓存、中转和超时规则；两条路径都失败时再检查酒馆、Render、出站 TURN 代理与上游。
2. 用 `curl -N` 或客户端原始网络日志观察：SSE 每约 15 秒有注释，非流式每约 15 秒有空白；正文首字时间与心跳首字节时间是两个指标。
3. 请求历史的 `Downstream disconnected (client or reverse proxy)` 表示网关收到下游断开通知，不能仅靠这条记录区分用户主动取消、客户端超时或 Cloudflare 关闭。`Request exceeded total timeout` 表示本网关总时限；上游 `ReadTimeout` / `RemoteProtocolError` 则指向出站连接。

示例日志中的前三次 HTTP 403 是真实的权限/证明拒绝。第 4 次等待 77 秒后随 ASGI cancel scope 取消，表示当时生成被取消；单凭该堆栈无法证明网络丢包，也不能确定是 Cloudflare 的哪条时限。缺失心跳是代码中可以确认并修复的问题。

心跳不能恢复已经失效的 TCP 连接。已交付部分内容的 POST 请求也没有通用的断点续传协议；本次不承诺跨断网继续原生成。重新发起请求可能产生新的推理用量。
