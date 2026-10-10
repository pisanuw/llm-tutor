#!/usr/bin/env bash
# Course book build script (optional): a thin wrapper around Quarto.
# Usage: ./build.sh {preview|render|pdf|clean|check}; see book/README.md.

set -euo pipefail

cd "$(dirname "$0")"

cmd="${1:-render}"

case "$cmd" in
  preview)
    exec quarto preview
    ;;
  render)
    exec quarto render
    ;;
  pdf)
    exec quarto render --to pdf
    ;;
  clean)
    rm -rf _book _freeze .quarto
    echo "Cleaned build artifacts."
    ;;
  check)
    # Sanity-check that all referenced chapter files exist.
    missing=0
    for f in $(grep -E '^\s+-\s+[^[:space:]]+\.qmd' _quarto.yml | awk '{print $2}'); do
      if [ ! -f "$f" ]; then
        echo "MISSING: $f"
        missing=$((missing + 1))
      fi
    done
    if [ "$missing" -eq 0 ]; then
      echo "All referenced .qmd files exist."
    else
      echo "$missing missing files."
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 {preview|render|pdf|clean|check}"
    exit 1
    ;;
esac
