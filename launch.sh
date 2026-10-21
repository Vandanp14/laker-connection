#!/bin/bash
# Starts Laker Connection: the automation Chrome + the dashboard, in this one
# Terminal window. Normally run by install.sh (which updates first); closing
# the window or pressing Ctrl+C stops everything this script started.
#
# Chrome: `open -na "Google Chrome" --args --user-data-dir=<own dir> ...`
# starts a separate Chrome instance with the debugging port open even while
# your everyday Chrome is running (checked 2026-10-21, Chrome 155) — so
# nobody has to quit Chrome first. The separate user-data-dir means it never
# touches your normal profiles, tabs, or history.
#
# Each run writes logs/runs/run-<time>.log (launcher + dashboard output,
# errors with tracebacks, each submitted row's outcome) and
# logs/runs/chrome-<time>.log (Chrome's own noisy output) under the data
# folder; the newest 20 of each are kept.
#
# Overrides (run.sh uses these to run a dev checkout in place):
#   LAKER_APP_DIR  code folder        (default ~/LakerConnection/app)
#   LAKER_PYTHON   python to use      (default ~/LakerConnection/.venv)
#   LAKER_DATA_DIR data folder        (default ~/LakerConnection/data)
#   LAKER_SKIP_LOGIN_PROMPT=1         don't ask to log into Starfish first
set -uo pipefail

LAKER_HOME="${LAKER_HOME:-$HOME/LakerConnection}"
APP="${LAKER_APP_DIR:-$LAKER_HOME/app}"
PY="${LAKER_PYTHON:-$LAKER_HOME/.venv/bin/python}"
export LAKER_DATA_DIR="${LAKER_DATA_DIR:-$LAKER_HOME/data}"
PORT="${LAKER_PORT:-5050}"
CHROME_DATA_DIR="$HOME/laker-chrome-automation"
DASHBOARD_URL="http://127.0.0.1:$PORT"

LOG_DIR="$LAKER_DATA_DIR/logs/runs"
RUN_STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_LOG="$LOG_DIR/run-$RUN_STAMP.log"
CHROME_LOG="$LOG_DIR/chrome-$RUN_STAMP.log"
mkdir -p "$LOG_DIR"
# Keep 19 old ones; this run adds the 20th.
for kind in run chrome; do
    # shellcheck disable=SC2012  # names are ours: no spaces/newlines
    ls -1t "$LOG_DIR"/"$kind"-*.log 2>/dev/null | tail -n +20 | while IFS= read -r old; do rm -f "$old"; done
done

release_lock() { if [ -n "${LAKER_LOCK:-}" ]; then rm -rf "$LAKER_LOCK"; fi; }
trap release_lock EXIT

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$RUN_LOG"; }
say() { printf '==> %s\n' "$1"; log "$1"; }
fail() { printf '\n%s\n' "$1" >&2; log "ERROR: $1"; exit 1; }

log "Laker Connection starting (app: $APP, data: $LAKER_DATA_DIR)"

port_open() { curl -s -o /dev/null -m 2 "$1"; }

# --- Already running? -------------------------------------------------------
if port_open "$DASHBOARD_URL"; then
    if curl -s -m 2 "$DASHBOARD_URL" | grep -q "Laker Connection"; then
        say "Laker Connection is already running — opening it."
        open "$DASHBOARD_URL"
        exit 0
    fi
    fail "Another app is using port $PORT, which Laker Connection needs. Quit that app (or restart your Mac) and try again."
fi

[ -x "$PY" ] || fail "Laker Connection isn't fully installed. Paste the install command from the README into Terminal again."

# The port the dashboard will connect to (config.json cdp_url), so the port
# checked below is exactly the one that gets driven. Override is for tests.
CDP_PORT="${LAKER_CDP_PORT:-$("$PY" -c 'import json,sys; from urllib.parse import urlparse; print(urlparse(json.load(open(sys.argv[1]))["cdp_url"]).port or 9222)' "$APP/config.json" 2>/dev/null || echo 9222)}"

STARTED_CHROME=""
DASHBOARD_PID=""

