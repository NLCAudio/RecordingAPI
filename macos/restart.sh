#!/bin/bash
#
# Restart the recording API on this Mac, for after a change to the code.
#
# Stops anything that is recording, restarts the launchd agent, and waits
# until the server answers. Recordings are stopped through the API rather than
# by killing ffmpeg, so each MP3 is finalised with an accurate duration header.
#
# Usage:
#   macos/restart.sh            asks first if a room is recording
#   macos/restart.sh --force    restarts without asking

set -uo pipefail

LABEL="${RECORDING_API_LABEL:-local.recording-api}"
BASE_URL="${RECORDING_API_URL:-http://127.0.0.1:8000}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="gui/$(id -u)/$LABEL"

FORCE=""
case "${1:-}" in
    "") ;;
    -f | --force) FORCE=1 ;;
    *)
        echo "usage: $0 [--force]" >&2
        exit 2
        ;;
esac

# Only used to read and write JSON, so any Python 3 will do.
PYTHON="$PROJECT_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

if ! launchctl print "$SERVICE" >/dev/null 2>&1; then
    echo "error: no launchd agent '$LABEL' is loaded." >&2
    echo "Run macos/install-agent.sh first." >&2
    exit 1
fi

# --- 1. Ask the running server what it is doing ------------------------------

# A server that is not answering is not an error here: restarting one that is
# already down is just starting it.
rooms=()
if status_json="$(curl -fsS --max-time 5 "$BASE_URL/status" 2>/dev/null)"; then
    # One room per line, because room names contain spaces.
    while IFS= read -r room; do
        [ -n "$room" ] && rooms+=("$room")
    done < <(printf '%s' "$status_json" | "$PYTHON" -c '
import json, sys
for room in json.load(sys.stdin).get("sessions", {}):
    print(room)
')
else
    echo "No server answering on $BASE_URL (it may already be stopped)."
fi

# --- 2. Stop recordings properly, before anything is killed ------------------

if [ "${#rooms[@]}" -gt 0 ]; then
    echo "Recording right now:"
    printf '  %s\n' "${rooms[@]}"
    if [ -z "$FORCE" ]; then
        read -r -p "Stop these recordings and restart? [y/N] " answer
        case "$answer" in
            [Yy]*) ;;
            *)
                echo "Nothing changed; the server is still running."
                exit 1
                ;;
        esac
    fi
    for room in "${rooms[@]}"; do
        echo "Stopping recording in $room ..."
        body="$("$PYTHON" -c 'import json, sys; print(json.dumps({"room_name": sys.argv[1]}))' "$room")"
        # Worth continuing if this fails: the restart is still wanted, and the
        # flush settings leave a killed recording playable regardless.
        curl -fsS --max-time 30 -X POST -H 'Content-Type: application/json' \
            -d "$body" "$BASE_URL/recording/stop" >/dev/null ||
            echo "  warning: could not stop $room cleanly" >&2
    done
fi

# --- 3. Restart the agent -----------------------------------------------------

# -k stops the running server first. launchd also clears out whatever the
# server left behind in its process group, so no ffmpeg survives to hold the
# audio device open.
echo "Restarting launchd agent '$LABEL' ..."
if ! launchctl kickstart -k "$SERVICE"; then
    echo "error: launchd could not restart the agent." >&2
    exit 1
fi

# --- 4. Wait until it actually answers ---------------------------------------

# A syntax error in the code just changed would exit immediately and be
# restarted in a loop, so confirm rather than assume.
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
echo "Look at server.log and launchd.log in $PROJECT_DIR for the reason." >&2
exit 1
