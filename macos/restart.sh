#!/bin/bash
#
# Restart the recording API on this Mac, for after a change to the code or to
# config.yaml.
#
# Stops every recording API server running on this Mac, not only the launchd
# agent's (stop_servers.py; it ends recordings through the API first, so each
# MP3 is finalised with an accurate duration header), then starts the agent
# and waits until the server answers.
#
# Usage:
#   macos/restart.sh            asks first if a room is recording
#   macos/restart.sh --force    restarts without asking

set -uo pipefail

LABEL="${RECORDING_API_LABEL:-local.recording-api}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

FORCE=""
case "${1:-}" in
    "") ;;
    -f | --force) FORCE=1 ;;
    *)
        echo "usage: $0 [--force]" >&2
        exit 2
        ;;
esac

if [ ! -x "$PYTHON" ]; then
    echo "error: no virtual environment at $PROJECT_DIR/.venv" >&2
    exit 1
fi
if [ ! -f "$PLIST" ]; then
    echo "error: no launchd agent '$LABEL' is installed." >&2
    echo "Run macos/install-agent.sh first." >&2
    exit 1
fi

# Where the restarted server will answer: listenHost and listenPort from
# config.yaml, worked out by the server's own code. RECORDING_API_URL overrides
# it. If config.yaml cannot be used, config.py says why, and the running server
# is left alone: the new one could not start with it either.
BASE_URL="${RECORDING_API_URL:-}"
if [ -z "$BASE_URL" ] && ! BASE_URL="$("$PYTHON" "$PROJECT_DIR/config.py")"; then
    echo "Nothing changed." >&2
    exit 1
fi

# --- 1. Stop every server on this Mac ----------------------------------------

# Asks first if a room is recording, and unloads the agent, which would
# otherwise start its server straight again.
RECORDING_API_LABEL="$LABEL" "$PYTHON" "$PROJECT_DIR/stop_servers.py" ${FORCE:+--force} ||
    exit $?

# --- 2. Start the agent -------------------------------------------------------

echo "Starting launchd agent '$LABEL' ..."
if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
    echo "error: launchd could not load the agent." >&2
    exit 1
fi

# --- 3. Wait until it actually answers ---------------------------------------

# A syntax error in the changed code would exit at once and be restarted in a
# loop, so confirm rather than assume.
sleep 1
for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 "$BASE_URL/status" >/dev/null 2>&1; then
        echo
        echo "Recording API is back up on $BASE_URL"
        exit 0
    fi
    sleep 0.5
done

echo
echo "The server did not answer within 30 seconds." >&2
echo "Look in $PROJECT_DIR/logs for the reason." >&2
exit 1
