# harness 工具调用与思考签名链路审查（2026-11-26）

聚焦客户端 harness（Claude Code / Codex CLI / Cline 等编码代理）接入场景：
工具调用全链路与思考签名（thought signature）的往返保真，是否会在多轮
工具循环中丢失签名/思考历史、进而降低模型实际表现或触发上游 400。

## 审查方法

- 全链路静态审查：`routes_compat` → `compat_claude/compat_openai/
  responses_service`（请求/响应双侧）→ `stream_utils.coalesce_tool_frames`
  → `chat_service._convert_part`（规范化）→ `wire_types/wire_codec`
  （JSPB 编码）→ `model_turns`（整轮回放）→ `tool_signatures`（缓存）。
- 全链路动态追踪：构建两个离线追踪脚本模拟真实 harness 行为——
  Claude 侧按 anthropic-sdk-typescript MessageStream 累积器语义
  （signature_delta 按索引赋值、不校验块 stop 状态）重建最终消息后
  原样回传，含 extra_content 保留/剥离、冷缓存、并行调用、参数分片、
  迟到签名等 34 项断言；OpenAI/Responses 侧 21 项断言（reasoning_content
  往返、reasoning item 往返、终结块 delta 载体、缓存回注）。
- 与 `anti_truncation` 的 call_id 补填分支交叉印证分片形态的真实性。

## 核心结论：主链路健康

Claude Code 接入的思考签名与工具调用往返**全链路保真**（34 + 21 项
追踪断言全过），不降低模型实际表现：

| 链路环节 | 验证结果 |
|---|---|
| 流式 FC 轮：思考摘要 → thinking 块（占位签名）；FC → tool_use 块（extra_content 签名载体） | PASS |
| 纯文本轮：尾部空文本签名载体 → finish() 前补发 signature_delta 覆盖占位值 | PASS |
| 回传：thinking signature envelope 解包、extra_content 载体读取 | PASS |
| Claude Code TS 剥离 extra_content → 签名缓存按（call_id + 函数名 + 规范化参数）精确回注 | PASS |
| 冷缓存（重启/账号轮换）→ SKIP 哨兵字面量，调用与结果**不剥离** | PASS |
| 并行工具调用各自签名配对；同 id 参数分片合并；签名独立帧并入调用 | PASS |
| model_turns 整轮回放（可见内容摘要匹配、签名槽位零化、调用 ID 改写） | PASS |
| `max` effort → xhigh 顶格思考档、budget_tokens → 档位映射 | PASS |

## 修复 1（P1）：混合 id 分片链断裂

`coalesce_tool_frames` 遇到**首帧无 call_id、续帧才携带真实 id** 的参数
分片链（JSPB 渐进填充形态——`anti_truncation.handle_synthetic_call` 的
`call_state.call_id is None and call_id_str is not None` 补填分支证明该
形态真实存在）时，会把链拆成两个都不完整的 `_ToolCall` 双双作废：
无 id 首帧以合成 id 立链（分片无法独立解析），带 id 续帧按新调用立链
（分片同样不完整）——工具调用整体丢失；若全流无其他已投递内容甚至
整流报错（`Incomplete tool arguments`）。

修复（`stream_utils.py`）：带 id 分片帧在 by_id miss 且最后一个 pending
调用是**合成 id**（`_ToolCall.synthetic_id` 标记）的未完成链、名称兼容时，
并入该链并采纳真实 id（合成 id 尚未投递给客户端，改写安全；后续同 id
分片经 by_id 命中继续累积）。真实 id 的链绝不并入——并行**同名函数**
分片时误并入会串联两条链的参数。带完整参数的独立调用不受影响（dict
快照整体替换分片，并入与否结果等价）。

回归测试 ×4：链合并（真实 id 采纳）/ 采纳后同 id 续片 / 并行同名链
互不并入 / dict 快照完成链。

## 修复 2（P2）：迟到裸签名的思考链误归属防御

内部流式管线把携带 slot 14 签名的 wire part 分解为「内容事件 + 签名
事件」两个相邻帧（`streaming.emit_parsed_chunk`：thinking/tool_calls/
body 在前、thought_signature 恒最后）。签名帧紧跟其内容帧到达时，
coalesce 正确并入 pending 调用；但**签名帧晚于内容边界冲刷到达**（调用
已投递、pending 已清空）时会作为裸签名 part 透传，被三个协议适配器与
签名缓存记录为**思考链签名**——若该轮带思考摘要，FC 的签名会污染
thinking 缓存，客户端回传时被回注到 thought part 上，可能触发上游
密码学校验 400 `Corrupted thought signature`。

判别依据（wire 抓包形态）：**纯文本轮的尾部签名载体必然不与
functionCall 共存**。据此在全部六个收口点加防误归属守卫——流中已出现
functionCall 时，裸签名 part（无文本载体）不再记为/下发为思考链签名：

- `compat_shared._iter_response_parts`（非流式缓存收口）
- `compat_shared._FrameSignatureRecorder`（流式缓存收口）
- `compat_claude.build_claude_message`（非流式尾部载体回填）
- `compat_claude.claude_stream_adapter`（迟到签名补发；**思考块仍打开**
  时不受守卫影响——那是 thought part 自带签名的分解形态，归属正确）
- `compat_openai.build_openai_completion` / `openai_stream_adapter`
- `responses_service._ResponsesOutputBuilder` / `responses_stream_adapter`

宁缺勿错的代价：假设性的「FC 轮 + 尾部思考签名载体」复合形态会失去
思考签名，退化为无签名思考历史——上游容忍（与 Claude 侧占位签名回传
的既有降级一致），且该形态在真机抓包中从未出现。

## 修复 3（P2）：拆分思考块的整轮签名兜底回注

thinking 签名缓存键是**整轮思考全文**（响应侧拼接全部 thought part 后
收口缓存），而 `_convert_part` 的回注查找是**逐 part 精确匹配**。客户端
把单个 thinking 块拆分成多块回传（或将一轮思考分散到多条 assistant
消息，Responses 的 reasoning item 与 message 分属两项时同理）会导致
逐 part 查找全部 miss，签名退化为无。

修复（`chat_service._backfill_thinking_signatures`）：轮合并
（`_drop_hollow_and_merge_same_role`）之后，对 model 轮内**全部**
thought part 均无有效签名时，按整轮拼接文本再查一次缓存，命中则挂到
最后一个 thought part（Gemini 语义：末位 part 的签名为整轮权威签名）。
任一 thought part 已带有效签名（envelope 载体回传成功）时不介入。

## 核销（观察项，不改）

- model_turns 回放失配窗口内（重启/账号轮换），回传思考签名挂在
  thought part 本体（slot 14）而非尾部空文本 part——形态与抓包不同但
  签名为真值，参考实现（AIStudio2API setPartThoughtSignature）同构，
  维持现状。
- OpenAI 生态 array 形态 arguments 透传（极少客户端）、`tool_result`
  内 URL 形态图片忽略（Claude Code 恒为 base64）——低频形态，不处理。
- `_openai_tool_arguments` 数字类型漂移（1 → 1.0）导致缓存键失配 →
  SKIP 哨兵降级——JSON 规范化层面无法区分，属可接受降级。

## 验证

- 全量单测 1644 passed + 5 skipped（基线 1636 + 新增 8 项回归）；
- ruff check / format 全过；pyright 0 errors（15 warnings 为存量）；
- Claude 侧 harness 追踪 34/34、OpenAI/Responses 侧 21/21 全过。
