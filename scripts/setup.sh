#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_COMMAND=""

cd "$PROJECT_ROOT"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_COMMAND="$PYTHON_BIN"
else
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON_COMMAND="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_COMMAND" ]] || ! "$PYTHON_COMMAND" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required. Install Python 3.12, or set PYTHON_BIN explicitly." >&2
  exit 1
fi

echo "Using $($PYTHON_COMMAND --version)"
"$PYTHON_COMMAND" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "Setup complete. Start the service with: ./scripts/start.sh"
