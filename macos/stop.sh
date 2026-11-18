#!/bin/bash
#
# Stop the recording API on this Mac: every server running on it, whatever
# started it and whichever port it listens on (see stop_servers.py).
#
# Recordings are ended through the API first, so each MP3 is finalised. The
# launchd agent is unloaded, or it would start its server straight again; the
# server is back at the next log in, or with macos/restart.sh.
# macos/install-agent.sh --remove keeps it from starting at all.
#
# Usage:
#   macos/stop.sh            asks first if a room is recording
#   macos/stop.sh --force    stops without asking
#   macos/stop.sh --list     only shows the servers that are running

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/stop_servers.py" "$@"
