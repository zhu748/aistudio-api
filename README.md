# AI Studio API

一个面向 Google AI Studio 的 OpenAI 兼容 API 包装层。  
这次重构先把项目从根目录脚本堆，整理成了可维护的 `src` 包结构，同时保留了旧入口兼容层。

## 当前结构

```text
aistudio-api/
├── pyproject.toml
├── main.py                    # 统一入口
├── server.py                  # 兼容入口
├── client.py                  # 兼容入口
├── proxy.py                   # 兼容入口
├── src/
│   └── aistudio_api/
│       ├── api/               # FastAPI 边界层
│       │   ├── app.py
│       │   └── schemas.py
│       ├── application/       # 请求编排与文件清理
│       │   └── chat_service.py
│       ├── domain/            # 纯类型与错误
│       │   ├── errors.py
│       │   └── models.py
│       ├── infrastructure/    # 浏览器、缓存、协议改写、流解析
│       │   ├── browser/
│       │   ├── cache/
│       │   ├── gateway/
│       │   └── utils/
│       └── config.py
├── tests/
│   └── unit/
```

## 设计意图

- `api` 只处理 HTTP 协议和 OpenAI 兼容输出。
- `application` 负责把消息、图片、临时文件这些流程编排起来。
- `domain` 放纯数据结构和异常，尽量不依赖 FastAPI / Playwright。
- `infrastructure` 集中外部依赖，后面继续拆也有落点。
- `main.py` 是新的统一入口。
- `server.py`、`client.py` 这些根目录脚本现在主要是兼容层，不再是推荐主入口。

## 运行

```bash
python3 main.py server --port 8080 --camoufox-port 9222
python3 main.py client "你好"
python3 main.py snapshot "测试 prompt"
```

或者安装成包：

```bash
pip install -e .
aistudio-api server --port 8080 --camoufox-port 9222
aistudio-api client "你好"
aistudio-api-server --port 8080 --camoufox-port 9222
aistudio-api-client "你好"
```

## 下一步建议

这次还是第一阶段，目标是先“立边界”，不是一次性洗到底。  
接下来最值得继续做的是：

- 把 `api/app.py` 再拆成 `routes` + `services`
- 给 `request_rewriter`、`stream_parser`、`domain/models` 补完整单测
- 把配置改成显式环境变量校验
- 清理历史文档和实验脚本，把 `REVERSE.md`、`REQUEST_BODY.md` 收进单独文档目录
