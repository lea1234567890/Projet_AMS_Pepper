#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

if [[ -z "${PEPPER_IP:-}" ]]; then
  echo "[backend] PEPPER_IP manquant dans .env"
  exit 1
fi
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "[backend] OPENAI_API_KEY manquant dans .env"
  exit 1
fi

export OPENAI_REALTIME_DISABLED=1
export PEPPER_FORCE_CERTIFI_CA="${PEPPER_FORCE_CERTIFI_CA:-1}"

MY_IP="${TABLET_WS_HOST_IP:-}"
if [[ -z "$MY_IP" ]]; then
  MY_IP="$(python3 - <<'PY'
import socket, os
pepper=os.environ.get('PEPPER_IP','').strip()
s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect((pepper,9559))
    print(s.getsockname()[0])
finally:
    s.close()
PY
)"
fi

TABLET_HTTP_PORT="${TABLET_HTTP_PORT:-8080}"
TABLET_WS_PORT="${TABLET_PORT:-8765}"
TABLET_URL="http://${MY_IP}:${TABLET_HTTP_PORT}/index.html?ws=ws://${MY_IP}:${TABLET_WS_PORT}&cb=$(date +%s)"

echo "[backend] PEPPER_IP=${PEPPER_IP}"
echo "[backend] TABLET_URL=${TABLET_URL}"
exec PYTHONPATH=src python3 -m assistant.main --pepper-ip "$PEPPER_IP" --tablet-url "$TABLET_URL"
