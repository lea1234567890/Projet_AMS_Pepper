#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR/tablet"
PORT="${TABLET_HTTP_PORT:-8080}"
HOST="${TABLET_HTTP_HOST:-0.0.0.0}"

echo "[tablet] Serving http://${HOST}:${PORT}/index.html"
exec python3 -m http.server "$PORT" --bind "$HOST"
