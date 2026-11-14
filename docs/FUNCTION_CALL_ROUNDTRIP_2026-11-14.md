# 2026-11-14 函数调用往返线缆对齐（hybrid6 混合捕获）

本文档承接 `BROWSER_HYBRID_403_2026-11-14.md`（403 解决方案与混合拦截
架构）。使用同一架构完成 AI Studio 前端**函数调用全链路**的自然形态
捕获：函数声明定义 → 模型 functionCall → UI 回填 functionResponse →
多轮签名链，并与项目 compat 层做逐槽位 diff。

## 一、实验环境

- 账号 jshsosjs77@gmail.com（Pro/TIER1），模型 `gemini-4-hydrogen`
- UI 工具配置：Google Search **关**（与 Function calling 互斥——
  开关禁用 tooltip 明示 "This tool is not compatible with the current
  active tools"）、Function calling **开**
- 函数声明经 "Edit function declarations" → Code Editor 以 JSON 粘贴：

  ```json
  [{
    "name": "get_weather",
    "description": "gets the current weather for a requested city",
    "parameters": {
      "type": "object",
      "properties": {"city": {"type": "string", "description": "the city name"}},
      "required": ["city"]
    }
  }]
  ```

- 交互要点：提交用 `Ctrl+Enter`；函数响应输入框是 `INPUT` 元素
  （`placeholder="Enter a function response"`，非 textarea/contenteditable），
  填 JSON 后点 Send。

## 二、捕获到的线缆结构（模型生成 → 回传）

### 2.1 响应侧 functionCall（resp_01，200）

单个 part 同时携带调用与签名：

```json
[null×10,
 ["get_weather", [[["city", [null, null, "Tokyo"]]]], "call_17864667614487423631"],
 null, null, null,
 "Ep8CCpwCAWkUfRPwbTMSPrJKjO+…"]
```

- slot 10 = `[name, args, call_id]`；**call_id 由上游生成**
  （`call_<19位数字>`），前端原样回传
- slot 14 = thoughtSignature（388B）——**签名挂在 functionCall part 本体**，
  不是独立尾 part（纯文本轮才是尾 part 形态）
- 尾随分片：空文本 part + finishReason=1(STOP) + usage + 会话 token

### 2.2 回传侧（req_02，前端自然构造，200）

```json
content[1] model: [null×10, ["get_weather", [[["city",[null,null,"Tokyo"]]]],
                              "call_14669973010617514372"], null,null,null, "<同签名388B>"]
content[2] user:  [null×11, ["get_weather",
                              [[["temperature",[null,18]],
                                ["condition",[null,null,"Partly cloudy"]]],
                                ["humidity",[null,62]]]],
                              "call_14669973010617514372"]]
```

- **functionCall 原样回传**：slot 10 三元素 + slot 14 同签名
- **functionResponse**：user 角色，slot 11 = `[name, response, call_id]`，
  **call_id 与 FC 一致**（配对键）
- response 载荷 wire：`[[[key, [v]], …]]`——int→`[null,18]`、
  str→`[null,null,"…"]`，与项目 `_encode_wire_args` 完全同构
- 模型 FC 轮**没有**额外尾签名 part（签名在 FC part 上）；后继文本轮
  恢复"思考摘要(slot12=1) + 文本 + 尾部空文本签名 part"形态

### 2.3 多轮（req_03）

第 2 次调用后历史 = [user, model(FC+SIG), user(FR), model(思考+文本+尾SIG),
user(新提问)]；会话 token（slot 11 顶层）每轮回传。

## 三、与项目 compat 层的对齐结论

用**等价 OpenAI 请求**（assistant.tool_calls 带 `extra_content.google.
thought_signature`、tool 消息、tools 声明）走项目完整转换管线
（`openai_to_gemini_request` → `normalize_gemini_request` → wire），
与捕获 req_02 逐槽位 diff：

| 对比项 | 结果 |
| --- | --- |
| contents 三轮（含 FC+SIG / FR） | **逐字节 MATCH** |
| tools functionDeclarations 编码 | MATCH（修复 propertyOrdering 后，见下） |
| 响应解析（slot10 + call_id + 签名附着） | MATCH（wire_parser 既有实现） |

**发现并修复的未对齐项**：

1. **propertyOrdering（Schema 槽位 22）缺省推导**：前端对 object
   schema 恒携带 propertyOrdering（= properties 键序快照），即使
   客户端未声明。项目此前仅在显式传入时携带 → 已改为 object 且有
   属性时按键序推导（`chat_service._encode_schema_to_wire`）。
