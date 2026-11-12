#!/bin/sh
# Container start script: web app (gunicorn) + optional Cloudflare Tunnel.
# CLOUDFLARE_TUNNEL_TOKEN set na thakle tunnel off thakbe, app shudhu chalbe.

PORT="${PORT:-5000}"
export PORT

echo "[app] starting on port ${PORT}"
gunicorn app:app --bind "0.0.0.0:${PORT}" --workers 1 --threads 4 --timeout 120 &
APP_PID=$!

if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]; then
  if command -v cloudflared >/dev/null 2>&1; then
    echo "[tunnel] Cloudflare Tunnel starting"
    # Tunnel ghuma ile abar chalu hobe (crash hole 5 second por retry)
    (
      while true; do
        cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN"
        echo "[tunnel] stopped, retry in 5s"
        sleep 5
      done
    ) &
  else
    echo "[tunnel] cloudflared not installed, tunnel skipped"
  fi
else
  echo "[tunnel] CLOUDFLARE_TUNNEL_TOKEN not set, tunnel off"
fi

shutdown() {
  kill $(jobs -p) 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap shutdown TERM INT

wait "$APP_PID"
STATUS=$?
kill $(jobs -p) 2>/dev/null
exit $STATUS
