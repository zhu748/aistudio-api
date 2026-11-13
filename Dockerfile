# syntax=docker/dockerfile:1
# Stage 1: Build native Go WAA signer daemon
# go.mod 要求 go 1.27（golang 官方镜像 GOTOOLCHAIN=local，低版本镜像会拒绝构建）
FROM golang:1.27-bookworm AS signer-builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    clang \
    build-essential \
    file \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# SpiderMonkey 静态库基于 LLVM libc++ 构建（std::__1:: 符号），clang/gcc 的
# libstdc++ 无法完成链接；zig 工具链自带 libc++（musl 静态链接），build.sh
# 检测到 zig 后会优先使用并产出全静态二进制（运行阶段无 libc++ 依赖）。
ARG ZIG_VERSION=0.17.1
RUN set -eux; \
    case "$(uname -m)" in \
        x86_64)  ZIG_ARCH="x86_64" ;; \
        aarch64) ZIG_ARCH="aarch64" ;; \
        *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 2 -o /tmp/zig.tar.xz \
        "https://ziglang.org/download/${ZIG_VERSION}/zig-${ZIG_ARCH}-linux-${ZIG_VERSION}.tar.xz"; \
    mkdir -p /opt/zig; \
    tar -xJf /tmp/zig.tar.xz -C /opt/zig --strip-components=1 --no-same-owner; \
    rm -f /tmp/zig.tar.xz; \
    ln -s /opt/zig/zig /usr/local/bin/zig; \
    zig version

COPY waa-signer/ waa-signer/
WORKDIR /build/waa-signer
RUN if [ ! -f "bin/waa-signer" ] || ! file bin/waa-signer | grep -q "ELF"; then \
        chmod +x build.sh && ./build.sh; \
    fi

# Stage 1b: Build TURN/SSTP proxy bridge (pure Go, no CGO)
# 把 TURN (RFC 6062) / TURNS / SSTP (MS-CHAPv2) 节点转换为本地回环 SOCKS5，
# 供 Python 侧 infrastructure/proxy_bridge.py 以子进程方式托管。无本地 Go
# 环境时也可用 AISTUDIO_PROXY_BRIDGE_BIN 指向外部二进制。
FROM golang:1.27-bookworm AS bridge-builder
COPY proxybridge/go.mod proxybridge/go.sum /build/proxybridge/
WORKDIR /build/proxybridge
RUN go mod download
COPY proxybridge/*.go /build/proxybridge/
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o bin/aistudio-proxy-bridge .

# Stage 2: Runtime Python environment
# Use Debian 12 (bookworm) explicitly so package names stay stable.
FROM python:3.15-slim-bookworm

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AISTUDIO_DATA_DIR=/app/data \
    AISTUDIO_CONFIG_FILE=/app/data/config.yaml

# Install minimal system dependencies for HTTPS and healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# 依赖版本由 uv export 从 uv.lock 生成（勿手工编辑 requirements.txt），
# 复制在前以便利用 Docker 层缓存。
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and default configuration template
# Copy precompiled native WAA signer service from builder stage
COPY --from=signer-builder /build/waa-signer/bin/waa-signer /app/waa-signer/bin/waa-signer
RUN chmod +x /app/waa-signer/bin/waa-signer
# Copy TURN/SSTP proxy bridge from builder stage
COPY --from=bridge-builder /build/proxybridge/bin/aistudio-proxy-bridge /app/proxybridge/bin/aistudio-proxy-bridge
RUN chmod +x /app/proxybridge/bin/aistudio-proxy-bridge

# Copy application code and default configuration template
COPY src/ src/
COPY main.py .
# 注意：镜像内嵌 defaults/config.yaml 纯净模板，而非仓库根目录的开发用
# config.yaml（后者可能含本地调试配置，不应进入分发镜像）。
COPY defaults/config.yaml /app/defaults/config.yaml
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
# Create necessary directories
RUN mkdir -p /app/data /tmp

# Set permissions
RUN chmod +x /app/main.py /app/docker-entrypoint.sh

# 安全：以非 root 用户运行。创建专用系统账号并把运行期需要写入的
# /app（data/、config.yaml、dumps/、静态资源等）授权给它；/tmp 保持
# 全局可写（1777），tempfile.gettempdir() 仍可正常工作。
# 同时兜底剥离 entrypoint 可能的 CRLF（.gitattributes 已保证 LF，
# 这里防御手工拷贝/旧 checkout 导致的 "bad interpreter: /bin/sh^M"）。
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app \
    && sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chown -R app:app /app
USER app

# Expose API server port (实际端口由 AISTUDIO_PORT / PORT 环境变量决定;
# Render 等 PaaS 平台会注入 PORT, 未注入时默认 8080)
EXPOSE 8080

# Health check: /healthz 为轻量无鉴权端点（k8s/PaaS 探活命名惯例，
# /health 为等价别名），不依赖上游账号与外部网络。
# 旧版用 /v1beta/models 会在“零账号首次启动”时探活失败导致部署误判。
# 注意 HEALTHCHECK 是独立进程，继承的是镜像/容器原始 ENV，看不到
# entrypoint 里 export 的 AISTUDIO_PORT；PaaS 只注入 PORT，因此必须
# 用 ${AISTUDIO_PORT:-${PORT:-8080}} 才能与实际监听端口一致。
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f "http://localhost:${AISTUDIO_PORT:-${PORT:-8080}}/healthz" || exit 1

# Container entrypoint and default command
# 不硬编码端口：entrypoint 已把平台注入的 PORT 映射为 AISTUDIO_PORT，
# 未注入时回退到默认 8080。
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "main.py", "server"]