2. **generationConfig 槽位 45**：多模型判别（hydrogen 携 1 /
   3.8-flash、3.5-flash-lite 不带）确认与 ListModels 目录槽位 [85]
   非空（gemini-4 三兄弟的 1M 上下文标记）严格对应，与 flag_13
   （[83]）正交。已按 flag_13 同构实现自动推导
   （`model_unknown_flag_45`，仅元数据命中时注入）。
3. **gemini-4 系浏览器默认 maxOutputTokens = 32768**：hydrogen 目录
   输出上限虽为 65536，真机请求实测恒 32768（同 [85] 标记家族；
   carbon/argon 输出上限亦为 32768）。`browser_default_max_tokens`
   已按家族修正。

修复后三模型默认 generationConfig 与真机捕获**完全一致**：

| 模型 | 项目产出 = 浏览器捕获 |
| --- | --- |
| gemini-4-hydrogen | `{3:32768, 4:1, 5:0.95, 6:64, 13:1, 16:[1,null,null,3], 45:1}` |
| gemini-3.8-flash | `{3:65536, 4:1, 5:0.95, 6:64, 13:1, 16:[1,null,null,2]}` |
| gemini-3.5-flash-lite | `{3:65536, 4:1, 5:0.95, 6:64, 16:[1,null,null,4]}` |

（思考档也随目录元数据吸附：hydrogen HIGH=3 / 3.8-flash MEDIUM=2 /
3.5-lite MINIMAL=4，与捕获一致。）

**实弹验证**：项目完整栈（含 slot45=1 + 32768 默认）对
`models/gemini-4-hydrogen` GenerateContent 实发 **200**。

## 四、签名强制校验语义（本地兼容服务 e2e 判别，2026-11-14）

用本地服务（8090，OpenAI 协议）对上游做了四组判别实验，**推翻了
此前"客户端函数签名宽容"的结论**：

| 实验 | 结果 |
| --- | --- |
| 客户端函数 FC 回传带原签名（extra_content 往返） | 200 |
| 客户端函数 FC 回传**无签名**（hydrogen 与 3.8-flash 均测） | **400** "missing thought_signature ... default_api:get_weather" |
| 伪签名（随机合法 base64） | **400 "Corrupted thought signature."**（密码学信封校验） |
| **跨调用交换真实签名**（Tokyo 调用带 Paris 调用的签名） | **200** |

结论：

1. **上游对一切 functionCall 回传强制签名**（客户端函数与服务端工具
   同规则；错误信息显示上游内部把客户端函数也纳入 `default_api:`
   命名空间）；
2. 签名**不可伪造**（Corrupted 校验），但**不绑定具体调用内容**
   （任何模型真实铸造的签名均可通过）；
3. 本轮全部浏览器捕获（含记忆开启账号）未出现 `default_api:` 命名空间
   调用——该形态仅在上游主动注入时出现；grounding 类内置工具本来就不
   产生历史中的 functionCall part。

### 修复：签名缓存回注

基于结论 2，实现「响应侧缓存、请求侧回注」：

- **响应侧**：`_iter_response_parts`（非流式）与 `_iter_gemini_sse_frames`
  （流式，覆盖 OpenAI / Claude / Responses 三协议的全部流式适配器）
  统一挂钩——凡见到 functionCall part 携带有效 thoughtSignature 即按
  `call_id`（首选）与函数名（兜底）缓存（`domain/tool_signatures.py`
  的 `ThoughtSignatureCache`，LRU + JSON 持久化 `data/tool_signature_cache.json`，
  重启不丢）；
- **请求侧**：三条输入路径（OpenAI tool_calls / Claude tool_use /
  Gemini 原生 functionCall parts）统一：客户端回传签名缺失或非法时先
  查缓存回注；仍未命中才剥离该调用及配对 functionResponse（模型会
  重新发起调用，功能降级但不再 400）；
- 客户端自身保存了签名（extra_content 机制）时优先用客户端的，缓存
  仅作丢失兜底。

**e2e 实测矩阵**（gemini-4-hydrogen，全 200）：

| 场景 | 结果 |
| --- | --- |
| 非流式：带签名回传 | 200 |
| 非流式：丢签名（缓存回注） | 200 |
| 流式：丢签名（缓存回注） | 200 |
| 服务重启后：丢签名（持久化缓存回注） | 200 |
| 缓存未命中：剥离（不 400） | 200（调用被剥离） |

## 五、证据文件

`/home/z/research-browser/hybrid6/`（实验机本地，不入库）：

- `req_01..03_natural_body.json`：页面自然构造的完整请求体
- `resp_01..03_status200.json`：上游响应（含 FC part + 签名）
- `turn1..5.png`：UI 渲染过程
- `hybrid7/`、`hybrid7b/`：3.5-flash-lite / 3.8-flash 判别捕获
