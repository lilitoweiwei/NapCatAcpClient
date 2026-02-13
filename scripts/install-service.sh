#!/usr/bin/env bash
# install-service.sh — Install nochan as a systemd service for auto-start on boot.
#
# Usage:
#   sudo bash scripts/install-service.sh [OPTIONS]
#
# Options (all optional, sensible defaults provided):
#   --user USER          Linux user to run the service as (default: current user)
#   --project-dir DIR    Absolute path to the nochan project root (default: script's parent dir)
#   --uv-path PATH       Absolute path to the uv binary (default: auto-detect via `which uv`)
#
# Examples:
#   sudo bash scripts/install-service.sh
#   sudo bash scripts/install-service.sh --user admin --project-dir /home/admin/nochan

set -euo pipefail

# ── Parse arguments ──────────────────────────────────────────────────────────

SERVICE_USER="${SUDO_USER:-$(whoami)}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UV_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)        SERVICE_USER="$2"; shift 2 ;;
        --project-dir) PROJECT_DIR="$2";  shift 2 ;;
        --uv-path)     UV_PATH="$2";      shift 2 ;;
        *)             echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Auto-detect uv path if not specified
if [[ -z "$UV_PATH" ]]; then
    UV_PATH=$(su - "$SERVICE_USER" -c "which uv" 2>/dev/null || which uv 2>/dev/null || true)
    if [[ -z "$UV_PATH" ]]; then
        echo "ERROR: Could not find 'uv' binary. Install uv first, or pass --uv-path."
        exit 1
    fi
fi

# Resolve the service user's home directory
USER_HOME=$(eval echo "~$SERVICE_USER")

SERVICE_NAME="nochan"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== nochan service installer ==="
echo "  User:        $SERVICE_USER"
echo "  Home:        $USER_HOME"
echo "  Project dir: $PROJECT_DIR"
echo "  uv path:     $UV_PATH"
echo "  Service:     $SERVICE_FILE"
echo ""

# ── Validate ─────────────────────────────────────────────────────────────────

if [[ ! -f "$PROJECT_DIR/main.py" ]]; then
    echo "ERROR: main.py not found in $PROJECT_DIR — is this the nochan project root?"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run with sudo."
    exit 1
fi

# ── Generate systemd unit file ───────────────────────────────────────────────

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=nochan — NapCatQQ OpenCode Channel bridge server
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${UV_PATH} run python main.py
Restart=on-failure
RestartSec=5

# Logging: stdout/stderr go to journalctl
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nochan

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
# Allow read-write to project data dir and opencode workspace (~/.nochan/)
ReadWritePaths=${PROJECT_DIR}/data ${USER_HOME}/.nochan

# Environment: inherit user's PATH so opencode CLI is discoverable
Environment="PATH=/usr/local/bin:/usr/bin:/bin:${USER_HOME}/.local/bin"
Environment="HOME=${USER_HOME}"

[Install]
WantedBy=multi-user.target
UNIT

echo "Created $SERVICE_FILE"

# ── Enable and provide instructions ──────────────────────────────────────────

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "=== Done! ==="
echo ""
echo "The nochan service is now installed and enabled for auto-start on boot."
echo ""
echo "Useful commands:"
echo "  sudo systemctl start nochan          # Start now"
echo "  sudo systemctl stop nochan           # Stop"
echo "  sudo systemctl restart nochan        # Restart"
echo "  sudo systemctl status nochan         # Check status"
echo "  journalctl -u nochan -f              # Follow live logs"
echo "  journalctl -u nochan --since today   # Today's logs"
echo ""
echo "To uninstall:"
echo "  sudo systemctl stop nochan"
echo "  sudo systemctl disable nochan"
echo "  sudo rm $SERVICE_FILE"
echo "  sudo systemctl daemon-reload"
