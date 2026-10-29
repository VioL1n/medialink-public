#!/bin/zsh
set -eu
ROOT_DIR="${0:A:h:h}"
cd "$ROOT_DIR"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
"$UV_BIN" sync --frozen --no-dev
cd "$ROOT_DIR/frontend"
npm ci
npm run build
exec /bin/zsh "$ROOT_DIR/scripts/start.sh"
