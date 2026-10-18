#!/usr/bin/env bash
# Optional DAISY CLI for accessibility integration checks; not a Python dependency.
set -euo pipefail
emend_ace_dir="${EMEND_ACE_DIR:-/workspace/.tools/ace-cli}"
command -v node >/dev/null
command -v npm >/dev/null
npm install --prefix "$emend_ace_dir" --no-audit --no-fund @daisy/ace-cli@1.4.6
test -x "$emend_ace_dir/node_modules/.bin/ace-cli"
