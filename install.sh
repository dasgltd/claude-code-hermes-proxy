#!/usr/bin/env bash
# install.sh — Install claude-proxy as a systemd user service
#
# Usage:
#   bash install.sh            # install on port 11435 (default)
#   PROXY_PORT=8080 bash install.sh
#
# Requirements:
#   - claude CLI installed and authenticated (run `claude --version` to verify)
#   - python3 with venv support

set -euo pipefail

INSTALL_DIR="$HOME/.local/lib/claude-proxy"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="claude-proxy"
PROXY_PORT="${PROXY_PORT:-11435}"

# ── Checks ────────────────────────────────────────────────────────────────────

echo "→ Checking requirements..."

if ! command -v claude &>/dev/null; then
  echo "✗ 'claude' CLI not found. Install Claude Code first:"
  echo "  npm install -g @anthropic-ai/claude-code"
  echo "  Then run: claude  (to authenticate)"
  exit 1
fi
echo "  ✓ claude $(claude --version 2>/dev/null | head -1)"

if ! command -v python3 &>/dev/null; then
  echo "✗ python3 not found. Install Python 3.10+."
  exit 1
fi
echo "  ✓ $(python3 --version)"

# ── Install ───────────────────────────────────────────────────────────────────

echo "→ Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp proxy.py "$INSTALL_DIR/proxy.py"

echo "→ Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r requirements.txt
echo "  ✓ fastapi + uvicorn installed"

# ── Systemd service ───────────────────────────────────────────────────────────

echo "→ Installing systemd user service..."
mkdir -p "$SERVICE_DIR"

# Substitute %h with actual $HOME in the service file
sed "s|%h|$HOME|g" claude-proxy.service > "$SERVICE_DIR/$SERVICE_NAME.service"

# Inject custom port if set
if [ "$PROXY_PORT" != "11435" ]; then
  sed -i "s|ExecStart=|Environment=PROXY_PORT=$PROXY_PORT\nExecStart=|" \
    "$SERVICE_DIR/$SERVICE_NAME.service"
fi

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.service"

# ── Verify ────────────────────────────────────────────────────────────────────

echo "→ Waiting for proxy to start..."
sleep 3

if curl -sf "http://127.0.0.1:$PROXY_PORT/health" >/dev/null; then
  echo ""
  echo "✓ claude-proxy is running on port $PROXY_PORT"
  echo ""
  echo "Configure your app:"
  echo "  ANTHROPIC_BASE_URL=http://127.0.0.1:$PROXY_PORT"
  echo "  ANTHROPIC_API_KEY=placeholder"
  echo ""
  echo "Manage:"
  echo "  systemctl --user status $SERVICE_NAME"
  echo "  systemctl --user restart $SERVICE_NAME"
  echo "  journalctl --user -u $SERVICE_NAME -f"
else
  echo "✗ Proxy did not respond. Check logs:"
  echo "  journalctl --user -u $SERVICE_NAME --no-pager -n 20"
  exit 1
fi
