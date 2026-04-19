#!/bin/bash
# launch-kiosk.sh
# Run via SSH to start Chromium in kiosk mode on a headless/touchscreen Pi

DISPLAY_NUM=":0"
URL="http://localhost:8000"
USER=$(who | grep "$DISPLAY_NUM" | awk '{print $1}' | head -n1)

# Kill any existing Chromium instances
DISPLAY=$DISPLAY_NUM pkill -f chromium-browser 2>/dev/null
sleep 1

# Disable screensaver and display power management
DISPLAY=$DISPLAY_NUM xset s off
DISPLAY=$DISPLAY_NUM xset s noblank
#DISPLAY=$DISPLAY_NUM xset -dpms

# Launch Chromium in kiosk mode
DISPLAY=$DISPLAY_NUM chromium \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --no-default-browser-check \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --touch-events=enabled \
  --force-device-scale-factor=1.5 \
  --disable-features=TranslateUI \
  "$URL" > /dev/null 2>&1 &

echo "Chromium launched in kiosk mode pointing to $URL"
