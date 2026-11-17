#!/bin/bash
#
# Register the launchd agent that starts the recording API on this Mac.
#
# Writes ~/Library/LaunchAgents/<label>.plist and loads it, so run_server.py
# starts at every log in and is started again if it ever exits.
#
# An agent (runs at log in), not a daemon (runs at boot), for the same reasons
# the Windows task runs at log on: microphone permission is per user and only
# honoured in that user's session, and OneDrive only runs there. Pair it with
# automatic log in for an unattended Mac; README.md explains how.
#
# Usage:
#   macos/install-agent.sh            install (or replace) and start the agent
#   macos/install-agent.sh --print    print the plist without installing it
#   macos/install-agent.sh --remove   stop the agent and delete the plist

set -euo pipefail

LABEL="${RECORDING_API_LABEL:-local.recording-api}"

# This script lives in <project>/macos, so the project is one level up.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$PROJECT_DIR/run_server.py"
PYTHON="$PROJECT_DIR/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# Where to reach the server: listenHost and listenPort from config.yaml, worked
# out by the server's own code. RECORDING_API_URL overrides it; the fallback is
# the default address.
BASE_URL="${RECORDING_API_URL:-}"
if [ -z "$BASE_URL" ]; then
    if ! BASE_URL="$(cd "$PROJECT_DIR" && "$PROJECT_DIR/.venv/bin/python" -c \
        'from config import load_config; print(load_config().local_url)' 2>/dev/null)" ||
        [ -z "$BASE_URL" ]; then
        BASE_URL="http://127.0.0.1:8080"
        echo "warning: could not read the address from config.yaml; trying $BASE_URL" >&2
    fi
fi

# Paths go into XML, where these three characters would end the document early.
xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

render_plist() {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(xml_escape "$LABEL")</string>

    <key>ProgramArguments</key>
    <array>
        <string>$(xml_escape "$PYTHON")</string>
        <string>$(xml_escape "$LAUNCHER")</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml_escape "$PROJECT_DIR")</string>

    <!-- launchd starts its jobs with a PATH of /usr/bin:/bin:/usr/sbin:/sbin,
         which does not include Homebrew. Without this the server starts fine
         and every recording then fails because ffmpeg cannot be found. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(xml_escape "$FFMPEG_DIR"):/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <!-- Start at log in, and again whenever it exits for any reason. -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <!-- Background jobs are throttled on CPU and disk by default. ffmpeg runs
         underneath this one and drops audio whenever it is held up, so ask
         for the same treatment an app in the foreground gets. -->
    <key>ProcessType</key>
    <string>Interactive</string>

    <!-- run_server.py sends uvicorn's log to server.log itself; this catches
         what is printed before that is set up, such as an import error. -->
    <key>StandardOutPath</key>
    <string>$(xml_escape "$PROJECT_DIR/launchd.log")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$PROJECT_DIR/launchd.log")</string>
</dict>
</plist>
EOF
}

# bootout returns before the job is fully gone, and loading the new one in that
# window fails with "Bootstrap failed: 5: Input/output error".
unload_agent() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || return 0
        sleep 0.5
    done
    echo "error: the old agent did not stop; try again in a moment." >&2
    exit 1
}

MODE="${1:-install}"

if [ "$MODE" = "--remove" ]; then
    unload_agent
    rm -f "$PLIST"
    echo "Removed launchd agent '$LABEL'. The server no longer starts at log in."
    exit 0
fi

if [ ! -f "$LAUNCHER" ]; then
    echo "error: run_server.py not found at $LAUNCHER" >&2
    exit 1
fi
if [ ! -x "$PYTHON" ]; then
    echo "error: no virtual environment at $PROJECT_DIR/.venv" >&2
    echo "Create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if ! FFMPEG="$(command -v ffmpeg)"; then
    echo "error: ffmpeg not found on PATH. Install it first:  brew install ffmpeg" >&2
    exit 1
fi
FFMPEG_DIR="$(dirname "$FFMPEG")"

if [ "$MODE" = "--print" ]; then
    render_plist
    exit 0
fi
if [ "$MODE" != "install" ]; then
    echo "usage: $0 [--print | --remove]" >&2
    exit 2
fi

mkdir -p "$(dirname "$PLIST")"
unload_agent
render_plist > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "Installed launchd agent '$LABEL'"
echo "  runs      : $PYTHON $LAUNCHER"
echo "  working in: $PROJECT_DIR"
echo "  ffmpeg    : $FFMPEG"
echo "  plist     : $PLIST"
echo

# Loading the agent starts it, but that only means launchd launched it; wait
# for it to answer.
up=""
for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 "$BASE_URL/status" >/dev/null 2>&1; then
        up=1
        break
    fi
    sleep 0.5
done
if [ -n "$up" ]; then
    echo "Recording API is up on $BASE_URL"
else
    echo "The server did not answer within 30 seconds." >&2
    echo "Look at server.log and launchd.log in $PROJECT_DIR for the reason." >&2
fi

# The agent only helps if the Mac itself comes back and stays awake. Reported,
# not changed: these settings need sudo, and this script deliberately does not.
echo
setting() { pmset -g custom | awk -v key="$1" '/^AC Power/ {ac=1} ac && $1 == key {print $2; exit}'; }
if [ "$(setting autorestart)" != "1" ]; then
    echo "warning: this Mac will not power back on after a power cut. Fix with:"
    echo "    sudo pmset -a autorestart 1"
fi
if [ "$(setting sleep)" != "0" ]; then
    echo "warning: this Mac is allowed to sleep, which stops recordings. Fix with:"
    echo "    sudo pmset -a sleep 0 disksleep 0"
fi
echo "Start a test recording now, sitting at this Mac: macOS asks once for"
echo "microphone access and that question has to be answered on screen."
echo "For an unattended Mac, set up automatic log in as well — see README.md."
