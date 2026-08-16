#!/bin/bash
# Bring up the desktop, then the control plane.
#
# Order matters: Xvfb must own the display before mutter starts, mutter must be
# managing windows before Chromium is launched (otherwise the browser window has
# no focus and keyboard input silently goes nowhere), and x11vnc must attach to a
# display that already exists.
set -euo pipefail

WIDTH="${CUA_DISPLAY_WIDTH:-1440}"
HEIGHT="${CUA_DISPLAY_HEIGHT:-900}"
DISPLAY_NUM="${DISPLAY#:}"

log() { echo "[entrypoint] $*" >&2; }

# The container runs as the invoking uid, which has no passwd entry and therefore
# no home. Everything from fontconfig to matplotlib wants one.
mkdir -p "${HOME:-/tmp/home}/.cache" "${HOME:-/tmp/home}/.config" 2>/dev/null || true
mkdir -p "${CUA_MODELS_DIR:-/models}/ultralytics/Ultralytics" 2>/dev/null || true

wait_for_x() {
    for _ in $(seq 1 50); do
        if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then return 0; fi
        sleep 0.2
    done
    log "X display $DISPLAY never came up"
    exit 1
}

log "starting Xvfb on $DISPLAY at ${WIDTH}x${HEIGHT}"
Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -ac -nolisten tcp &
wait_for_x

log "starting window manager"
# --x11 keeps mutter off wayland; the sleep-free retry is deliberate, we already
# waited on the display above.
dbus-launch --exit-with-session mutter --x11 --sm-disable >/tmp/mutter.log 2>&1 &

log "starting x11vnc"
# -shared        : the agent's session and an operator can both be attached
# -forever       : do not exit when the last client disconnects
# -nopw          : no VNC password. Acceptable ONLY because this port is bound to
#                  localhost by compose and the whole stack is a local demo.
#                  A real deployment terminates this behind an authenticated proxy.
x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 \
       -noxdamage -wait 10 >/tmp/x11vnc.log 2>&1 &

log "starting noVNC on :6080"
/opt/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 >/tmp/novnc.log 2>&1 &

log "starting control plane on :8000"
exec uvicorn cua.api.main:app --host 0.0.0.0 --port 8000
