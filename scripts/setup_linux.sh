#!/usr/bin/env bash
# ==============================================================================
# One-Command Bare-Metal Linux Deployment Script for Freebuff API Gateway
# Target OS: Ubuntu 20.04+, Debian 11+, Debian 12
#
# Installs the pinned official Freebuff CLI (telemetry is blocked at the
# gateway proxy, so the executable stays byte-for-byte official), installs the
# gateway under /opt/freebuff-api-service, and installs a systemd unit running
# as the unprivileged `freebuff` user.
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run as root (e.g. sudo bash scripts/setup_linux.sh)"
  exit 1
fi

echo "============================================================"
echo "    Installing Freebuff API Gateway on Linux"
echo "============================================================"

# 1. Update OS and Install Prerequisites
echo "[1/7] Installing OS packages (Python, Zsh, Git, sing-box)..."
apt-get update -y
apt-get install -y --no-install-recommends \
    curl \
    git \
    zsh \
    procps \
    ca-certificates \
    python3 \
    python3-pip \
    python3-venv \
    nodejs \
    npm \
    gcc \
    libc6-dev

# The pinned npm launcher is only for OAuth login; workers run the official
# executable installed in step 4 and egress through the gateway's sing-box.
npm install -g freebuff@0.2.34

ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then SB_ARCH="amd64"; elif [ "$ARCH" = "aarch64" ]; then SB_ARCH="arm64"; else SB_ARCH="amd64"; fi
curl -fsSL "https://github.com/SagerNet/sing-box/releases/download/v1.10.7/sing-box-1.10.7-linux-${SB_ARCH}.tar.gz" -o /tmp/sing-box.tar.gz
tar -xzf /tmp/sing-box.tar.gz -C /tmp
mv /tmp/sing-box-*/sing-box /usr/local/bin/sing-box
chmod +x /usr/local/bin/sing-box
rm -rf /tmp/sing-box*

# 2. Setup Project Directory
INSTALL_DIR="/opt/freebuff-api-service"
echo "[2/7] Setting up application in $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." >/dev/null 2>&1 && pwd )"
cp -r "$SCRIPT_DIR/"* "$INSTALL_DIR/"
if [ -f "$SCRIPT_DIR/.env.example" ]; then
    cp "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/.env.example"
fi

# 3. Install Python Dependencies
echo "[3/7] Installing Python dependencies..."
python3 -m pip install --break-system-packages -r "$INSTALL_DIR/requirements.txt" 2>/dev/null || \
python3 -m pip install -r "$INSTALL_DIR/requirements.txt"

# 4. Install the pinned official CLI from the verified release archive.
echo "[4/7] Installing the official Freebuff CLI (pinned 0.2.34)..."
python3 "$INSTALL_DIR/scripts/fetch_official_cli.py" \
    --fetch --output /opt/freebuff/official-cli/freebuff

# 5. Configure Credentials
echo "[5/7] Checking credentials..."
if [ ! -f "$INSTALL_DIR/.env" ]; then
    if [ -f "$INSTALL_DIR/.env.example" ]; then
        cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
        chmod 0600 "$INSTALL_DIR/.env"
        echo "Created $INSTALL_DIR/.env template (chmod 0600)."
    fi
fi

# 6. Dedicated unprivileged service account
echo "[6/7] Creating the freebuff service account..."
if ! id freebuff >/dev/null 2>&1; then
    useradd --system --home-dir /opt/freebuff --shell /usr/sbin/nologin freebuff
fi
chown -R freebuff:freebuff /opt/freebuff "$INSTALL_DIR"

# 7. Install & Enable Systemd Service
echo "[7/7] Installing systemd service..."
cp "$INSTALL_DIR/systemd/freebuff-api.service" /etc/systemd/system/freebuff-api.service
systemctl daemon-reload
systemctl enable freebuff-api.service

echo ""
echo "============================================================"
echo "🎉 Installation completed successfully!"
echo "============================================================"
echo "Next Steps:"
echo "1. Set your tokens in $INSTALL_DIR/.env:"
echo "   CODEBUFF_API_KEY=<upstream token>"
echo "   FREEBUFF_API_KEY=$(python3 -c 'import secrets; print("sk-" + secrets.token_hex(32))')"
echo ""
echo "2. Start the service (loopback by default; set FREEBUFF_API_HOST and"
echo "   FREEBUFF_API_KEY together to expose it):"
echo "   systemctl start freebuff-api.service"
echo ""
echo "3. Check status & logs:"
echo "   systemctl status freebuff-api.service"
echo "   journalctl -u freebuff-api.service -f"
echo "============================================================"
