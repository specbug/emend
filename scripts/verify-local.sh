#!/usr/bin/env bash
# Runs only the original synthetic corpus in disposable libraries, never yours.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/uv}"
command -v uv >/dev/null
command -v java >/dev/null
command -v rclone >/dev/null
if [[ -z "${EMEND_TEST_CALIBRE_BIN:-}" && -d /Applications/calibre.app/Contents/MacOS ]]; then
  export EMEND_TEST_CALIBRE_BIN=/Applications/calibre.app/Contents/MacOS
fi
: "${EMEND_TEST_CALIBRE_BIN:?Set this to the folder containing calibredb and calibre-debug}"
: "${EMEND_TEST_EPUBCHECK_JAR:?Set this to the official EPUBCheck JAR}"
if [[ -z "${EMEND_TEST_CHROMIUM:-}" ]]; then
  export EMEND_TEST_PLAYWRIGHT=1
fi
if [[ "${1:-}" == --require-ace ]]; then
  : "${EMEND_TEST_ACE:?Set this to the installed ace-cli executable}"
elif [[ -n "${1:-}" ]]; then
  echo "Usage: bash scripts/verify-local.sh [--require-ace]" >&2
  exit 2
fi
emend_validation_dir="${EMEND_VALIDATION_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/emend-validation.XXXXXX")}"
mkdir -p "$emend_validation_dir"
emend_validation_dir="$(cd -- "$emend_validation_dir" && pwd)"
uv sync --frozen --extra visual
uv run --extra visual python - <<'PY'
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

for name in ('calibredb', 'calibre-debug', 'ebook-meta', 'ebook-convert'):
    path = Path(os.environ['EMEND_TEST_CALIBRE_BIN']) / name
    if not path.is_file():
        raise SystemExit(f'Missing Calibre tool: {name}')
if not Path(os.environ['EMEND_TEST_EPUBCHECK_JAR']).is_file():
    raise SystemExit('EPUBCheck JAR not found')
if os.environ.get('EMEND_TEST_ACE') and not os.access(os.environ['EMEND_TEST_ACE'], os.X_OK):
    raise SystemExit('Configured Ace CLI is not executable')
# Catch an absent browser up front, rather than accepting skipped integration cases.
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get('EMEND_TEST_CHROMIUM'))
    browser.close()
print('Preflight: actual Calibre, EPUBCheck, rclone and Chromium are configured.')
if not os.environ.get('EMEND_TEST_ACE'):
    print('Ace is not configured: its full CLI cases will be explicit skips.')
PY
uv run --extra visual ruff check . | tee "$emend_validation_dir/lint.txt"
uv run --extra visual ruff format --check . | tee "$emend_validation_dir/format.txt"
uv run --extra visual pytest -q --junitxml="$emend_validation_dir/tests.xml" | tee "$emend_validation_dir/tests.txt"
uv build 2>&1 | tee "$emend_validation_dir/build.txt"
printf 'Validation evidence: %s\n' "$emend_validation_dir"
printf 'Personal Calibre, Drive and reader checks: docs/mac-validation.md\n'
