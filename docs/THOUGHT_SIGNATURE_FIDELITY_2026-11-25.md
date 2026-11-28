# thought_signature 思考链保真:两轮审计与修复(2026-11-25)

> 历史记录：下文描述当时版本的签名处理，不是当前网关对丢失字段的恢复保证。客户端应按[多轮工具调用](../README.md#多轮工具调用)回传完整历史。

## 背景

上游 MakerSuite 对回传历史的签名强制校验(见 `TOOL_HISTORY_LOSS_2026-11-14.md` /
`WIRE_SPECIFICATION.md` §6.5):

- **functionCall part**:并行批次首个调用承载 `thoughtSignature`，后续调用
  通常不带签名。缺少必需签名或回传上游无法校验的签名均可能返回 400；
- **纯文本轮**:思考摘要 + 文本 + **尾部空文本签名 part**(签名不在
  thought part 本体);
- **模型 FC 轮**:思考摘要无签名,签名只挂在 FC part 上。

网关对工具调用轮已有四重恢复(extra_content 扩展字段 / 持久化签名缓存 /
model_turns 原始 wire 回放 / SKIP 哨兵字面量),Claude Code 等客户端接入时
工具调用签名不丢失。本两轮审计聚焦**思考链(thinking)签名**在 Claude /
OpenAI Chat / OpenAI Responses 三个协议边界的保真。

## 第一轮:三处丢失点(实测复现后修复)

| 丢失点 | 位置 | 根因 |
|---|---|---|
| 非流式尾部签名载体被丢弃 | `build_claude_message` | 循环只处理非空文本 part,空文本纯签名 part 落入任何分支 |
| 流式迟到签名帧被丢弃 | `claude_stream_adapter` | 签名晚于正文到达时 thinking 块已带占位签名关闭,独立签名分支仅当当前打开块为 thinking 时才记录 |
| URL-safe / 缺 padding 签名被校验层拒绝 | `valid_thought_signature` | 校验层严格标准 Base64,与 wire 编码层 `_normalize_standard_base64` 的归一化语义自相矛盾 |

修复:

1. **非流式**:`build_claude_message` 新增空文本纯签名载体分支,记录尾部
   签名;停止词过滤后回填最后一个 thinking 块(当前为占位/空签名时才
   覆盖,`to_gemini_parts` 的互斥语义保持防御性)。
2. **流式**:发射器新增 `thinking_index` / `late_signature` 状态,
   `finish()` 在 `message_delta` 之前对已关闭的 thinking 块补发携带
   真实签名的 `signature_delta`。Anthropic SDK(含 Claude Code)对
   `signature_delta` 按块索引**赋值**且不校验块是否已 stop——后到者
   覆盖先前下发的占位值,不依赖客户端宽容实现。
3. **校验**:`valid_thought_signature` 标准校验失败后换写标准字母表并
   补齐 padding 二次校验(len%4==1 直接拒绝),与 wire 编码层语义对齐。

## 第二轮:OpenAI / Responses 思考链回传与签名载体

Claude 协议 thinking 块自带 `signature` 字段,而 OpenAI 生态
(`reasoning_content` / Responses `reasoning` item)没有官方签名字段;
此前请求侧直接丢弃思考文本,响应侧也没有任何可回传的签名载体:

- **OpenAI 请求侧**:`OpenAIChatMessage` 新增
  `reasoning_content` / `reasoning`(字符串形态)/ `extra_content`
  字段;assistant 回传的思考文本转为 thought part,签名经消息级
  `extra_content.google.thought_signature` 精确回读(与 tool_calls 的
  载体形态一致)。
- **OpenAI 非流式响应**:尾部签名载体随消息级 `extra_content` 下发
  (仅在存在 `reasoning_content` 时——无思考文本的轮无绑定对象)。
- **OpenAI 流式响应**:thought part 附带签名与尾部独立签名帧统一记录,
  终结块的 `delta.extra_content` 下发(客户端合并 delta 后随消息回传)。
- **Responses 请求侧**:非空 `reasoning` item 的 summary 文本转
  thought part(此前整项静默丢弃,Codex CLI 等客户端重发的思考历史全部
  丢失);签名经 item 级 `extra_content` 精确回读,伪造值在规范化层被
  校验丢弃,不会触达上游。
- **Responses 响应侧**:reasoning item 关闭时携带
  `extra_content.google.thought_signature`(非流式 builder 与流式
  `output_item.done` 的 final item 同形态)。
- **thinking 签名缓存**(域层 `tool_signatures`):按思考摘要**全文
  哈希**精确匹配,与函数调用签名共用同一缓存实例与持久化文件(键经
  "thinking" 伪调用标识隔离,哈希键不落明文思考内容)。响应路径
  (非流式 `_iter_response_parts` / 流式 `_FrameSignatureRecorder` 收口)
  记住"全文+签名",规范化层 `_convert_part` 在 thought part 无有效签名
  时按全文回注——客户端丢失载体、model_turns 回放失配(账号轮换/重启)
  的窗口内思考链签名仍可恢复。FC part 的签名只属于调用本身,不入
  thinking 缓存(上游 FC 轮的思考摘要本就无签名)。

## 验证

- 端到端验证脚本(Claude 侧 8 项 / 第二轮 15 项断言,含修复前后对照);
- 回归测试:第一轮 5 个(test_compat_claude / test_unsigned_tool_history),
  第二轮 9 个(test_compat_openai / test_compat_responses /
  test_unsigned_tool_history);
- 全量单测 1604 项通过(沙箱 httpx 代理依赖导致的 3 项环境失败除外,
  经 git stash 对照确认与改动无关)。

## 边界与后续

- 原生 Gemini 接口首次按原样回放缓存未观测到的显式签名。上游明确返回
  `400 Corrupted thought signature` 且尚未发布流式响应内容时，网关最多
  重试一次。重试按 Google 的历史迁移约定，移除候选轮次不可用的思维签名，
  给并行批次首个调用使用 `skip_thought_signature_validator`，保留文本、
  思考摘要、工具调用及工具结果。已校验归属的历史轮次保持原有回放路径。
  完整生成后，网关在当前进程和作用域内缓存已接受的迁移，后续回传同一
  段历史时可直接续聊。 [Google Gemini 思维签名文档](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures#faqs)
- OpenAI 生态客户端是否原样回传 `extra_content` 取决于客户端实现;
  三层兜底(消息级载体 / thinking 缓存 / model_turns 回放)保证任一
  层命中即恢复,最坏退化为无签名思考历史(上游容忍,与 Claude 侧
  占位签名回传的既有行为一致), SKIP 哨兵仅适用于 functionCall。
- `_normalize_standard_base64` 对 <16 字符值不归一化(文件名豁免),
  真实签名远长于该阈值,无实际影响。
