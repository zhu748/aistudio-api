# browser-hybrid：AI Studio 浏览器混合捕获复现资产

配套文档：`docs/BROWSER_HYBRID_403_2026-11-14.md`（403 根因判别与
解决方案完整记录，先读它）。

## 组成

| 文件 | 作用 |
| --- | --- |
| `socks5_bridge.mjs` | 认证 SOCKS5 本地桥接（Chromium 不支持 RFC1929 认证代理） |
| `mint_server.py` | 本地 mint+send 服务（spinsnow 铸 proof / httpx 代发 / 403 重签） |
| `hybrid_capture.mjs` | Playwright 主脚本（拦截 / 观察 / 代发 / 回填） |

## 运行

```bash
# 前置: 项目 venv 就绪、Playwright Chromium 已安装、账号 auth.json 存在
export UPSTREAM_SOCKS_HOST=... UPSTREAM_SOCKS_PORT=... \
       UPSTREAM_SOCKS_USER=... UPSTREAM_SOCKS_PASS=...
export HYBRID_AUTH_FILE=/path/to/data/accounts/<id>/auth.json
export HYBRID_PROXY='socks5://user:pass@host:port'
export HYBRID_OUT=/tmp/capture
node hybrid_capture.mjs
```

产物：`req_NN_natural_headers.json` / `req_NN_natural_body.json` /
`resp_NN_statusNNN.json` / `turnN.png`。

## 注意

- 仅用于协议观察研究，不是生产通路；生产通路是项目自身签名栈。
- 捕获数据含账号会话凭据的派生信息（Cookie、签名、proof），
  不要提交进仓库。
- 实验严格限定 `aistudio.google.com` 域。
