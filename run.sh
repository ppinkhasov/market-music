#!/usr/bin/env bash
# Launch market-music locally.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv (.venv)…"
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null
  ./.venv/bin/python -m pip install -r requirements.txt
fi

if [ ! -f ".env" ]; then
  echo "No .env found — copy .env.example to .env and add your keys for Spotify/DeepSeek."
  echo "(The market dashboard works without keys; playlists need Spotify.)"
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "Starting market-music on http://${HOST}:${PORT}"
exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
