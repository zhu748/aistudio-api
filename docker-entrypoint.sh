#!/bin/sh
set -e

CONFIG_FILE="${AISTUDIO_CONFIG_FILE:-/app/data/config.yaml}"
TEMPLATE_FILE="/app/defaults/config.yaml"

if [ ! -f "$CONFIG_FILE" ] && [ -f "$TEMPLATE_FILE" ]; then
    # 镜像内只有 defaults/config.yaml 一个模板（原先的 /app/config.yaml
    # 回退分支永远不可达，属死代码）
    mkdir -p "$(dirname "$CONFIG_FILE")"
    cp "$TEMPLATE_FILE" "$CONFIG_FILE"
    echo "[entrypoint] Initialized default config.yaml at $CONFIG_FILE from $TEMPLATE_FILE"
fi

# PaaS 端口注入（Render / Railway / Fly.io 等通过 PORT 下发监听端口）：
# 优先级 AISTUDIO_PORT > PORT > 默认 8080。
if [ -n "${PORT:-}" ] && [ -z "${AISTUDIO_PORT:-}" ]; then
    export AISTUDIO_PORT="$PORT"
    echo "[entrypoint] Using platform injected PORT=${PORT}"
fi

exec "$@"
