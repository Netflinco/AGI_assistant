#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_COMMAND="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
SERVER_PORT="${PORT:-8000}"

cd "$PROJECT_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ ! -x "$PYTHON_COMMAND" ]]; then
  echo "Python runtime not found: $PYTHON_COMMAND" >&2
  echo "Run ./scripts/setup.sh first, or set PYTHON_BIN to a compatible Python executable." >&2
  exit 1
fi

mkdir -p data
exec "$PYTHON_COMMAND" server.py --port "$SERVER_PORT" "$@"
