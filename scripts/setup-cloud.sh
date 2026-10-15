#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-/workspace/.cache/uv}"
emend_tools="${EMEND_TOOLS_DIR:-/workspace/.tools}"
command -v uv >/dev/null
command -v java >/dev/null
uv sync --frozen
mkdir -p "$emend_tools"
export EMEND_TOOLS_DIR="$emend_tools"
uv run python - <<'PY'
import hashlib
import os
import urllib.request
import zipfile
from pathlib import Path

root = Path(os.environ['EMEND_TOOLS_DIR'])
archive = root / 'epubcheck-5.3.0.zip'
expected = '6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5'

def valid(path):
    if not path.is_file():
        return False
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest() == expected

if not valid(archive):
    temporary = archive.with_suffix('.download')
    try:
        with urllib.request.urlopen('https://github.com/w3c/epubcheck/releases/download/v5.3.0/epubcheck-5.3.0.zip', timeout=120) as src, temporary.open('wb') as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
        if not valid(temporary):
            raise RuntimeError('EPUBCheck distribution checksum mismatch')
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
with zipfile.ZipFile(archive) as distribution:
    if distribution.testzip() is not None:
        raise RuntimeError('EPUBCheck ZIP CRC verification failed')
    for member in distribution.infolist():
        path = Path(member.filename)
        if path.is_absolute() or '..' in path.parts:
            raise RuntimeError('Unsafe distribution archive member')
    distribution.extractall(root)
print(f'EPUBCheck installed at {root / "epubcheck-5.3.0" / "epubcheck.jar"}')
PY
