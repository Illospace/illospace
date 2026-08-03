#!/bin/sh
# Start the virtual display ourselves instead of via xvfb-run: the xvfb-run
# readiness handshake can hang forever without output, leaving the container
# "up" with no server bound (observed on the first illo-dev deploy). Chromium
# only needs DISPLAY to exist; uvicorn must own PID 1 semantics via exec.
set -eu

Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
export DISPLAY=:99

exec uvicorn meetbot.app:app --host 0.0.0.0 --port 8010
