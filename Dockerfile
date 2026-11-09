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

# Stage 2: Runtime Python environment
# Use Debian 12 (bookworm) explicitly so package names stay stable.
FROM python:3.11-slim-bookworm

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

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and default configuration template
# Copy precompiled native WAA signer service from builder stage
COPY --from=signer-builder /build/waa-signer/bin/waa-signer /app/waa-signer/bin/waa-signer
RUN chmod +x /app/waa-signer/bin/waa-signer

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

# Expose API server port (实际端口由 AISTUDIO_PORT / PORT 环境变量决定;
# Render 等 PaaS 平台会注入 PORT, 未注入时默认 8080)
EXPOSE 8080

# Health check: /healthz 为轻量无鉴权端点（k8s/PaaS 探活命名惯例，
# /health 为等价别名），不依赖上游账号与外部网络。
# 旧版用 /v1beta/models 会在“零账号首次启动”时探活失败导致部署误判。
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f "http://localhost:${AISTUDIO_PORT:-8080}/healthz" || exit 1

# Container entrypoint and default command
# 不硬编码端口：entrypoint 已把平台注入的 PORT 映射为 AISTUDIO_PORT，
# 未注入时回退到默认 8080。
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "main.py", "server"]
