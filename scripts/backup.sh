#!/bin/zsh
set -eu
ROOT_DIR="${0:A:h:h}"
# Use the same SQLite runtime as the service, including URI paths with spaces.
"$ROOT_DIR/.venv/bin/python" - <<'PY'
import os
import pathlib
import sqlite3
from datetime import datetime, timezone

data = pathlib.Path(os.environ.get('MEDIALINK_DATA', str(pathlib.Path.home() / 'Library/Application Support/MediaLink'))).expanduser().resolve()
source = data / 'medialink.db'
if source.exists():
    directory = data / 'backups'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    destination = directory / ('medialink-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.db')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as reader, sqlite3.connect(destination) as writer:
        reader.backup(writer)
    print('Database backup: ' + str(destination))
PY
