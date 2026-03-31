#!/usr/bin/env bash
# install.sh – install server-watchdog on RHEL 8
# Run as root: sudo bash install.sh

set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/usr/local}"
VENV_DIR="/opt/server-watchdog/venv"
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

# find_python – pick the newest Python in [3.10, 3.14] available on this host.
# Tries versioned binaries from newest to oldest first, then falls back to the
# plain 'python3' symlink.  Sets the global PYTHON variable.
find_python() {
    local minor
    for minor in 14 13 12 11 10; do
        if command -v "python3.${minor}" &>/dev/null; then
            PYTHON="python3.${minor}"
            return 0
        fi
    done
    # Fall back to the plain python3 symlink and verify it meets the minimum.
    if command -v python3 &>/dev/null; then
        if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PYTHON="python3"
            return 0
        fi
    fi
    return 1
}

find_python \
    || error "Python 3.10 or later is required but not found. Install python3.10 or newer and re-run."
info "Using $($PYTHON --version)"

# ── Python virtual environment ────────────────────────────────────────────────
info "Creating Python virtual environment in ${VENV_DIR}..."
"$PYTHON" -m venv "$VENV_DIR"

# ── Python dependencies ───────────────────────────────────────────────────────
info "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --quiet -r "$(dirname "$0")/requirements.txt"

# ── Install the Python package ────────────────────────────────────────────────
info "Installing server-watchdog package..."
"$VENV_DIR/bin/pip" install --quiet "$(dirname "$0")"

# ── Link entry-point scripts into PATH ────────────────────────────────────────
info "Linking entry-point scripts to ${INSTALL_PREFIX}/bin/..."
ln -sf "$VENV_DIR/bin/server-watchdog-avc-monitor" "$INSTALL_PREFIX/bin/server-watchdog-avc-monitor"
ln -sf "$VENV_DIR/bin/server-watchdog-monthly"     "$INSTALL_PREFIX/bin/server-watchdog-monthly"
ln -sf "$VENV_DIR/bin/server-watchdog-send-now"       "$INSTALL_PREFIX/bin/server-watchdog-send-now"

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
