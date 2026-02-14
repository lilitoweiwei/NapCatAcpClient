#!/usr/bin/env bash
# install-service.sh — Install ncat as a systemd service for auto-start on boot.
#
# Usage:
#   sudo bash scripts/install-service.sh [OPTIONS]
#
# Options (all optional, sensible defaults provided):
#   --user USER          Linux user to run the service as (default: current user)
#   --project-dir DIR    Absolute path to the ncat project root (default: script's parent dir)
#   --uv-path PATH       Absolute path to the uv binary (default: auto-detect via `which uv`)
#
# Examples:
#   sudo bash scripts/install-service.sh
#   sudo bash scripts/install-service.sh --user admin --project-dir /home/admin/ncat

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

SERVICE_NAME="ncat"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== ncat service installer ==="
echo "  User:        $SERVICE_USER"
echo "  Home:        $USER_HOME"
echo "  Project dir: $PROJECT_DIR"
echo "  uv path:     $UV_PATH"
echo "  Service:     $SERVICE_FILE"
echo ""

# ── Validate ─────────────────────────────────────────────────────────────────

if [[ ! -f "$PROJECT_DIR/main.py" ]]; then
    echo "ERROR: main.py not found in $PROJECT_DIR — is this the ncat project root?"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run with sudo."
    exit 1
fi

# ── Generate systemd unit file ───────────────────────────────────────────────

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=ncat — NapCatQQ OpenCode Channel bridge server
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
SyslogIdentifier=ncat

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
# Allow read-write to project data dir and opencode workspace (~/.ncat/)
ReadWritePaths=${PROJECT_DIR}/data ${USER_HOME}/.ncat

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
echo "The ncat service is now installed and enabled for auto-start on boot."
echo ""
echo "Useful commands:"
echo "  sudo systemctl start ncat          # Start now"
echo "  sudo systemctl stop ncat           # Stop"
echo "  sudo systemctl restart ncat        # Restart"
echo "  sudo systemctl status ncat         # Check status"
echo "  journalctl -u ncat -f              # Follow live logs"
echo "  journalctl -u ncat --since today   # Today's logs"
echo ""
echo "To uninstall:"
echo "  sudo systemctl stop ncat"
echo "  sudo systemctl disable ncat"
echo "  sudo rm $SERVICE_FILE"
echo "  sudo systemctl daemon-reload"
