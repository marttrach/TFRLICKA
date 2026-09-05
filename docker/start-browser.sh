#!/usr/bin/env bash
# Start Xvfb, a headed Chromium with a CDP endpoint, and noVNC over VNC.
#
# The API container drives this browser over CDP; the person watches and solves
# the official reCAPTCHA over noVNC. Nothing in this script inspects, solves, or
# submits a challenge.
set -euo pipefail

DISPLAY="${DISPLAY:-:99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x1024x24}"
CDP_PORT="${CDP_PORT:-9222}"
# Chromium 140+ silently ignores --remote-debugging-address and always binds
# DevTools to 127.0.0.1 (upstream hardening against remote cookie theft), so
# Chromium gets a private loopback port and socat republishes it on the
# container interface. The two ports must differ: socat cannot bind a port
# Chromium already holds on loopback.
CDP_LOOPBACK_PORT="${CDP_LOOPBACK_PORT:-9223}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
export DISPLAY

# The Playwright image stores Chromium under a version-stamped directory, so
# resolve it at runtime instead of pinning a path that breaks on image updates.
CHROME_BIN="$(find /ms-playwright -maxdepth 3 -type f -name chrome -path '*chrome-linux*' \
    | sort | tail -n 1)"
if [ -z "${CHROME_BIN}" ]; then
    echo "start-browser: no Chromium found under /ms-playwright" >&2
    exit 1
fi
echo "start-browser: using ${CHROME_BIN}"

terminate() {
    echo "start-browser: shutting down"
    kill 0 2>/dev/null || true
}
trap terminate EXIT INT TERM

Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &
XVFB_PID=$!

for _ in $(seq 1 50); do
    if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done

# --no-sandbox is required for Chromium as root inside a container.
"${CHROME_BIN}" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --remote-debugging-port="${CDP_LOOPBACK_PORT}" \
    --no-first-run \
    --no-default-browser-check \
    --disable-features=TranslateUI \
    --window-position=0,0 \
    --window-size="${SCREEN_GEOMETRY%x*}" \
    about:blank &
CHROME_PID=$!

# Exposing CDP on the container interface is safe ONLY because 9222 is never
# published outside the compose network -- it has no authentication and this
# browser holds the user's national ID and TRC credentials.
#
# The API must still address this by IP, not by container name: Chromium answers
# 500 "Host header is specified and is not an IP address or localhost" to a bare
# name, and it builds the returned webSocketDebuggerUrl from that same header.
# See cdp_url_over_ipv4() in src/tra_sniper/automation.py.
socat TCP-LISTEN:"${CDP_PORT}",fork,reuseaddr TCP:127.0.0.1:"${CDP_LOOPBACK_PORT}" &
SOCAT_PID=$!

x11vnc -display "${DISPLAY}" -forever -shared -nopw -rfbport "${VNC_PORT}" -quiet &
X11VNC_PID=$!

websockify --web=/usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" &
WEBSOCKIFY_PID=$!

echo "start-browser: ready (cdp=${CDP_PORT} novnc=${NOVNC_PORT})"

# Exit as soon as any component dies so the container restart policy recovers
# the whole stack instead of leaving a half-dead browser the API cannot use.
wait -n "${XVFB_PID}" "${CHROME_PID}" "${SOCAT_PID}" "${X11VNC_PID}" "${WEBSOCKIFY_PID}"
echo "start-browser: a component exited; stopping container" >&2
exit 1
