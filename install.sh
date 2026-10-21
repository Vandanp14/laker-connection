#!/bin/bash
# Laker Connection installer + updater.
#
# First time (paste into Terminal):
#   curl -fsSL https://raw.githubusercontent.com/Vandanp14/laker-connection/main/install.sh | bash
# After that, double-click "Laker Connection" on the Desktop — it re-runs this
# script, which pulls the latest version and then starts the app (launch.sh).
#
# Layout:  ~/LakerConnection/app   code, replaced on every update
#          ~/LakerConnection/data  your residents/check-ins — never touched here
#          ~/LakerConnection/.venv Python + libraries (managed by uv)
#
# Needs no git, Homebrew, Xcode tools or admin password: curl and tar ship
# with macOS, and uv installs into ~/.local/bin.
#
# Test/dev switches: LAKER_HOME, LAKER_TARBALL_URL, LAKER_SKIP_DEPS=1,
# LAKER_SKIP_CHROME_CHECK=1, LAKER_NO_LAUNCH=1.
set -euo pipefail

DEFAULT_SOURCE="Vandanp14/laker-connection main"
PYTHON_VERSION="3.12"

say() { printf '==> %s\n' "$1"; }
fail() { printf '\n%s\n' "$1" >&2; exit 1; }

if [ "$(uname)" != "Darwin" ]; then
    fail "Laker Connection only runs on a Mac."
fi

LAKER_HOME="${LAKER_HOME:-$HOME/LakerConnection}"
APP="$LAKER_HOME/app"
DATA="$LAKER_HOME/data"
VENV="$LAKER_HOME/.venv"
mkdir -p "$DATA"

# --- Already running / already starting? ----------------------------------------
# A second double-click must not swap app/ (or reinstall libraries) under a
# running dashboard, and two quick double-clicks must not race each other.
DASHBOARD_URL="http://127.0.0.1:${LAKER_PORT:-5050}"
if curl -s -m 2 "$DASHBOARD_URL" 2>/dev/null | grep -q "Laker Connection"; then
    say "Laker Connection is already running — opening it."
    open "$DASHBOARD_URL"
    exit 0
fi
LOCK="$LAKER_HOME/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
        say "Laker Connection is already starting in another window — give it a moment."
        exit 0
    fi
    rm -rf "$LOCK"  # left behind by a run that crashed
    mkdir "$LOCK" || fail "Couldn't start. Restart your Mac and try again."
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# --- Google Chrome --------------------------------------------------------
if [ "${LAKER_SKIP_CHROME_CHECK:-}" != "1" ] && [ ! -d "/Applications/Google Chrome.app" ]; then
    fail "Google Chrome isn't installed. Install it from https://www.google.com/chrome/ and run this again."
fi

# --- uv (installs Python for us) ----------------------------------------------
UV=""
if [ "${LAKER_SKIP_DEPS:-}" != "1" ]; then
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
    elif [ -x "$HOME/.local/bin/uv" ]; then
        UV="$HOME/.local/bin/uv"
    else
        say "Installing uv (gets Python for Laker Connection)…"
        if ! curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null; then
            fail "Couldn't install uv. Check your internet connection and run the command again."
        fi
        UV="$HOME/.local/bin/uv"
    fi
fi

# --- Download the latest app code ----------------------------------------------
tarball_url() {
    if [ -n "${LAKER_TARBALL_URL:-}" ]; then
        echo "$LAKER_TARBALL_URL"
        return
    fi
    local source="$DEFAULT_SOURCE"
    if [ -f "$APP/UPDATE_SOURCE" ]; then
        source="$(head -n 1 "$APP/UPDATE_SOURCE")"
    fi
    local repo="${source% *}"
    local branch="${source##* }"
    echo "https://codeload.github.com/$repo/tar.gz/refs/heads/$branch"
}

# Downloads into a temp dir next to app/ (same disk, so the final mv is a
# rename), and only swaps once the download is complete and looks right.
# A bash script that's running from app/install.sh keeps reading its old copy
# after the swap — the open file survives the rename.
update_app() {
    local tmp="$1"
    mkdir -p "$tmp/new"
    curl -fsSL --connect-timeout 10 --max-time 180 "$(tarball_url)" -o "$tmp/app.tar.gz" 2>/dev/null || return 1
    tar -xzf "$tmp/app.tar.gz" -C "$tmp/new" --strip-components=1 2>/dev/null || return 1
    [ -f "$tmp/new/dashboard.py" ] || return 1

    rm -rf "$LAKER_HOME/app.old"
    if [ -d "$APP" ]; then
        mv "$APP" "$LAKER_HOME/app.old"
    fi
    mv "$tmp/new" "$APP"
    rm -rf "$LAKER_HOME/app.old"
}

say "Getting the latest Laker Connection…"
DOWNLOAD_DIR="$LAKER_HOME/.download"
rm -rf "$DOWNLOAD_DIR"
if update_app "$DOWNLOAD_DIR"; then
    rm -rf "$DOWNLOAD_DIR"
else
    rm -rf "$DOWNLOAD_DIR"
    if [ -f "$APP/dashboard.py" ]; then
        say "Couldn't download the latest version (are you online?). Starting the version you already have."
    else
        fail "Couldn't download Laker Connection. Check your internet connection and run the command again."
    fi
fi

# --- Python + libraries ----------------------------------------------------------
if [ "${LAKER_SKIP_DEPS:-}" != "1" ]; then
    if [ ! -x "$VENV/bin/python" ]; then
        say "Setting up Python (first time only, takes a minute)…"
        "$UV" venv --quiet --python "$PYTHON_VERSION" "$VENV" \
            || fail "Couldn't set up Python. Check your internet connection and run the command again."
    fi
    req_hash="$(shasum -a 256 "$APP/requirements.txt" | cut -d' ' -f1)"
    if [ "$(cat "$VENV/.req-hash" 2>/dev/null || true)" != "$req_hash" ]; then
        say "Installing libraries…"
        "$UV" pip install --quiet --python "$VENV/bin/python" -r "$APP/requirements.txt" \
            || fail "Couldn't install libraries. Check your internet connection and run the command again."
        echo "$req_hash" > "$VENV/.req-hash"
    fi
fi

# --- Double-click launcher ------------------------------------------------------------
LAUNCHER="$LAKER_HOME/Laker Connection.command"
cat > "$LAUNCHER" <<EOF
#!/bin/bash
# Double-click to start Laker Connection (updates itself first).
export LAKER_HOME="$LAKER_HOME"
exec /bin/bash "\$LAKER_HOME/app/install.sh"
EOF
chmod 755 "$LAUNCHER"
if [ -d "$HOME/Desktop" ] && [ ! -e "$HOME/Desktop/Laker Connection.command" ]; then
    # Not fatal: macOS may ask permission for the Desktop, and "Don't Allow"
    # must not stop the app from starting.
    if cp "$LAUNCHER" "$HOME/Desktop/Laker Connection.command" 2>/dev/null; then
        say "Added a Laker Connection launcher to your Desktop."
    else
        say "Couldn't add a Desktop shortcut. To start Laker Connection later, double-click \"Laker Connection.command\" in the LakerConnection folder in your home folder."
    fi
fi

if [ "${LAKER_NO_LAUNCH:-}" = "1" ]; then
    exit 0
fi
# launch.sh keeps the lock (same pid after exec) until the dashboard is up.
trap - EXIT
export LAKER_HOME LAKER_LOCK="$LOCK"
exec /bin/bash "$APP/launch.sh"