chrome_pid() {
    # The main Chrome process for our profile (helpers carry --type=…).
    local pid
    for pid in $(pgrep -f -- "--user-data-dir=$CHROME_DATA_DIR" 2>/dev/null); do
        if ! ps -o args= -p "$pid" | grep -q -- "--type="; then
            echo "$pid"
            return
        fi
    done
}

cleanup() {
    trap - EXIT INT TERM HUP
    release_lock
    if [ -n "$DASHBOARD_PID" ]; then
        kill "$DASHBOARD_PID" 2>/dev/null
        wait "$DASHBOARD_PID" 2>/dev/null
    fi
    if [ -n "$STARTED_CHROME" ]; then
        local pid
        pid="$(chrome_pid)"
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    fi
    say "Laker Connection stopped."
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# --- Automation Chrome --------------------------------------------------------------
if port_open "http://127.0.0.1:$CDP_PORT/json/version"; then
    # Only reuse it if it's *our* Chrome (our profile dir) listening there —
    # the dashboard drives whatever browser answers on this port.
    owners="$(lsof -nP -t -iTCP:"$CDP_PORT" -sTCP:LISTEN 2>/dev/null | sort -u)"
    if [ -z "$owners" ] || [ "$owners" != "$(chrome_pid)" ]; then
        fail "Another app is using port $CDP_PORT, which Laker Connection's Chrome needs. Quit that app (or restart your Mac) and try again."
    fi
    say "Automation Chrome is already open."
else
    say "Opening the automation Chrome window…"
    APPOINTMENTS_URL="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["appointments_url"])' "$APP/config.json")"
    # The three --disable-*background* flags stop Chrome from slowing down
    # the automation window when it's behind other windows — a stalled,
    # throttled tab can make a submission hang until something wakes it.
    open -na "Google Chrome" --stdout "$CHROME_LOG" --stderr "$CHROME_LOG" --args \
        --remote-debugging-port="$CDP_PORT" \
        --disable-background-timer-throttling \
        --disable-backgrounding-occluded-windows \
        --disable-renderer-backgrounding \
        --user-data-dir="$CHROME_DATA_DIR" \
        --profile-directory="LakerConnection" \
        --no-first-run \
        "$APPOINTMENTS_URL"
    STARTED_CHROME=1
    for _ in $(seq 1 40); do
        port_open "http://127.0.0.1:$CDP_PORT/json/version" && break
        sleep 0.5
    done
    port_open "http://127.0.0.1:$CDP_PORT/json/version" \
        || fail "Chrome didn't start in automation mode. Close any Laker Connection Chrome window (Cmd+Q in that window) and try again."
fi

# --- First-time Starfish login ---------------------------------------------------------
LOGIN_MARKER="$LAKER_DATA_DIR/.logged_in_once"
mkdir -p "$LAKER_DATA_DIR"
if [ ! -f "$LOGIN_MARKER" ] && [ "${LAKER_SKIP_LOGIN_PROMPT:-}" != "1" ]; then
    echo
    echo "First time: log into Starfish in the Chrome window that just opened"
    echo "(including the Duo/second step). When you see your calendar, come back"
    echo "here and press Enter."
    read -r _ 2>/dev/null </dev/tty || true
    touch "$LOGIN_MARKER"
else
    say "If Starfish asks you to log in again, do it in the automation Chrome window."
fi

# --- Dashboard ---------------------------------------------------------------------------
say "Starting the dashboard…"
# Output shows here and is copied into the run log.
LAKER_PORT="$PORT" PYTHONUNBUFFERED=1 "$PY" "$APP/dashboard.py" > >(tee -a "$RUN_LOG") 2>&1 &
DASHBOARD_PID=$!
for _ in $(seq 1 30); do
    port_open "$DASHBOARD_URL" && break
    kill -0 "$DASHBOARD_PID" 2>/dev/null || fail "The dashboard stopped while starting (see the message above)."
    sleep 0.5
done
port_open "$DASHBOARD_URL" || fail "The dashboard didn't start. Close this window and double-click Laker Connection again."

release_lock
open "$DASHBOARD_URL"
echo
echo "Laker Connection is running at $DASHBOARD_URL"
echo "Log for this run: $RUN_LOG"
echo "Keep this window open. Close it (or press Ctrl+C) when you're done."
wait "$DASHBOARD_PID"
