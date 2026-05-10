#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]]; then kill "$BACKEND_PID" 2>/dev/null || true; fi
  if [[ -n "${TABLET_PID:-}" ]]; then kill "$TABLET_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

"$ROOT_DIR/run_tablet.sh" &
TABLET_PID=$!
sleep 1
"$ROOT_DIR/run_backend.sh" &
BACKEND_PID=$!

echo "[all] tablet pid=${TABLET_PID}"
echo "[all] backend pid=${BACKEND_PID}"
echo "[all] Ctrl+C pour arrêter"
wait "$BACKEND_PID"
