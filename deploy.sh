#!/usr/bin/env bash
# deploy.sh — Deploy PixCut-App to a Raspberry Pi
#
# Usage:
#   ./deploy.sh                         # uses defaults below
#   PI_HOST=mypi.local ./deploy.sh
#
# Requires: rsync, ssh, sshpass (optional — brew install sshpass for non-interactive use)
# The Pi user must have sudo access.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — override via environment variables
# ---------------------------------------------------------------------------
PI_HOST="${PI_HOST:-raspberrypi.local}"
PI_USER="${PI_USER:-pi}"
PI_PASS="${PI_PASS:-}"
REMOTE_DIR="/home/${PI_USER}/pixcut-app"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# SSH/SCP helpers — use sshpass if available, otherwise prompt
# ---------------------------------------------------------------------------
if command -v sshpass &>/dev/null; then
    if [[ -z "${PI_PASS}" ]]; then
        echo "Error: sshpass is installed but PI_PASS is not set."
        echo "       Set it: PI_PASS=yourpassword ./deploy.sh"
        echo "       Or uninstall sshpass to fall back to interactive SSH prompts."
        exit 1
    fi
    SSH="sshpass -p ${PI_PASS} ssh -o StrictHostKeyChecking=accept-new"
    RSYNC_RSH="sshpass -p ${PI_PASS} ssh -o StrictHostKeyChecking=accept-new"
else
    echo "Note: sshpass not found — you will be prompted for the password."
    echo "      Install with: brew install sshpass"
    SSH="ssh -o StrictHostKeyChecking=accept-new"
    RSYNC_RSH="ssh -o StrictHostKeyChecking=accept-new"
fi

echo "==> Deploying PixCut-App to ${PI_USER}@${PI_HOST}:${REMOTE_DIR}"

# ---------------------------------------------------------------------------
# Step 1: Sync project files
# ---------------------------------------------------------------------------
echo ""
echo "--- Syncing files ---"
rsync -av --progress \
    -e "${RSYNC_RSH}" \
    --exclude='.venv/' \
    --exclude='run-logs/' \
    --exclude='output/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='research/' \
    --exclude='.git/' \
    --exclude='deploy.sh' \
    "${SCRIPT_DIR}/" \
    "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/"

# ---------------------------------------------------------------------------
# Prompt: Chromium kiosk autostart
# ---------------------------------------------------------------------------
echo ""
echo "--- Chromium kiosk launcher ---"
echo "launch-kiosk.sh opens Chromium fullscreen pointing at the kiosk UI."
echo ""
echo "  WARNING: Kiosk mode is fullscreen with no browser chrome. Without a"
echo "           keyboard or mouse attached there is no way to exit the browser."
echo "           Only enable autostart on a dedicated touchscreen display."
echo ""
read -r -p "Auto-launch Chromium at desktop login? [y/N] " AUTOSTART_CHOICE
echo ""

# ---------------------------------------------------------------------------
# Step 2: Remote setup
# ---------------------------------------------------------------------------
echo "--- Running remote setup ---"

$SSH "${PI_USER}@${PI_HOST}" bash -s -- "${PI_USER}" "${AUTOSTART_CHOICE}" <<'REMOTE'
set -euo pipefail
PI_USER="$1"
AUTOSTART_CHOICE="$2"
REMOTE_DIR="/home/${PI_USER}/pixcut-app"
cd "${REMOTE_DIR}"

echo "  [1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-dev libusb-1.0-0 chromium

echo "  [2/7] Adding ${PI_USER} to plugdev group..."
sudo usermod -aG plugdev "${PI_USER}" || true

echo "  [3/7] Installing udev rules..."
sudo cp "${REMOTE_DIR}/deploy/99-pixcut.rules" /etc/udev/rules.d/99-pixcut.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "  [4/7] Creating Python venv and installing dependencies..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -r requirements-server.txt

echo "  [5/7] Installing systemd service..."
sudo cp "${REMOTE_DIR}/deploy/pixcut-kiosk.service" /etc/systemd/system/pixcut-kiosk.service
sudo systemctl daemon-reload
sudo systemctl enable pixcut-kiosk

echo "  [6/7] Setting up Chromium launcher..."
chmod +x "${REMOTE_DIR}/deploy/launch-kiosk.sh"

DESKTOP_FILE="[Desktop Entry]
Type=Application
Name=PixCut Kiosk
Comment=Open PixCut kiosk UI in fullscreen Chromium
Exec=${REMOTE_DIR}/deploy/launch-kiosk.sh
Terminal=false
Icon=chromium"

if [[ "${AUTOSTART_CHOICE,,}" == "y" || "${AUTOSTART_CHOICE,,}" == "yes" ]]; then
    mkdir -p "/home/${PI_USER}/.config/autostart"
    echo "${DESKTOP_FILE}" > "/home/${PI_USER}/.config/autostart/pixcut-kiosk-browser.desktop"
    echo "  Autostart entry written to ~/.config/autostart/pixcut-kiosk-browser.desktop"
    echo "  Chromium will launch automatically at next desktop login."
else
    mkdir -p "/home/${PI_USER}/Desktop"
    ICON_PATH="/home/${PI_USER}/Desktop/PixCut-Kiosk.desktop"
    echo "${DESKTOP_FILE}" > "${ICON_PATH}"
    chmod +x "${ICON_PATH}"
    # Mark as trusted so LXDE launches it without a confirmation prompt
    gio set "${ICON_PATH}" metadata::trusted true 2>/dev/null || true
    echo "  Desktop icon created at ~/Desktop/PixCut-Kiosk.desktop"
    echo "  Double-tap the icon to open the kiosk browser."
fi

echo "  [7/7] Starting service..."
sudo systemctl restart pixcut-kiosk

sleep 2
echo ""
echo "  Service status:"
sudo systemctl status pixcut-kiosk --no-pager || true
REMOTE

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "==> Deploy complete!"
echo ""
echo "    Kiosk URL:  http://${PI_HOST}:8000"
echo "    API check:  curl http://${PI_HOST}:8000/api/stickers"
echo "    Logs:       ssh ${PI_USER}@${PI_HOST} 'journalctl -u pixcut-kiosk -f'"
echo ""
if [[ "${AUTOSTART_CHOICE,,}" == "y" || "${AUTOSTART_CHOICE,,}" == "yes" ]]; then
    echo "    Browser:    auto-launches at desktop login"
    echo "    To disable: delete ~/.config/autostart/pixcut-kiosk-browser.desktop on the Pi"
else
    echo "    Browser:    launch manually via the PixCut-Kiosk desktop icon"
    echo "    To enable autostart later: re-run deploy.sh and answer Y at the prompt"
fi
echo ""
echo "Note: If this is your first deploy, the plugdev group membership change"
echo "      requires a reboot to take full effect:"
echo "      ssh ${PI_USER}@${PI_HOST} 'sudo reboot'"
