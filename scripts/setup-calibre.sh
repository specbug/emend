#!/usr/bin/env bash
# Optional real Calibre CLI integration on Linux x86_64; no system install needed.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "This installer supports Linux x86_64. Use an existing Calibre installation elsewhere." >&2
  exit 1
fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-/workspace/.cache/uv}"
export EMEND_CALIBRE_DIR="${EMEND_CALIBRE_DIR:-/workspace/.tools/calibre-9.17.0}"
uv run python - <<'PY'
import hashlib
import os
import tarfile
import urllib.request
from pathlib import Path

target = Path(os.environ['EMEND_CALIBRE_DIR']).expanduser().absolute()
archive = target.parent / 'calibre-9.17.0-x86_64.txz'
expected = '953e4512a6482848cd803c88b6d942e6afca65dc3b67c7d1353ae048d616de53'
url = 'https://github.com/kovidgoyal/calibre/releases/download/v9.17.0/calibre-9.17.0-x86_64.txz'
target.parent.mkdir(parents=True, exist_ok=True)

def valid(path):
    if not path.is_file():
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == expected

if not valid(archive):
    partial = archive.with_suffix('.partial')
    try:
        with urllib.request.urlopen(url, timeout=120) as src, partial.open('wb') as dst:
            while chunk := src.read(1024**2):
                dst.write(chunk)
        if not valid(partial):
            raise RuntimeError('Calibre distribution checksum mismatch')
        partial.replace(archive)
    finally:
        partial.unlink(missing_ok=True)
target.mkdir(exist_ok=True)
with tarfile.open(archive, 'r:xz') as distribution:
    distribution.extractall(target, filter='data')
print(f'Official Calibre 9.17.0 installed at {target}')
PY
CALIBRE_CONFIG_DIRECTORY="$EMEND_CALIBRE_DIR/../calibre-config" QT_QPA_PLATFORM=offscreen "$EMEND_CALIBRE_DIR/calibredb" --version
