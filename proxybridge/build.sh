#!/usr/bin/env bash
set -euo pipefail

# 编译 TURN / SSTP → 本地 SOCKS5 协议桥接器（纯 Go，无 CGO 依赖）
# 产物：proxybridge/bin/aistudio-proxy-bridge
# Python 侧 infrastructure/proxy_bridge.py 按固定顺序发现该二进制。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v go >/dev/null 2>&1; then
  echo "错误: 未找到 Go 工具链（需要 go >= 1.27）。" >&2
  echo "安装指引: https://go.dev/dl/ 或 Termux: pkg install golang" >&2
  exit 1
fi

mkdir -p bin
echo ">>> 开始编译 aistudio-proxy-bridge..."
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o bin/aistudio-proxy-bridge .

echo ">>> 编译成功: ${SCRIPT_DIR}/bin/aistudio-proxy-bridge"
ls -lh bin/aistudio-proxy-bridge
