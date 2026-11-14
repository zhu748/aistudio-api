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

## 四、客户端函数工具的签名语义（与 46f5139 的关系）

- 上游对**客户端声明函数**（functionDeclarations）的 functionCall
  回传**宽容**：签名可原样回传（本项目 extra_content 机制）、缺失
  亦可（不触发 400）。真机捕获证明带签名回传是被接受的形态。
- 上游对**服务端工具**（`default_api:*` 与内置 grounding 类）的
  functionCall 回传**强制**校验签名（原始 400 根因）。commit
  `46f5139` 的防御性剥离因此仅作用于服务端工具，与浏览器自然
  形态完全兼容：grounding 工具（google_search 等）本来就不产生
  历史中的 functionCall part。
- 本轮全部捕获（含记忆开启账号、多轮、多模型）**未出现任何
  `default_api:` 命名空间调用**；该形态仅在上游主动注入时出现。

## 五、证据文件

`/home/z/research-browser/hybrid6/`（实验机本地，不入库）：

- `req_01..03_natural_body.json`：页面自然构造的完整请求体
- `resp_01..03_status200.json`：上游响应（含 FC part + 签名）
- `turn1..5.png`：UI 渲染过程
- `hybrid7/`、`hybrid7b/`：3.5-flash-lite / 3.8-flash 判别捕获
