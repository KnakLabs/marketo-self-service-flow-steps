#!/usr/bin/env bash
# Run the app locally and expose it to Marketo over ngrok.
#
#   ./dev_ngrok.sh
#
# Leave PUBLIC_BASE_URL blank in .env - /install reads the tunnel URL off the
# incoming request, so a new ngrok URL needs no file edits.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-3000}"

# Prefer the project venv. A bare `python` is either absent on macOS or the
# system 3.9, which cannot parse this code (it uses 3.10+ syntax) - and because
# main.py skips a service whose import fails, that shows up as every route
# 404ing rather than as an error.
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

"$PY" - <<'EOF' || { echo "Need Python 3.10+. Create the venv: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

# Fail early if the port is already taken. A second listener on the same port
# does not error - the more specific bind just wins, and the tunnel silently
# serves somebody else's app.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use:"
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN
  echo "Stop it, or run with a different port:  PORT=3001 ./dev_ngrok.sh"
  exit 1
fi

cleanup() { kill "${FLASK_PID:-}" "${NGROK_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "Starting Flask on :$PORT using $PY ..."
"$PY" main.py > /tmp/ssfs_flask.log 2>&1 &
FLASK_PID=$!

echo "Starting ngrok ..."
ngrok http "$PORT" --log=stdout > /tmp/ssfs_ngrok.log 2>&1 &
NGROK_PID=$!

# Wait for the tunnel to come up and read its public URL from ngrok's local API.
URL=""
for _ in $(seq 1 30); do
  URL=$(curl -s http://127.0.0.1:4040/api/tunnels \
        | python3 -c 'import json,sys; t=json.load(sys.stdin)["tunnels"]; print(next((x["public_url"] for x in t if x["public_url"].startswith("https")), ""))' 2>/dev/null || true)
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "ngrok did not come up. Check /tmp/ssfs_ngrok.log"
  exit 1
fi

USER_NAME="$(grep -E '^MARKETO_USER=' .env 2>/dev/null | cut -d= -f2- || true)"

# One install URL per service that actually registered. main.py prints
# "[ok] /<name>" for each, and skips any whose dependencies are missing - so
# reading its log is what tells you which services are really being served,
# rather than listing services that are not running.
SERVICES="$(grep -Eo '^\[ok\] +/[A-Za-z0-9_-]+' /tmp/ssfs_flask.log 2>/dev/null \
            | awk '{print $2}' | sed 's|^/||' || true)"

cat <<INFO

======================================================================
  Tunnel is live: $URL
======================================================================
INFO

if [ -z "$SERVICES" ]; then
  echo
  echo "  No services registered. Check /tmp/ssfs_flask.log - main.py prints"
  echo "  '[skipped] /<name>' with the reason for each one that failed to load."
  echo
else
  echo
  echo "  Paste into Marketo Admin -> Service Providers -> New Service:"
  echo
  for s in $SERVICES; do
    echo "      $URL/$s/install"
  done
  echo
  echo "  Basic auth username: ${USER_NAME:-<MARKETO_USER from .env>}"
  echo "  Basic auth password: <MARKETO_PASSWORD from .env>"
  echo
  echo "  Health checks:"
  for s in $SERVICES; do
    echo "      curl -u \"\$MARKETO_USER:\$MARKETO_PASSWORD\" $URL/$s/status"
  done
  echo
fi

cat <<INFO
  Logs:  /tmp/ssfs_flask.log   /tmp/ssfs_ngrok.log
  ngrok inspector: http://127.0.0.1:4040

  The tunnel URL changes every restart on ngrok's free plan, and Marketo
  stores it at install time - so restarting ngrok means uninstalling and
  reinstalling the service in Marketo.

  Ctrl-C to stop both.
======================================================================

INFO

wait "$FLASK_PID"
