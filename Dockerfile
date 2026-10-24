# syntax=docker/dockerfile:1
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

# 1. Install base utilities, Zsh, Git, Node.js & Procps
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    git \
    zsh \
    procps \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Freebuff CLI globally and pre-cache native Linux binary
RUN npm install -g freebuff@latest \
    && mkdir -p /root/.config/manicode /workspace \
    && freebuff --version || true

WORKDIR /app

# 3. Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy application source
COPY . /app/

# Ensure entrypoint and helper scripts are executable
RUN chmod +x /app/entrypoint.sh /app/start.sh /app/stop.sh /app/login.sh 2>/dev/null || true

# 5. Pre-initialize an authentic Git repository in /workspace
RUN git config --global user.name "Cloud Developer" \
    && git config --global user.email "cloud@freebuff.local" \
    && git config --global init.defaultBranch main \
    && cd /workspace \
    && git init \
    && echo "# Cloud Workspace" > README.md \
    && echo -e "node_modules/\n*.pyc\n__pycache__/\n.env\n" > .gitignore \
    && git add . \
    && git commit -m "Initial commit for cloud workspace"

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
