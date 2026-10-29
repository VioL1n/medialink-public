#!/bin/zsh
set -eu
ROOT_DIR="${0:A:h:h}"
cd "$ROOT_DIR"
# launchd defaults to 256 descriptors; libtorrent peers plus SQLite and HTTP clients need more.
ulimit -n 8192 2>/dev/null || true
exec "$ROOT_DIR/.venv/bin/python" -m uvicorn medialink.app:create_app --factory \
  --app-dir "$ROOT_DIR/backend" --host "${MEDIALINK_HOST:-0.0.0.0}" --port "${MEDIALINK_PORT:-8766}" \
  --no-access-log --log-level warning
