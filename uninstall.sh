#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="toyota-bot"
USER_SERVICE_DIR="$HOME/.config/systemd/user"
USER_SERVICE_FILE="$USER_SERVICE_DIR/$SERVICE_NAME.service"
SYSTEM_SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

PURGE=false
KEEP_VENV=false
KEEP_CONFIG=true
KEEP_DATA=true
DISABLE_LINGER=false
YES=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Uninstall the Toyota Telegram Bot installed via ./install.sh.
Reverses: stops/disables systemd service, removes service files, daemon-reload.
Optionally removes venv / config / data.

Options:
  --purge             Remove everything: venv + config.json + data/ (irreversible)
  --keep-venv         Keep .venv even when --purge is used (default: remove venv)
  --remove-config     Delete config.json (default: keep, --purge deletes)
  --remove-data       Delete data/ directory (default: keep, --purge deletes)
  --disable-linger    Also run 'sudo loginctl disable-linger \$USER' if linger is enabled
  -y, --yes           Non-interactive, assume yes to prompts
  -h, --help          Show this help

Defaults (no flags): stop & disable service, remove service file(s),
keep config.json, data/ and .venv (use --purge to remove them).

Examples:
  ./uninstall.sh                 # keep config/data/venv, just remove service
  ./uninstall.sh --purge -y      # full cleanup
  ./uninstall.sh --remove-data   # remove service + data/
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=true; shift ;;
        --keep-venv) KEEP_VENV=true; shift ;;
        --remove-config) KEEP_CONFIG=false; shift ;;
        --remove-data) KEEP_DATA=false; shift ;;
        --disable-linger) DISABLE_LINGER=true; shift ;;
        -y|--yes) YES=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [[ "$PURGE" == true ]]; then
    KEEP_CONFIG=false
    KEEP_DATA=false
    if [[ "$KEEP_VENV" == false ]]; then
        # purge removes venv unless --keep-venv was explicitly passed
        KEEP_VENV=false
    fi
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

confirm() {
    if [[ "$YES" == true ]]; then return 0; fi
    read -r -p "$1 [y/N] " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]]
}

echo "Uninstalling $SERVICE_NAME (from $DIR)..."

# --- 1) Stop & disable user service ------------------------------------------
if systemctl --user list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME.service"; then
    echo "-> Stopping user service $SERVICE_NAME..."
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
elif systemctl --user status "$SERVICE_NAME" >/dev/null 2>&1; then
    echo "-> Stopping user service $SERVICE_NAME..."
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
else
    echo "-> No user service $SERVICE_NAME found."
fi

if [[ -f "$USER_SERVICE_FILE" ]]; then
    echo "-> Removing $USER_SERVICE_FILE"
    rm -f "$USER_SERVICE_FILE"
fi

# Reload user daemon if user systemd is running
if systemctl --user daemon-reload 2>/dev/null; then
    echo "-> User daemon reloaded."
else
    echo "-> User daemon not running (no bus), skipping user daemon-reload."
fi

# --- 2) Stop & disable system service (manual system-wide install) -----------
if [[ -f "$SYSTEM_SERVICE_FILE" ]]; then
    echo "-> Found system service $SYSTEM_SERVICE_FILE"
    if command -v sudo >/dev/null 2>&1; then
        echo "-> Stopping system service $SERVICE_NAME (requires sudo)..."
        sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        echo "-> Removing $SYSTEM_SERVICE_FILE"
        sudo rm -f "$SYSTEM_SERVICE_FILE"
        sudo systemctl daemon-reload 2>/dev/null || true
        echo "-> System daemon reloaded."
    else
        echo "Warning: sudo not found, cannot remove system service. Remove manually: $SYSTEM_SERVICE_FILE" >&2
    fi
else
    # also try to stop/disable even if file not there (in case it was already removed but still enabled)
    if sudo systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME.service"; then
        echo "-> Stopping orphan system service $SERVICE_NAME..."
        sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        sudo systemctl daemon-reload 2>/dev/null || true
    fi
fi

# --- 3) Linger ----------------------------------------------------------------
if [[ "$DISABLE_LINGER" == true ]]; then
    if command -v loginctl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
        if loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -qi "yes"; then
            echo "-> Disabling linger for $USER..."
            sudo loginctl disable-linger "$USER" 2>/dev/null || echo "Warning: could not disable linger." >&2
        else
            echo "-> Linger already disabled."
        fi
    else
        echo "-> Skipping linger disable (loginctl/sudo not available)."
    fi
else
    if command -v loginctl >/dev/null 2>&1; then
        if loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -qi "yes"; then
            echo "Note: linger is still enabled for $USER. Bot was configured to start at boot."
            echo "      Run with --disable-linger to disable it, or leave it if you use other user services."
        fi
    fi
fi

# --- 4) Optional removals ----------------------------------------------------
if [[ "$KEEP_VENV" == false && "$PURGE" == true ]]; then
    if [[ -d "$DIR/.venv" ]]; then
        if confirm "Remove virtualenv $DIR/.venv?"; then
            echo "-> Removing .venv..."
            rm -rf "$DIR/.venv"
        else
            echo "-> Keeping .venv."
        fi
    fi
elif [[ -d "$DIR/.venv" ]]; then
    echo "-> Keeping .venv (use --purge to remove)."
fi

if [[ "$KEEP_CONFIG" == false ]]; then
    if [[ -f "$DIR/config.json" ]]; then
        if confirm "Delete $DIR/config.json (contains Toyota/Telegram secrets)?"; then
            echo "-> Removing config.json..."
            rm -f "$DIR/config.json"
        else
            echo "-> Keeping config.json."
        fi
    fi
else
    echo "-> Keeping config.json."
fi

if [[ "$KEEP_DATA" == false ]]; then
    if [[ -d "$DIR/data" ]]; then
        if confirm "Delete $DIR/data/ (notification state)?"; then
            echo "-> Removing data/..."
            rm -rf "$DIR/data"
        else
            echo "-> Keeping data/."
        fi
    fi
else
    echo "-> Keeping data/."
fi

# Clean empty service dir
if [[ -d "$USER_SERVICE_DIR" ]] && [[ -z "$(ls -A "$USER_SERVICE_DIR" 2>/dev/null)" ]]; then
    rmdir "$USER_SERVICE_DIR" 2>/dev/null || true
fi

echo ""
echo "Uninstall complete."
echo "  User service:  systemctl --user status $SERVICE_NAME  (should be not-found/disabled)"
echo "  System service: sudo systemctl status $SERVICE_NAME  (should be not-found/disabled)"
if [[ -f "$DIR/config.json" ]]; then echo "  Config kept: $DIR/config.json"; fi
if [[ -d "$DIR/data" ]]; then echo "  Data kept:   $DIR/data/"; fi
if [[ -d "$DIR/.venv" ]]; then echo "  Venv kept:   $DIR/.venv/"; fi
echo "To reinstall: ./install.sh"
