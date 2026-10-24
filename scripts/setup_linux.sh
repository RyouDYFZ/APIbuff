#!/usr/bin/env bash
# ==============================================================================
# One-Command Bare-Metal Linux Deployment Script for Freebuff API Gateway
# Target OS: Ubuntu 20.04+, Debian 11+, Debian 12
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
echo "[1/6] Installing OS packages (Python, Node.js, Zsh, Git)..."
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
    npm

# 2. Install Freebuff CLI Globally
echo "[2/6] Installing Freebuff CLI via npm..."
npm install -g freebuff@latest

# Pre-fetch native binary
freebuff --version || true

# 3. Setup Project Directory
INSTALL_DIR="/opt/freebuff-api-service"
echo "[3/6] Setting up application in $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." >/dev/null 2>&1 && pwd )"
cp -r "$SCRIPT_DIR/"* "$INSTALL_DIR/"

# 4. Install Python Dependencies
echo "[4/6] Installing Python dependencies..."
python3 -m pip install --break-system-packages -r "$INSTALL_DIR/requirements.txt" 2>/dev/null || \
python3 -m pip install -r "$INSTALL_DIR/requirements.txt"

# 5. Configure Credentials
echo "[5/6] Checking credentials..."
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo "Created $INSTALL_DIR/.env template."
fi

# 6. Install & Enable Systemd Service
echo "[6/6] Installing systemd service..."
cp "$INSTALL_DIR/systemd/freebuff-api.service" /etc/systemd/system/freebuff-api.service
systemctl daemon-reload
systemctl enable freebuff-api.service

echo ""
echo "============================================================"
echo "🎉 Installation completed successfully!"
echo "============================================================"
echo "Next Steps:"
echo "1. Set your token in $INSTALL_DIR/.env:"
echo "   CODEBUFF_API_KEY=your_token_here"
echo "   FREEBUFF_API_KEY=your_gateway_secret"
echo ""
echo "2. Start the service:"
echo "   systemctl start freebuff-api.service"
echo ""
echo "3. Check status & logs:"
echo "   systemctl status freebuff-api.service"
echo "   journalctl -u freebuff-api.service -f"
echo "============================================================"
