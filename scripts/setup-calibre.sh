#!/usr/bin/env bash
# Checksum-verified official tools in an isolated directory, never /Applications.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    export EMEND_CALIBRE_PLATFORM=linux
    export EMEND_CALIBRE_DIR="${EMEND_CALIBRE_DIR:-/workspace/.tools/calibre-9.17.0}"
    ;;
  Darwin:arm64|Darwin:x86_64)
    export EMEND_CALIBRE_PLATFORM=macos
    export EMEND_CALIBRE_DIR="${EMEND_CALIBRE_DIR:-$HOME/Library/Caches/emend/calibre-9.17.0}"
    ;;
  *) echo "Use an existing Calibre installation on this platform." >&2; exit 1 ;;
esac
export UV_CACHE_DIR="${UV_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/uv}"
uv run python - <<'PY'
import hashlib
import os
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

target = Path(os.environ['EMEND_CALIBRE_DIR']).expanduser().absolute()
mac = os.environ['EMEND_CALIBRE_PLATFORM'] == 'macos'
filename = 'calibre-9.17.0.dmg' if mac else 'calibre-9.17.0-x86_64.txz'
expected = ('fdab4bd1400996c381845b7a95026f96bd9f68bfec68cf3d49afd458b4717842' if mac
            else '953e4512a6482848cd803c88b6d942e6afca65dc3b67c7d1353ae048d616de53')
size = 357816630 if mac else 201590724
archive = target.parent / filename
target.parent.mkdir(parents=True, exist_ok=True)

def valid(path):
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == expected

if not valid(archive):
    partial = archive.with_suffix('.partial')
    # The official archive retains older versions after GitHub release assets rotate.
    urls = [f'https://download.calibre-ebook.com/9.17.0/{filename}',
            f'https://github.com/kovidgoyal/calibre/releases/download/v9.17.0/{filename}']
    for index, url in enumerate(urls):
        try:
            remaining = size
            with urllib.request.urlopen(url, timeout=120) as src, partial.open('wb') as dst:
                while chunk := src.read(min(1024**2, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise RuntimeError('Calibre distribution exceeds its pinned size')
                    dst.write(chunk)
            if not valid(partial):
                raise RuntimeError('Calibre distribution checksum/size mismatch')
            partial.replace(archive)
            break
        except Exception:
            if index == len(urls) - 1:
                raise
            print('Official archive unavailable; trying the official GitHub release.')
        finally:
            partial.unlink(missing_ok=True)
target.mkdir(exist_ok=True)
if not mac:
    with tarfile.open(archive, 'r:xz') as distribution:
        distribution.extractall(target, filter='data')
    binary = target
else:
    mount = Path(tempfile.mkdtemp(prefix='emend-calibre-image-'))
    attached = False
    try:
        subprocess.run(['hdiutil', 'attach', str(archive), '-readonly', '-nobrowse',
                        '-mountpoint', str(mount)], check=True)
        attached = True
        app = mount / 'calibre.app'
        if not app.is_dir():
            raise RuntimeError('Official Calibre image has no calibre.app')
        subprocess.run(['ditto', str(app), str(target / 'calibre.app')], check=True)
    finally:
        if attached:
            result = subprocess.run(['hdiutil', 'detach', str(mount)], capture_output=True)
            if result.returncode:
                subprocess.run(['hdiutil', 'detach', '-force', str(mount)], check=True)
        mount.rmdir()
    binary = target / 'calibre.app' / 'Contents' / 'MacOS'
env = {**os.environ, 'CALIBRE_CONFIG_DIRECTORY': str(target.parent / 'calibre-config'),
       'QT_QPA_PLATFORM': 'offscreen'}
result = subprocess.run([str(binary / 'calibredb'), '--version'], env=env,
                        text=True, capture_output=True, check=True)
if '9.17' not in result.stdout:
    raise RuntimeError('Installed Calibre version does not match pinned 9.17.0')
print(result.stdout.strip())
print(f'Calibre CLI directory: {binary}')
PY
