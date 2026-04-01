#!/usr/bin/env bash
# install.sh – install server-watchdog on RHEL 8
# Run as root: sudo bash install.sh

set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/usr}"
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

# ── Auto-detect system context ────────────────────────────────────────────────
# Builds a plain-English description of this host that is written into
# [server] context in config.ini.  The LLM uses it to tailor its advice.
detect_system_context() {
    local parts=()

    # OS / distribution
    local os_name="" os_version=""
    if [[ -f /etc/os-release ]]; then
        os_name=$(    . /etc/os-release 2>/dev/null && echo "${NAME:-}"       )
        os_version=$( . /etc/os-release 2>/dev/null && echo "${VERSION_ID:-}" )
        [[ -n "$os_name" ]] && parts+=("${os_name}${os_version:+ ${os_version}}")
    elif [[ -f /etc/redhat-release ]]; then
        parts+=("$(< /etc/redhat-release)")
    fi

    # Graphical vs headless
    if systemctl is-active --quiet graphical.target 2>/dev/null; then
        parts+=("graphical desktop system")
    else
        parts+=("headless server")
    fi

    # VNC
    if rpm -q tigervnc-server &>/dev/null 2>&1 || \
       systemctl list-unit-files 'vncserver*' --no-legend 2>/dev/null | grep -q .; then
        parts+=("VNC remote desktop installed")
    fi

    # EDA software
    local eda_found=()
    [[ -d /opt/cadence ]]  && eda_found+=("Cadence")
    [[ -d /opt/mentor ]]   && eda_found+=("Mentor/Siemens EDA")
    [[ -d /opt/synopsys ]] && eda_found+=("Synopsys")
    [[ -d /opt/ansys ]]    && eda_found+=("Ansys")
    [[ ${#eda_found[@]} -gt 0 ]] && parts+=("EDA tools: ${eda_found[*]}")

    # Common server roles
    if systemctl is-enabled nfs-server &>/dev/null 2>/dev/null; then
        parts+=("NFS server")
    fi
    local _svc
    for _svc in httpd nginx apache2; do
        if systemctl is-enabled "$_svc" &>/dev/null 2>/dev/null; then
            parts+=("${_svc} web server"); break
        fi
    done
    for _svc in postgresql mariadb mysql; do
        if systemctl is-enabled "$_svc" &>/dev/null 2>/dev/null; then
            parts+=("${_svc} database"); break
        fi
    done

    # Audio hardware
    if ls /proc/asound/card* &>/dev/null 2>&1; then
        parts+=("audio hardware present")
    else
        parts+=("no audio hardware")
    fi

    # Bluetooth
    if rfkill list 2>/dev/null | grep -qi bluetooth; then
        parts+=("Bluetooth available")
    else
        parts+=("no Bluetooth")
    fi

    # WiFi
    if iw dev 2>/dev/null | grep -q Interface \
       || nmcli dev 2>/dev/null | grep -qi wifi; then
        parts+=("WiFi available")
    else
        parts+=("wired network only")
    fi

    # Assemble into a single sentence
    local ctx="" part
    for part in "${parts[@]}"; do
        [[ -n "$ctx" ]] && ctx+=". "
        ctx+="$part"
    done
    echo "${ctx}."
}

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
ln -sf "$VENV_DIR/bin/server-watchdog-send-now"    "$INSTALL_PREFIX/bin/server-watchdog-send-now"
ln -sf "$VENV_DIR/bin/server-watchdog-sampler"     "$INSTALL_PREFIX/bin/server-watchdog-sampler"

# ── Create directories ────────────────────────────────────────────────────────
info "Creating directories..."
install -d -m 755 "$CONFIG_DIR"
install -d -m 755 "$LOG_DIR"

# ── Install configuration file ────────────────────────────────────────────────
if [[ -f "$CONFIG_DIR/config.ini" ]]; then
    warn "Config file $CONFIG_DIR/config.ini already exists – skipping."
else
    # ── Auto-detect system context ─────────────────────────────────────────
    info "Auto-detecting system configuration..."
    DETECTED_CONTEXT=$(detect_system_context)
    info "Detected: ${DETECTED_CONTEXT}"

    # ── Prompt user for a plain-English description of this server's role ──
    echo
    info "A brief description of this server's purpose helps the LLM give"
    info "more relevant maintenance advice."
    USER_DESCRIPTION=""
    if [[ -t 0 ]]; then
        echo -n "  What is this server used for? (press Enter to skip): "
        read -r USER_DESCRIPTION || true
    else
        warn "Non-interactive install detected – server role prompt skipped."
    fi

    if [[ -n "$USER_DESCRIPTION" ]]; then
        SERVER_CONTEXT="${DETECTED_CONTEXT} Role: ${USER_DESCRIPTION}"
    else
        SERVER_CONTEXT="$DETECTED_CONTEXT"
    fi

    # ── Copy example config and inject the detected context ────────────────
    install -m 640 "$(dirname "$0")/config.ini.example" "$CONFIG_DIR/config.ini"

    export _WATCHDOG_CONTEXT="$SERVER_CONTEXT"
    "$PYTHON" - "$CONFIG_DIR/config.ini" <<'PYEOF'
import re, sys, os
config_file = sys.argv[1]
new_context = os.environ["_WATCHDOG_CONTEXT"]
with open(config_file, encoding="utf-8") as fh:
    content = fh.read()
# Replace "context = ..." including any backslash-continued lines
content = re.sub(
    r"^context\s*=\s*(?:[^\n]*\\\n)*[^\n]*\n",
    lambda _: f"context = {new_context}\n",
    content,
    flags=re.MULTILINE,
)
with open(config_file, "w", encoding="utf-8") as fh:
    fh.write(content)
PYEOF
    unset _WATCHDOG_CONTEXT

    info "Installed config to $CONFIG_DIR/config.ini"
    info "Server context auto-detected and written to config."
    warn "Edit $CONFIG_DIR/config.ini and set your email and Gemini API key."
fi

# ── Install systemd units ─────────────────────────────────────────────────────
info "Installing systemd units..."
install -m 644 "$(dirname "$0")/systemd/server-watchdog-avc.service"      "$SYSTEMD_DIR/"
install -m 644 "$(dirname "$0")/systemd/server-watchdog-monthly.service"  "$SYSTEMD_DIR/"
install -m 644 "$(dirname "$0")/systemd/server-watchdog-monthly.timer"    "$SYSTEMD_DIR/"
install -m 644 "$(dirname "$0")/systemd/server-watchdog-sampler.service"  "$SYSTEMD_DIR/"
install -m 644 "$(dirname "$0")/systemd/server-watchdog-sampler.timer"    "$SYSTEMD_DIR/"

systemctl daemon-reload

# ── Enable and start services ─────────────────────────────────────────────────
info "Enabling and starting services..."
systemctl enable --now server-watchdog-avc.service
systemctl enable --now server-watchdog-monthly.timer
systemctl enable --now server-watchdog-sampler.timer

info "Installation complete!"
echo
echo "Next steps:"
echo "  1. Edit ${CONFIG_DIR}/config.ini"
echo "     - Set [email] smtp_host / to_addr"
echo "     - Set [llm] api_key to your Gemini API key"
echo "     - Review [server] context (auto-detected; refine if needed)"
echo "  2. Check service status:"
echo "     systemctl status server-watchdog-avc.service"
echo "     systemctl list-timers server-watchdog-monthly.timer"
echo "     systemctl list-timers server-watchdog-sampler.timer"
