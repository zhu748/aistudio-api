# aistudio-proxy-bridge

TURN / TURNS / SSTP 协议桥接器（独立 Go 程序）：把单个 TURN（RFC 6062 TCP 中继）
或 SSTP（MS-CHAPv2 VPN）节点转换为**仅监听 127.0.0.1 的本地 SOCKS5** 入站，
供 Python 侧 `src/aistudio_api/infrastructure/proxy_bridge.py` 以子进程方式托管。

## 协议能力

| 协议 | 说明 |
| --- | --- |
| `turn://` | TURN 长期凭据 + 匿名分配双模式（启动期一次探测自动选择），RFC 6062 TCP 中继（Allocate → Connect → ConnectionBind），支持 `realm` 参数 |
| `turns://` | TURN over TLS 1.2+（默认端口 5349），支持 `sni` / `ca` / `insecure` |
| `sstp://` | MS-SSTP v2 + PPP + MS-CHAPv2 认证，用户态虚拟网卡 + 轻量 TCP/IP 栈（无需 NET_ADMIN / tun 设备） |

## 构建

```bash
bash proxybridge/build.sh        # 需要 Go >= 1.27（纯 Go，无 CGO）
# 产物: proxybridge/bin/aistudio-proxy-bridge
```

Docker 镜像已在 `Dockerfile` 的 `bridge-builder` 阶段自动构建并安装到
`/app/proxybridge/bin/aistudio-proxy-bridge`；也可通过环境变量
`AISTUDIO_PROXY_BRIDGE_BIN` 指定自定义路径。

## 进程契约（与 Python 侧 `proxy_bridge.py` 同步维护）

- 命令行仅 `-config <私有 JSON 配置文件路径>`；凭据经 0600 权限临时文件传递，
  绝不进入命令行（避免 ps 泄漏）。启动后 Python 侧立即删除该文件。
- 配置 JSON 字段：`scheme / server / server_port / username / password /
  tls_server_name / ca_file / insecure / realm / mtu / listen_port / ready_file`。
- 就绪标记：先绑定本地 SOCKS5 端口，再写出 `ready_file`（内容 `ready\n`）；
  Python 侧同时校验 ready 文件与 SOCKS5 方法协商应答，防止外来进程抢占端口造成误判。
- 错误码：启动期（ready 写出前）全部走受控错误码（`config_read_failed`、
  `sstp_auth_failed_requires_mschapv2` 等），Python 侧维护对应中文文案映射；
  连接期错误码（`turn_*` / `target_*` / `socks_*`）仅作诊断输出。
- 生命周期：由 Python 父进程管理（SIGTERM 终止；Docker 部署随主容器同生命周期）。

## 测试

```bash
go test ./...      # Go 单测：真实 Pion TURN 服务器的 TURN/TURNS/匿名/SOCKS5 E2E、SSTP 包结构、中继看门狗
gofmt -l . testdata/   # 格式检查（空输出 = 通过）
go vet ./...
```

Python 侧跨进程 E2E（`tests/unit/test_proxy_bridge_e2e.py`）：真实编译本桥接器 +
`testdata/turnfixture.go`（Pion TURN 服务器）+ Python httpx，验证
`httpx → 本地 SOCKS5 → 桥接器 → TURN(RFC 6062) → HTTP 目标` 完整链路、
凭据错误 fail-closed 与关停清扫。需要 PATH 中有 Go >= 1.27，缺失时自动 skip。

源码移植自同作者的 [vertex2openai](https://github.com/zhu748/vertex2openai)
（MIT License），第三方依赖许可见 [NOTICE.txt](./NOTICE.txt)。
