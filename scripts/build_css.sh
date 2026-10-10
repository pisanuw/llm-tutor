#!/usr/bin/env bash
# Rebuild the precompiled Tailwind stylesheet after template/class changes.
#
#   ./scripts/build_css.sh            # uses ./tailwindcss or tailwindcss on PATH
#   TAILWIND_BIN=/path/to/tailwindcss ./scripts/build_css.sh
#
# The output (tutor/web/static/vendor/tailwind.css) is COMMITTED; CI rebuilds
# it and fails if the committed file is stale (see .github/workflows/tests.yml).
# Pinned binary: tailwindcss v3.4.17 standalone
#   https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-x64
#   sha256 7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4
set -euo pipefail
cd "$(dirname "$0")/.."
BIN="${TAILWIND_BIN:-}"
if [ -z "$BIN" ]; then
  if [ -x "./tailwindcss" ]; then BIN="./tailwindcss";
  elif command -v tailwindcss >/dev/null 2>&1; then BIN="tailwindcss";
  else
    echo "tailwindcss binary not found. Download the pinned standalone build:" >&2
    echo "  curl -fsSL -o tailwindcss https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-x64 && chmod +x tailwindcss" >&2
    exit 1
  fi
fi
"$BIN" -c frontend/tailwind.config.js -i frontend/tailwind.input.css \
       -o tutor/web/static/vendor/tailwind.css --minify
echo "wrote tutor/web/static/vendor/tailwind.css"
