#!/usr/bin/env bash
# install.sh – install server-watchdog on RHEL 8
# Run as root: sudo bash install.sh

set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/usr/local}"
CONFIG_DIR="/etc/server-watchdog"
LOG_DIR="/var/log/server-watchdog"
SYSTEMD_DIR="/etc/systemd/system"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Preflight checks ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "This script must be run as root."
command -v python3 &>/dev/null || error "python3 is required but not found."
command -v pip3   &>/dev/null || warn "pip3 not found; attempting to install python3-pip."

# ── Python dependencies ───────────────────────────────────────────────────────
info "Installing Python dependencies..."
pip3 install --quiet -r "$(dirname "$0")/requirements.txt"

# ── Install the Python package ────────────────────────────────────────────────
info "Installing server-watchdog package..."
pip3 install --quiet "$(dirname "$0")"

# ── Create directories ────────────────────────────────────────────────────────
info "Creating directories..."
install -d -m 755 "$CONFIG_DIR"
install -d -m 755 "$LOG_DIR"

# ── Install configuration file ────────────────────────────────────────────────
if [[ -f "$CONFIG_DIR/config.ini" ]]; then
    warn "Config file $CONFIG_DIR/config.ini already exists – skipping."
else
    install -m 640 "$(dirname "$0")/config.ini.example" "$CONFIG_DIR/config.ini"
    info "Installed default config to $CONFIG_DIR/config.ini"
    warn "Edit $CONFIG_DIR/config.ini and set your email and Gemini API key."
fi

# ── Install systemd units ─────────────────────────────────────────────────────
info "Installing systemd units..."
install -m 644 "$(dirname "$0")/systemd/server-watchdog-avc.service"     "$SYSTEMD_DIR/"
install -m 644 "$(dirname "$0")/systemd/server-watchdog-monthly.service" "$SYSTEMD_DIR/"
install -m 644 "$(dirname "$0")/systemd/server-watchdog-monthly.timer"   "$SYSTEMD_DIR/"

systemctl daemon-reload

# ── Enable and start services ─────────────────────────────────────────────────
info "Enabling and starting services..."
systemctl enable --now server-watchdog-avc.service
systemctl enable --now server-watchdog-monthly.timer

info "Installation complete!"
echo
echo "Next steps:"
echo "  1. Edit ${CONFIG_DIR}/config.ini"
echo "     - Set [email] smtp_host / to_addr"
echo "     - Set [llm] api_key to your Gemini API key"
echo "  2. Check service status:"
echo "     systemctl status server-watchdog-avc.service"
echo "     systemctl list-timers server-watchdog-monthly.timer"
