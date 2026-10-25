# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: fetch the pinned official 0.2.34 CLI. The executable is installed
# byte-for-byte from the official release archive; its archive and executable
# SHA-256 must match cli/freebuff-0.2.34-binary-checksums.json. Telemetry is
# blocked at the gateway proxy (sing-box), so nothing is rebuilt or patched.
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS official-cli-fetch

ARG TARGETARCH

WORKDIR /src
COPY . /src/

RUN case "${TARGETARCH}" in \
        amd64) CLI_TARGET=linux-x64 ;; \
        arm64) CLI_TARGET=linux-arm64 ;; \
        *) echo "Unsupported build architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && python3 scripts/fetch_official_cli.py \
        --target "${CLI_TARGET}" --fetch \
        --output /out/official-cli/freebuff

# ---------------------------------------------------------------------------
# Stage 2: runtime image. Workers run the pinned official CLI only; the npm
# launcher is installed solely for interactive `freebuff login` (login.sh).
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm

LABEL maintainer="Antigravity Freebuff Gateway Team"
LABEL description="Headless, high-performance OpenAI-compatible API Gateway for Freebuff CLI"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8000
ENV FREEBUFF_WORKER_WORKDIR=/workspace
ENV FREEBUFF_SPAWN_MODE=detached_zsh
ENV SHELL=/bin/zsh
ENV FREEBUFF_BIN=/opt/freebuff/official-cli/freebuff
ENV FREEBUFF_HARDWARE_PRIVACY=auto

# 1. Base utilities, Zsh, Git, Node.js (login launcher), GCC & Sing-box
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    git \
    zsh \
    procps \
    curl \
    tar \
    ca-certificates \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH=$(uname -m) \
    && if [ "$ARCH" = "x86_64" ]; then SB_ARCH="amd64"; elif [ "$ARCH" = "aarch64" ]; then SB_ARCH="arm64"; else SB_ARCH="amd64"; fi \
    && curl -fsSL "https://github.com/SagerNet/sing-box/releases/download/v1.10.7/sing-box-1.10.7-linux-${SB_ARCH}.tar.gz" -o /tmp/sing-box.tar.gz \
    && tar -xzf /tmp/sing-box.tar.gz -C /tmp \
    && mv /tmp/sing-box-*/sing-box /usr/local/bin/sing-box \
    && chmod +x /usr/local/bin/sing-box \
    && rm -rf /tmp/sing-box* \
    # Launcher only: workers never execute it (receipt verification would fail).
    && npm install -g freebuff@0.2.34 \
    && rm -rf /root/.config/manicode || true

# 2. Install the pinned official CLI fetched in stage 1.
COPY --from=official-cli-fetch /out/official-cli/ /opt/freebuff/official-cli/
RUN chmod 0755 /opt/freebuff/official-cli/freebuff

WORKDIR /app

# 3. Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy application source
COPY . /app/

# Ensure entrypoint and helper scripts are executable, and compile the Linux
# hardware privacy library ahead of time.
RUN chmod +x /app/entrypoint.sh /app/start.sh /app/stop.sh /app/login.sh 2>/dev/null || true \
    && if [ -f /app/app/linux_hardware_privacy.c ]; then \
        gcc -shared -fPIC -O2 -o /app/libhardware_privacy.so /app/app/linux_hardware_privacy.c -ldl; \
    fi

# 5. Workspace root (each account initializes its own Git repository)
RUN mkdir -p /workspace

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
