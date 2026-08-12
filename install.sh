#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="toyota-bot"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME.service"
PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Error: $PYTHON not found. Install Python 3.11+ first."
    exit 1
fi

cd "$DIR"

if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "Created config.json — edit it (Toyota credentials, Telegram token), then run this script again."
    exit 1
fi

mkdir -p data
if [ ! -f data/data.json ]; then
    echo '{"notify_chats":[],"last_notification":null}' > data/data.json
fi

if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --quiet -r requirements.txt

mkdir -p "$SERVICE_DIR"
sed "s|__REPO_DIR__|$DIR|g" "$DIR/toyota-bot.service" > "$SERVICE_FILE"

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

if command -v loginctl >/dev/null 2>&1; then
    if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -qi "yes"; then
        echo "Enabling linger so the bot starts at boot (may ask for sudo)..."
        if command -v sudo >/dev/null 2>&1; then
            sudo loginctl enable-linger "$USER" || \
                echo "Warning: could not enable linger — the bot runs only while you are logged in."
        else
            echo "Warning: sudo not found — the bot runs only while you are logged in."
        fi
    fi
fi

echo ""
echo "Bot installed and started."
echo "Status: systemctl --user status $SERVICE_NAME"
echo "Logs:   journalctl --user -u $SERVICE_NAME -f"
